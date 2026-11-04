"""
Whisper local — transcrição offline com faster-whisper.

Mesma segmentação de fala do GroqListener (core.groq_stt): os blocos do
microfone são cortados em utterances e só então transcritos, mas sem nenhuma
chamada de rede. É o que o modo economy usa quando o usuário não quer nuvem.

O modelo é carregado na primeira fala (download único, fica em cache no
Hugging Face). Configuração opcional em config/api_keys.json:

    "plugin_config": {
      "local_stt": { "model": "medium", "language": "pt",
                     "device": "cuda", "compute_type": "float16" }
    }

Modelos: tiny, base, small, medium, large-v3.

GPU: `device: "cuda"` só funciona com as bibliotecas CUDA 12 dentro do venv —
dá para instalar sem root, e neste projeto já estão instaladas:

    .venv/bin/pip install nvidia-cublas-cu12 nvidia-cudnn-cu12

Elas não ficam no caminho do loader do sistema, então _preload_cuda_libs()
carrega à mão antes da primeira decodificação. Se a GPU não puder ser usada
(sem as bibliotecas, VRAM tomada por outro modelo, driver incompatível) a
transcrição cai para CPU/int8 em vez de deixar o assistente surdo.

Neste host, medido num clipe de 11 s em português: `small` em CPU/int8 levava
2,6 s e errava números por extenso ("9h30" em vez de "nove e meia"); `medium`
em GPU leva 0,6 s e acerta. Use `int8_float16`, não `float16`: os pesos em int8
custam ~1,1 GB de VRAM em vez de ~2,4 GB, e a placa é dividida com o Ollama —
em `float16` o carregamento estourava com o modelo local do cérebro já na
memória. `large-v3` não cabe de jeito nenhum nessa divisão.
"""
from __future__ import annotations

import ctypes
import glob
import os
import sysconfig
import threading
from typing import Callable

import numpy as np

from core import groq_stt as _groq_stt
from core.groq_stt import GroqListener

NAMESPACE = "local_stt"
DEFAULT_MODEL = "small"
DEFAULT_LANGUAGE = "pt"
MODELS = ("tiny", "base", "small", "medium", "large-v3")

_APPROX_MB = {"tiny": 75, "base": 145, "small": 480, "medium": 1500,
              "large-v3": 3000}

# Up to this clip length the decode uses beam search instead of greedy. Short
# commands are where a wrong word is expensive and the extra cost is small; see
# transcribe_pcm().
_BEAM_MAX_SECONDS = 8.0

# Anti-hallucination thresholds, the defaults from the original Whisper.
# `no_speech_prob` is the one that matters here: on silence, a fan, music or a
# TV in the room the model happily emits a stock sentence — measured on this
# host, half a second of *pure silence* came back as "e" repeated and a hiss
# came back as "Legendas pela comunidade de Amara.org", both with
# no_speech_prob ≈ 0.88. Invented text is worse than no text: it becomes a turn
# the user never spoke, and the assistant answers it.
_NO_SPEECH_THRESHOLD = 0.75
_LOGPROB_THRESHOLD = -1.2
_COMPRESSION_THRESHOLD = 2.4
# Dormindo, a chamada é uma palavra só ("Gino", "oi Gino") e chega com
# no_speech_prob alta; até este valor ela ainda é transcrita para acordar.
WAKE_NO_SPEECH_THRESHOLD = 0.92
# A quiet microphone is not silence: lower the internal Silero threshold so a
# soft sentence survives, while keeping the model's own anti-hallucination.
_VAD_PARAMETERS = {
    "threshold": 0.30,
    "min_speech_duration_ms": 150,
    "min_silence_duration_ms": 300,
}

# What to transcribe with once the GPU is out: on CPU a medium/large model takes
# seconds per sentence and the segmenter hands it ever-shorter clips, so the
# text degrades instead of improving. A small model on CPU beats a big one that
# cannot keep up.
_CPU_FALLBACK_MODEL = "small"


def _model_is_cached(name: str) -> bool:
    """True when faster-whisper already has this model on disk.

    The first load of a model downloads it, and saying so in the status is
    useful once. Saying "primeira vez baixa ~1500 MB" on every start, when the
    files have been there for weeks, reads like a download is happening — the
    user should not have to wonder. Only consult the cache; on any doubt, say
    nothing rather than the wrong thing.
    """
    roots = [os.environ.get("HF_HUB_CACHE"),
             os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for d in glob.glob(os.path.join(root, f"models--*--faster-whisper-{name}")):
            snap = os.path.join(d, "snapshots")
            try:
                if os.path.isdir(snap) and os.listdir(snap):
                    return True
            except OSError:
                pass
    return False


def _preload_cuda_libs() -> None:
    """Load the pip-shipped CUDA libraries into this process, globally.

    ctranslate2 resolves `libcublas.so.12` / `libcudnn.so.9` by soname at the
    first GPU call. The `nvidia-*` wheels put them inside the venv, which is not
    on the dynamic loader's search path, so a GPU decode used to fail with
    "Library libcublas.so.12 is not found". Setting LD_LIBRARY_PATH would work
    only when the launcher remembers to export it — the menu shortcut, the
    autostart entry and a plain `python main.py` would all behave differently.
    Loading them here with RTLD_GLOBAL makes the later dlopen find them already
    resident, whatever launched the app. Failures are ignored on purpose: no
    wheels just means the CPU path, which is handled by the caller.
    """
    nvidia = os.path.join(sysconfig.get_paths()["purelib"], "nvidia")
    if not os.path.isdir(nvidia):
        return
    # cuDNN is built on top of cuBLAS, so the math core goes in first.
    ordered = [os.path.join(nvidia, n, "lib") for n in ("cublas", "cudnn")]
    rest = [d for d in sorted(glob.glob(os.path.join(nvidia, "*", "lib")))
            if d not in ordered]
    for folder in ordered + rest:
        for so in sorted(glob.glob(os.path.join(folder, "*.so*"))):
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def settings(get_plugin_config: Callable[[str], dict]) -> dict:
    """Reads plugin_config.local_stt, falling back to sensible defaults."""
    try:
        cfg = get_plugin_config(NAMESPACE) or {}
    except Exception:
        cfg = {}
    # The pre-roll ring is sized in ONE place (core.groq_stt); the key lives in
    # the groq namespace so local and cloud Whisper share the same setting.
    try:
        groq_cfg = get_plugin_config("groq") or {}
    except Exception:
        groq_cfg = {}
    model = str(cfg.get("model") or DEFAULT_MODEL).strip().lower()
    if model not in MODELS:
        model = DEFAULT_MODEL
    lang = str(cfg.get("language") or DEFAULT_LANGUAGE).strip().lower()
    if lang.startswith("pt"):
        lang = "pt"
    elif "-" in lang:
        lang = lang.split("-", 1)[0]
    return {
        "model": model,
        "language": lang or DEFAULT_LANGUAGE,
        "device": str(cfg.get("device") or "cpu").strip().lower(),
        "compute_type": str(cfg.get("compute_type") or "int8").strip().lower(),
        "preroll_s": _groq_stt.preroll_from_config(groq_cfg),
        # Same cap as the cloud listener: one key in plugin_config.groq.
        "max_utterance_s": _groq_stt.utterance_cap_from_config(groq_cfg),
    }


class _Whisper:
    """Lazy faster-whisper model — import and load only when first needed."""

    def __init__(self, model_name: str, language: str, device: str,
                 compute_type: str, on_status: Callable[[str], None],
                 prompt: str = "", sample_rate: int = 16000):
        self._model_name = model_name
        self._language = None if language in ("", "auto") else language
        self._device = device
        self._compute_type = compute_type
        self._sr = int(sample_rate or 16000)
        # Whisper's vocabulary hint: without it the assistant's name comes back
        # as whatever sounds alike ("Ei, alívio!" for "Alice, tá aí?"), and a
        # wake-by-name gate then refuses the call that woke nothing.
        self._prompt = prompt
        self._status = on_status or (lambda m: None)
        self._model = None
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise RuntimeError(
                    "faster-whisper não instalado — rode "
                    "'.venv/bin/pip install faster-whisper'") from e
            mb = _APPROX_MB.get(self._model_name, 0)
            hint = (f" (baixando ~{mb} MB, só nesta primeira vez)"
                    if mb and not _model_is_cached(self._model_name) else "")
            self._status(f"Whisper local: carregando '{self._model_name}'{hint}…")
            try:
                if str(self._device).startswith("cuda"):
                    _preload_cuda_libs()
                self._model = WhisperModel(
                    self._model_name, device=self._device,
                    compute_type=self._compute_type)
            except Exception as e:
                # A GPU that will not load (driver, missing cuDNN libs, VRAM
                # already taken by another model) must not leave the assistant
                # deaf — and it must not abort the start-up path either, which is
                # what raising here would do. Say it once, then do the work on
                # the CPU.
                if self._device == "cpu":
                    raise
                self._fall_back_to_cpu(e)
                return self._model
            self._status(f"Whisper local pronto ({self._model_name}, "
                         f"{self._device}/{self._compute_type}).")
            return self._model

    def _fall_back_to_cpu(self, why) -> None:
        """Switch to the CPU path that always works — and to a model that keeps up."""
        from faster_whisper import WhisperModel
        name = self._model_name
        if name in ("medium", "large-v3"):
            name = _CPU_FALLBACK_MODEL
        self._status(f"Whisper: GPU indisponível ({str(why)[:90]}) — "
                     f"transcrevendo na CPU com '{name}'.")
        self._device, self._compute_type = "cpu", "int8"
        self._model_name = name
        self._model = WhisperModel(name, device="cpu", compute_type="int8")

    def warm(self) -> None:
        """Load the model and pay the one-off CUDA cost before anyone speaks.

        The first real utterance used to cost 3.4 s for a 2.4 s clip — model
        load, kernel compilation and cache fills all landed on the one sentence
        that matters most ("Gê, está aí?"). Transcribing a second of silence now
        at start-up moves that price to boot time, where nobody is waiting.
        """
        try:
            self.transcribe_pcm(b"\x00\x00" * int(self._sr or 16000), prompt="")
        except Exception as e:
            print(f"[STT] warm-up falhou: {e}")

    def transcribe_pcm(self, pcm: bytes, prompt: str = "",
                       wake: bool = False) -> str:
        """int16 mono PCM at the listener's sample rate → text.

        ``wake`` is True while the assistant sleeps: the call is short, so the
        anti-hallucination no-speech gate is relaxed (see
        WAKE_NO_SPEECH_THRESHOLD) instead of dropping the owner's own name.
        """
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        seconds = len(audio) / float(self._sr) if self._sr else 0.0
        # Distant microphones deliver speech at a level the model reads as a
        # whisper. Boost only quiet clips, and only for the decode.
        if audio.size:
            rms = float(np.sqrt(np.mean(audio * audio)))
            if 0.004 <= rms < 0.03:
                audio = audio * min(10.0, 0.055 / rms)
                peak = float(np.max(np.abs(audio)))
                if peak > 0.95:
                    audio = audio * (0.95 / peak)
        # Greedy decoding is what turns a short command into a different short
        # command ("pela ajuda" came back as "pela futebol"): with beam 1 the
        # model commits to the first plausible token and never looks back. On a
        # 2s clip beam 5 costs a few tenths of a second, so it is worth it; past
        # _BEAM_MAX_SECONDS the clip is a sentence in a flow, latency matters
        # more, and greedy holds up well enough.
        beam = 5 if 0.0 < seconds <= _BEAM_MAX_SECONDS else 1
        try:
            return self._decode(audio, beam, prompt, wake=wake)
        except Exception as e:
            # Same reasoning as the load: a CUDA failure during decode (out of
            # memory because Ollama took the card, a missing .so) is recovered
            # here rather than surfacing as a silent turn.
            if self._device == "cpu":
                raise
            self._fall_back_to_cpu(e)
            return self._decode(audio, beam, prompt, wake=wake)

    def _decode(self, audio, beam: int, prompt: str, wake: bool = False) -> str:
        model = self._ensure()
        no_speech = WAKE_NO_SPEECH_THRESHOLD if wake else _NO_SPEECH_THRESHOLD
        kw = dict(
            language=self._language,
            beam_size=beam,
            condition_on_previous_text=False,
            initial_prompt=prompt or self._prompt or None,
            no_speech_threshold=no_speech,
            log_prob_threshold=_LOGPROB_THRESHOLD,
            compression_ratio_threshold=_COMPRESSION_THRESHOLD,
        )
        try:
            # The segmenter above cuts on loudness, which a fan, music or a TV
            # also triggers; Silero VAD is what tells speech from the rest. It
            # needs onnxruntime, hence the fallback — without it the per-segment
            # filter below still keeps the worst hallucinations out.
            segments, _ = model.transcribe(audio, vad_filter=True,
                                           vad_parameters=_VAD_PARAMETERS, **kw)
            segments = list(segments)
        except Exception as e:
            if "vad" not in str(e).lower() and "onnx" not in str(e).lower():
                raise
            print(f"[STT] VAD indisponível ({str(e)[:80]}) — seguindo sem ele.")
            segments, _ = model.transcribe(audio, vad_filter=False, **kw)
            segments = list(segments)
        kept: list[str] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            # Belt and braces: enforce the thresholds here too, because whether
            # faster-whisper itself drops a segment by no_speech_prob depends on
            # the version and on whether VAD ran.
            if seg.no_speech_prob > no_speech:
                continue
            if seg.avg_logprob < _LOGPROB_THRESHOLD:
                continue
            if seg.compression_ratio > _COMPRESSION_THRESHOLD:
                continue
            kept.append(text)
        return " ".join(kept).strip()


class LocalWhisperListener(GroqListener):
    """Drop-in replacement for GroqListener backed by a local model.

    Same feed()/flush()/stop() surface, so the audio callback and the economy
    brain do not need to know which transcriber is underneath.
    """

    def __init__(self, on_text: Callable[[str], None],
                 on_status: Callable[[str], None] | None = None,
                 model: str = DEFAULT_MODEL, language: str = DEFAULT_LANGUAGE,
                 sample_rate: int = 16000, device: str = "cpu",
                 compute_type: str = "int8",
                 prompt: str = "",
                 fallback_transcribe: Callable[[bytes], str] | None = None,
                 on_utterance: Callable[[str, bytes], None] | None = None,
                 precheck: Callable[[bytes], bool] | None = None,
                 on_lost: Callable[[str, str, float], None] | None = None,
                 preroll_s: float | None = None,
                 max_utterance_s: float | None = None) -> None:
        whisper = _Whisper(model, language, device, compute_type,
                           on_status or (lambda m: None), prompt=prompt,
                           sample_rate=sample_rate)
        super().__init__(
            "", model, language,
            on_text=on_text, on_status=on_status, sample_rate=sample_rate,
            prompt=prompt, transcribe_fn=None,
            engine_label="Whisper local", usage_key="whisper",
            fallback_transcribe=fallback_transcribe,
            on_utterance=on_utterance,
            precheck=precheck,
            on_lost=on_lost,
            preroll_s=preroll_s,
            max_utterance_s=max_utterance_s)
        # Read self.prompt at call time, not now: the assistant's name is only
        # settled later in startup, and the hint has to carry the real one.
        self._transcribe_fn = lambda pcm: whisper.transcribe_pcm(
            pcm, self.prompt, wake=self.sleeping)
        # Warm the model while the microphone and the brain are still coming up,
        # so the FIRST thing the user says is not the one that pays for CUDA
        # kernels and cache fills (measured: 3.4 s for a 2.4 s clip, warm ~0.3 s).
        threading.Thread(target=whisper.warm, daemon=True,
                         name="whisper-warm").start()
