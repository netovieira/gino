"""
Groq listening — the microphone goes to Whisper on Groq, and only the TEXT
reaches the Gemini Live session.

Why anyone would want this: Gemini Live bills audio input by the second, and
on a quiet day most of what it hears is room tone. Whisper on Groq is free up
to a generous daily limit and transcribes Portuguese very well. With this on,
the conversation stays on Gemini (voice out, tools, memory, everything), but
"what did the user say" is answered here, for free, and sent as text.

What changes for the user: speech is cut into utterances locally (start when
the room gets louder than its own floor, end after ~0.7 s of silence), so
JARVIS answers a beat after you stop talking rather than mid-sentence, and it
cannot be interrupted by voice while it speaks — ESC or the INTERRUPT button
still work.

Settings live in config/api_keys.json under plugin_config.groq, written by the
⚙ → PLUGIN SETTINGS → GROQ form that plugins/groq_listen.py declares:
    api_key   gsk_...            (https://console.groq.com/keys)
    model     whisper-large-v3-turbo | whisper-large-v3
    language  pt | en | ... | auto
    preroll_s 1.5                seconds of audio kept before the first phoneme
    max_utterance_s 15           voiced seconds before a live segment is cut
"""
from __future__ import annotations

import collections
import io
import math
import os
import queue
import re
import threading
import time
import unicodedata
import wave
from typing import Callable, NamedTuple

import numpy as np
import requests

from core import duck as _duck
from core import noise_filter as _noise
from core import silero_vad
from core import stt_streaming

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")
NAMESPACE = "groq"

# Segmenter, in 64 ms blocks at 16 kHz (CHUNK_SIZE 1024 in main.py).
_START_BLOCKS = 3        # ~190 ms above threshold opens an utterance
_END_BLOCKS = 11         # ~700 ms below closes it
# Guided voice enrollment: the owner repeats a short phrase and the flow must
# never advance on a breath/pause between words. 28 blocks = ~1.8 s of silence,
# the "só depois de 1,5–2 s de fim de fala" rule (04/11/2026).
_ENROLL_END_BLOCKS = 28
# An utterance only closes on BLOCKS, and silence is a block. When the
# microphone stops being heard at all (muted, a phone call, the device gone)
# no block arrives and the segment stays open forever: `speaking` stuck at
# True held every out-of-turn line for the full 30 s cap. Past this long with
# no block, an open segment is an orphan — provably unfinishable.
_STALE_SPEAKING_S = 5.0
# Pre-roll: audio kept from BEFORE the first phoneme. ONE place sizes the ring
# (override: plugin_config.groq.preroll_s in config/api_keys.json).
_PREROLL_S = 1.5
_MIN_PREROLL_S = 0.0     # a disabled ring still keeps _START_BLOCKS to fire
_MAX_PREROLL_S = 5.0     # a small cap keeps a typo from eating memory
_BLOCK_SECONDS = 0.064   # one microphone frame: CHUNK_SIZE 1024 at 16 kHz
_MIN_SPEECH = 5          # ~320 ms of voiced blocks, else dropped as noise
_MAX_BLOCKS = 600        # ~38 s hard cap, then sent as-is
# A live utterance used to grow to that 38 s cap whenever nobody paused — a TV
# monologue, music, a fan — and 38 s is not transcribable: Whisper gave up on
# the whole blob ("não reconheceu nada no que ouvi") and the owner's words
# inside it went with it. Past ~15 s of VOICED audio the segment is handed
# over and a new one starts on the same breath, so a monologue yields chunks
# that transcribe while a normal owner turn (< 10 s) is never chopped.
# Override: plugin_config.groq.max_utterance_s in config/api_keys.json.
_UTTERANCE_CAP_S = 15.0
_MIN_UTTERANCE_S = 5.0
_MAX_UTTERANCE_S = _MAX_BLOCKS * _BLOCK_SECONDS   # never looser than the hard cap
# Known non-owner voice (a TV the gate can already identify) is cut tighter:
# none of it deserves a long turn, and short chunks keep an owner sentence
# spoken over the TV from hiding inside one blob.
_NON_OWNER_CAP_S = 8.0
# While the assistant is speaking only echo-guard-approved blocks reach this
# segmenter, but a stray one must not grow into a minute of self-audio: cut it
# short so a barge-in phrase still works and a false start costs 2.5 s, not 38.
_SPEAK_CAP_BLOCKS = 40
# Quiet, distant microphones: Whisper sees a whisper and gives up. Level is
# raised after the utterance is cut, never before the VAD, so the segmenter
# still tracks the real room floor. The ceiling is deliberately MODEST: a room
# noise at 0.005 RMS multiplied by 10x became "speech" for Whisper and reached
# the voiceprint as a turn (owner, 04/11/2026). 3x lifts a distant voice into
# transcribable range without turning a weak hum into a loud one.
_TARGET_RMS = 0.04
_MAX_GAIN = 3.0
# Asleep, the 25 s cap is worthless: it only closes on 700 ms of silence, and a
# room with music or a fan never gives that. The block grew to the full 25 s and
# was then thrown away for being "too long for a wake call" — every single time.
# Cutting on time instead gives one chance to hear the name every ~3.8 s, which
# is just under _SLEEP_MAX_S so the block is never discarded unseen.
_SLEEP_CAP_BLOCKS = 59
_FLOOR_MIN = 0.002       # the quietest floor we believe
_THRESH_MIN = 0.006      # the quietest threshold we accept (≈ -44 dBFS)
_FLOOR_MULT = 2.2        # speech must be this many times the room floor
_LEVEL_LOG_S = 5.0       # console line with the mic level, to tune the above
_SLEEP_MAX_S = 4.0        # asleep, longer "utterances" are music/TV, not a call

# ── Portão de transitório: tecla, clique e impacto não abrem turno ──────────
# The microphone stays open all day, and energy alone is not voice: a key, a
# mouse click or a table impact crossed the threshold for a few frames and
# produced ghost turns. The measurement the owner already accepted as the
# discriminator: a transient carries its energy OUTSIDE the voice band and has
# a NARROW envelope. An analyzed window is "possible voice" only when ALL THREE
# hold (fractions, so the level does not matter):
#   * 300–3400 Hz holds at least 35% of the energy (a thump is all bass);
#   * the envelope's active run is at least 25 ms wide (a click is 1–3 ms);
#   * spectral entropy inside the voice band is at least 0.45 (a beep, a tone
#     and a ringing impact concentrate in a few bins).
_VOICE_BAND_LO_HZ = 300.0
_VOICE_BAND_HI_HZ = 3400.0
_VOICE_MIN_BAND_FRAC = 0.35
_VOICE_MIN_ENVELOPE_S = 0.025
_VOICE_MIN_ENTROPY = 0.45
# The window is a few 64 ms blocks, NOT the utterance: the callback thread can
# pay one small rFFT per voiced frame, never a multi-second transform. Three
# blocks (192 ms) make all three measurements stable without adding latency —
# a turn still opens after _START_BLOCKS voiced frames.
_VOICE_WINDOW_BLOCKS = 3
_VOICE_ANALYSIS_MAX_SAMPLES = 8192   # public voice_proof() cap: 512 ms
# Envelope resolution: 8 ms sub-frames inside the analysis window.
_ENVELOPE_SUBFRAME = 128
_ENVELOPE_REL_FLOOR = 0.25           # "active" = sub-frame >= 25% of the peak
# Safety valve against a false negative, which the owner fears far more than a
# ghost: a transient is SHORT. Energy that stays above the threshold for
# ~1.3 s without ever passing the voice proof is not a click — it is a strange
# but sustained sound (a filtered mic, a rumble), and it opens the turn just
# like before this gate. Counted separately, never silent.
_VOICE_BYPASS_BLOCKS = 20
# A clip is only emitted when its segment carried voice proof. This is the
# second half of the same rule: no proof -> no transcription, no voiceprint.
_EMIT_MIN_VOICE_BLOCKS = _START_BLOCKS
# Stable reason codes for a refused clip. main.py logs these strings, so they
# are a contract: "banda", "envelope" and "entropia" are the three failed
# proofs; "silencio"/"curto" mean there was nothing to measure; "sem_prova"
# means the segment reached the emit path without ever proving voice.
VOICE_GATE_REASONS = ("banda", "envelope", "entropia", "silencio", "curto",
                      "sem_prova")

# Endpointing: a pause is not necessarily the end of a turn. Smart Turn v3.2
# looks at the last 8 s and decides; if it says "incomplete", the next segment
# is merged with this one before transcription. This is what prevents
# "…mas" from becoming a whole turn while the user is still talking.
_ENDPOINT_THRESHOLD = 0.5
_ENDPOINT_MIN_S = 0.6      # very short commands close immediately
_CONTINUE_WINDOW_S = 3.0   # how long to wait for the rest of the sentence
_MAX_PENDING_S = 18.0      # never hold a fragment forever

# ── Cauda adaptativa do endpointing (05/11/2026) ────────────────────────────
# A cauda fixa de 3 s paga-se em TODO pauseamento que o modelo de "fim de
# turno" (score já calculado em `_endpoint_complete`) chama de incompleto.
# Medida no PC: mediana do vão YOU→resposta 7 s. Para frase comum de conversa
# a cauda fecha em ~1,2 s (ganho de 1,8 s por turno); os 3 s ficam onde o dono
# NÃO pode ser cortado: verbo de ação, número, confirmação ou pedido sensível.
# Por que uma lista aqui e não a detecção dos gates: os portões privilegiados
# vivem em main.py, que importa este módulo — importa-los de volta criaria
# ciclo. A lei dos gates (nunca afrouxar portagem) não é tocada por esta
# listagem: ela só escolhe entre 1,2 s e 3,0 s de silêncio; o regex é
# deliberadamente LARGO demais — errar para os 3 s custa latência, errar para
# o outro lado custa a frase do dono cortada no meio.
_CONTINUE_WINDOW_CHAT_S = 1.2
# Score "quase acabou": acima disso a pausa é respiração, não pensamento, e a
# cauda encurta quando não há transcrição local para ler a palavra.
_CHAT_TAIL_PROB = 0.35
_TAIL_SENSITIVE_RE = re.compile(
    r"\d|"
    r"\b(reinici\w*|deslig\w*|ligar|liga\b|apag\w*|envi\w*|mandar|mande|"
    r"mande\b|delet\w*|instal\w*|atualiz\w*|configur\w*|confirm\w*|"
    r"aceit\w*|autoriz\w*|senha|codigo|c[oó]digo|energia|luz|luzes|"
    r"ar[- ]?condicionado|port[aã]o|fech\w*|tranc\w*|lembret\w*|lembrar|"
    r"agend\w*|cancel\w*|toca\b|tocar|abre\b|abrir|aumenta|diminui|"
    r"play\b|pausa)\b",
    re.IGNORECASE)


def _tail_norm(text: str) -> str:
    """Minúsculas sem acento (mesmo jogo do roteador de fluxos)."""
    value = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(c for c in value if unicodedata.category(c) != "Mn")


def tail_sensitive(text: str) -> bool:
    """A pausa aconteceu dentro de frase com verbo de ação/número/ sensível?"""
    return bool(_TAIL_SENSITIVE_RE.search(_tail_norm(text)))


def continue_window_for(text: str = "",
                        probability: float | None = None) -> float:
    """Segundos de cauda para esta pausa de vírgula.

    * texto (transcrição local do trecho em curso) com sinal sensível → 3,0 s;
    * texto de conversa comum → 1,2 s;
    * sem texto (transcritor remoto, falha): decide o score do "fim de turno"
      — quase-acabado (≥ 0,35) fecha em 1,2 s; pausa clara de pensamento
      mantém os 3,0 s.
    """
    if tail_sensitive(text):
        return _CONTINUE_WINDOW_S
    if str(text or "").strip():
        return _CONTINUE_WINDOW_CHAT_S
    if probability is not None and probability >= _CHAT_TAIL_PROB:
        return _CONTINUE_WINDOW_CHAT_S
    return _CONTINUE_WINDOW_S

# ── Fala retida quando o microfone fecha no meio da frase ───────────────────
# Owner's rule (02/11/2026): he is NEVER ignored nor cut — a registered
# utterance always ends in an answer or in an explicit item. When the mute
# window (or a phone call) closes the microphone while a segment is open, the
# audio he was saying at that instant used to be thrown away (`discard_open`).
# Now it is RETAINED (`hold_open`): the next microphone block after the reopen
# hands it to the worker, where the endpointing window decides whether it is
# the head of the next sentence (merged, one transcript, one answer) or a
# fragment old enough to be a turn of its own.
_HELD_MIN_VOICED = _MIN_SPEECH   # fewer voiced blocks than an utterance has
_HELD_MIN_SECONDS = 0.32         # ...or too little audio to transcribe
_HELD_WAIT_S = 10.0              # how long a retained head waits for its tail
# What counts as "there was a voice here" when a clip transcribes to nothing:
# the segmenter already required voiced blocks to emit it, so one of these is
# the second piece of evidence before the owner is asked to repeat.
_LOST_MIN_SPEECH_S = 0.35
_LOST_MIN_SECONDS = 0.8

# Whisper hallucinates on silence, and does so with a short list of
# favourites. Anything that is only one of these is dropped.
_NOISE = {
    "obrigado", "obrigado.", "obrigada", "obrigada.", "tchau", "tchau.", "legendas",
    "thank you", "thank you.", "thanks for watching", "thanks for watching.",
    "you", "bye", "bye.", "legendas pela comunidade amara.org",
    "legendas pela comunidade amara.org.", "amara.org",
}


# Speech evidence inside one clip: 64 ms frames whose RMS reaches a level only
# a nearby voice produces. Rain, a fan and a table thump stay far below it (the
# 02/11 ghost clips peaked at 0.06–0.07 on the mic; the owner at the desk hits
# 0.27–0.55), and a spoken "Gino" covers several frames. Used by the wake gate
# so a lone name decoded from noise is not mistaken for a call.
_SPEECH_FRAME_SAMPLES = 1024          # 64 ms at 16 kHz
_SPEECH_FRAME_RMS = 0.05


def _speech_seconds(f: np.ndarray) -> float:
    """Seconds of 64 ms frames loud enough to be a voice in the room."""
    n = f.size // _SPEECH_FRAME_SAMPLES
    if n <= 0:
        return 0.0
    frames = f[:n * _SPEECH_FRAME_SAMPLES].reshape(n, _SPEECH_FRAME_SAMPLES)
    levels = np.sqrt(np.mean(frames * frames, axis=1))
    return float(np.count_nonzero(levels >= _SPEECH_FRAME_RMS)
                 * _SPEECH_FRAME_SAMPLES) / 16000.0


class VoiceProof(NamedTuple):
    """The three numbers behind "this window can be a voice", and the verdict.

    ``reason`` is ``""`` when ``ok`` is True, otherwise the first failed test
    in the fixed order ``banda``, ``envelope``, ``entropia`` (plus the special
    cases ``silencio`` and ``curto``). It is the string ``main.py`` receives in
    the gate-drop callback, so it must stay stable and human-readable.
    """

    ok: bool
    band_frac: float
    envelope_s: float
    entropy: float
    reason: str


# Cached Hann windows / band masks / bin frequencies per window length. The
# gate runs on the audio callback: one small FFT per voiced frame, no
# allocation of the transform tables after the first call.
_VOICE_TABLES: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}


def _voice_tables(n: int, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    key = (int(n), int(sample_rate))
    tables = _VOICE_TABLES.get(key)
    if tables is None:
        window = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, 1.0 / float(sample_rate))
        band = (freqs >= _VOICE_BAND_LO_HZ) & (freqs <= _VOICE_BAND_HI_HZ)
        tables = (window, band)
        if len(_VOICE_TABLES) > 8:      # only a handful of sizes are in use
            _VOICE_TABLES.clear()
        _VOICE_TABLES[key] = tables
    return tables


def voice_proof(blocks, sample_rate: int = 16000) -> VoiceProof:
    """Does this audio carry the three proofs of a possible voice?

    ``blocks`` is one int16 array or an iterable of them (the listener hands it
    the last few microphone frames). The measurement is amplitude-independent
    — every test is a ratio — so a quiet, distant voice measures the same as a
    loud one, and the level gate keeps being the segmenter's job.

    Cheap by construction: the analysis window is capped at
    `_VOICE_ANALYSIS_MAX_SAMPLES` (512 ms), the transform tables are cached and
    the envelope is read from a cumsum-free reshape of 8 ms sub-frames.
    """
    if isinstance(blocks, np.ndarray):
        pcm = blocks.ravel()
    else:
        parts = [np.asarray(b).ravel() for b in blocks]
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
    if pcm.size > _VOICE_ANALYSIS_MAX_SAMPLES:
        pcm = pcm[-_VOICE_ANALYSIS_MAX_SAMPLES:]
    if pcm.size < _ENVELOPE_SUBFRAME:
        return VoiceProof(False, 0.0, 0.0, 0.0, "curto")
    x = pcm.astype(np.float32) / 32768.0
    x -= x.mean()
    n = x.size
    window, band = _voice_tables(n, sample_rate)
    spec = np.fft.rfft(x * window)
    power = spec.real * spec.real + spec.imag * spec.imag
    total = float(power[1:].sum())
    if total <= 1e-18:
        return VoiceProof(False, 0.0, 0.0, 0.0, "silencio")
    band_power = power[band]
    band_sum = float(band_power.sum())
    band_frac = band_sum / total
    entropy = 0.0
    if band_sum > 0.0 and band_power.size > 1:
        q = band_power / band_sum
        q = q[q > 0.0]
        entropy = float(-(q * np.log2(q)).sum() / math.log2(band_power.size))
    # Envelope: contiguous 8 ms sub-frames at >= 25% of the window's peak.
    usable = (n // _ENVELOPE_SUBFRAME) * _ENVELOPE_SUBFRAME
    envelope_s = 0.0
    if usable >= _ENVELOPE_SUBFRAME:
        frames = x[:usable].reshape(-1, _ENVELOPE_SUBFRAME)
        levels = np.sqrt(np.mean(frames * frames, axis=1))
        peak = float(levels.max())
        if peak > 0.0:
            best = run = 0
            for active in levels >= peak * _ENVELOPE_REL_FLOOR:
                run = run + 1 if active else 0
                if run > best:
                    best = run
            envelope_s = best * _ENVELOPE_SUBFRAME / float(sample_rate)
    if band_frac < _VOICE_MIN_BAND_FRAC:
        return VoiceProof(False, band_frac, envelope_s, entropy, "banda")
    if envelope_s < _VOICE_MIN_ENVELOPE_S:
        return VoiceProof(False, band_frac, envelope_s, entropy, "envelope")
    if entropy < _VOICE_MIN_ENTROPY:
        return VoiceProof(False, band_frac, envelope_s, entropy, "entropia")
    return VoiceProof(True, band_frac, envelope_s, entropy, "")


class HeardClip(bytes):
    """int16 PCM carrying how it was pre-processed for transcription.

    ``pre_rms`` is the level of the ORIGINAL clip (before any gain) and
    ``boost_gain`` the amplification ``_boost_quiet`` applied. The wake gate
    needs both: a name invented out of a quiet clip that had to be multiplied
    by 7x is not the same evidence as a full-level voice saying it.
    ``speech_s`` is how long the original clip stayed above the frame level a
    nearby voice reaches — the length half of the same proof, so one loud
    thump is not a call. ``voice_proven`` is the transient-gate verdict for
    the segment that produced this clip (`core.groq_stt.voice_proof`): False
    means it only opened through the sustained-energy valve, so main.py can
    log that at the voiceprint instead of taking the clip as a voice. Bytes
    subclasses compare and behave exactly like ``bytes`` everywhere else.
    """

    boost_gain: float = 1.0
    pre_rms: float | None = None
    speech_s: float | None = None
    voice_proven: bool = True

    def __new__(cls, pcm: bytes, *, boost_gain: float = 1.0,
                pre_rms: float | None = None,
                speech_s: float | None = None):
        obj = super().__new__(cls, pcm)
        obj.boost_gain = float(boost_gain)
        obj.pre_rms = None if pre_rms is None else float(pre_rms)
        obj.speech_s = None if speech_s is None else float(speech_s)
        return obj


def _boost_quiet(pcm16: bytes) -> HeardClip:
    """Raise a quiet, distant utterance to a level Whisper can decode.

    Applied only to the clip sent to transcription; the segmenter keeps seeing
    the real room level, so a loud room does not become a loud false positive.
    The returned clip records the original RMS, the gain applied and how many
    seconds of it were loud enough to be a voice, so the wake gate can tell a
    real call from amplified room noise.
    """
    x = np.frombuffer(pcm16, dtype=np.int16)
    if x.size == 0:
        return HeardClip(pcm16, pre_rms=0.0, speech_s=0.0)
    f = x.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(f * f)))
    speech_s = _speech_seconds(f)
    if rms >= 0.03 or rms < 0.004:
        return HeardClip(pcm16, pre_rms=rms, speech_s=speech_s)
    gain = min(_MAX_GAIN, _TARGET_RMS / rms)
    if gain <= 1.2:
        return HeardClip(pcm16, pre_rms=rms, speech_s=speech_s)
    y = f * gain
    # Soft ceiling instead of hard clipping: a loud consonant on top of the
    # boost is scaled down, so the extra input gain never turns into distortion.
    peak = float(np.max(np.abs(y)))
    if peak > 0.95:
        y *= 0.95 / peak
    return HeardClip((y * 32767.0).astype(np.int16).tobytes(),
                     boost_gain=gain, pre_rms=rms, speech_s=speech_s)


def _sum_speech_s(*clips) -> float | None:
    """Total loud seconds among merged pieces (the clip Whisper sees)."""
    values = [getattr(c, "speech_s", None) for c in clips]
    values = [float(v) for v in values if v is not None]
    return sum(values) if values else None


def _min_pre_rms(*clips) -> float | None:
    """Lowest original RMS among the merged pieces (conservative evidence)."""
    values = [getattr(c, "pre_rms", None) for c in clips]
    values = [float(v) for v in values if v is not None]
    return min(values) if values else None


class _ForcedChunk(HeardClip):
    """A chunk closed by the time boundary instead of by a pause.

    Same bytes, one extra fact for the worker: Smart Turn must not merge it
    with the continuation — two 15 s pieces glued back together are the same
    untranscribable blob this boundary exists to prevent.
    """


class _HeldChunk(HeardClip):
    """Speech retained when the microphone was closed mid-sentence.

    It goes to the worker as a *pending* fragment, never straight to the
    brain: the endpointing window waits `_HELD_WAIT_S` for the continuation
    that completes the same phrase, and only then is one transcript produced.
    ``held_head`` survives the merge, so an untranscribable result can be
    announced as "perdi o começo" instead of a generic failure.
    """

    held_head: bool = True


def preroll_from_config(cfg: dict | None) -> float:
    """Seconds of pre-roll, clamped. One key: plugin_config.groq.preroll_s.

    A bad value (text, negative, huge) must never leave the microphone
    unbounded or crash the listener: fall back to the default.
    """
    try:
        value = float((cfg or {}).get("preroll_s", _PREROLL_S))
    except (TypeError, ValueError):
        value = _PREROLL_S
    return min(_MAX_PREROLL_S, max(_MIN_PREROLL_S, value))


def ring_blocks(preroll_s: float) -> int:
    """How many 64 ms frames the ring holds.

    The detector needs `_START_BLOCKS` voiced blocks before it fires, and those
    blocks are part of the ring; the extra room is what keeps the full
    `preroll_s` of honest pre-speech audio even after that detection lag.
    """
    frames = int(math.ceil(max(0.0, float(preroll_s)) / _BLOCK_SECONDS))
    return frames + _START_BLOCKS


def utterance_cap_from_config(cfg: dict | None) -> float:
    """Seconds of voiced audio allowed in one live segment, clamped.

    One key: plugin_config.groq.max_utterance_s. The floor keeps a typo from
    chopping a normal sentence; the ceiling keeps the old 38 s blob from
    coming back through configuration.
    """
    try:
        value = float((cfg or {}).get("max_utterance_s", _UTTERANCE_CAP_S))
    except (TypeError, ValueError):
        value = _UTTERANCE_CAP_S
    return min(_MAX_UTTERANCE_S, max(_MIN_UTTERANCE_S, value))


def settings(get_plugin_config: Callable[[str], dict]) -> dict:
    cfg = get_plugin_config(NAMESPACE)
    model = str(cfg.get("model") or MODELS[0]).strip()
    lang = str(cfg.get("language") or "pt").strip().lower()
    return {
        "api_key": str(cfg.get("api_key") or "").strip(),
        "model": model if model in MODELS else MODELS[0],
        "language": "" if lang in ("auto", "") else lang,
        "preroll_s": preroll_from_config(cfg),
        "max_utterance_s": utterance_cap_from_config(cfg),
    }


def transcribe(pcm16: bytes, api_key: str, model: str, language: str = "pt",
               sample_rate: int = 16000, timeout: float = 20.0,
               prompt: str = "") -> str:
    """One utterance of int16 mono PCM → text. Raises on HTTP/network errors.

    `prompt` is Whisper's vocabulary hint: a short text in the user's
    language that contains the words it should know how to spell — above all
    the assistant's name, which it otherwise renders as Gervis or Charles."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt[:200]
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": ("fala.wav", buf.getvalue(), "audio/wav")},
        data=data, timeout=timeout,
    )
    if r.status_code in (400, 404) and model != MODELS[1]:
        # The turbo model can be renamed or paused; the full model is the
        # fallback Ares uses too.
        return transcribe(pcm16, api_key, MODELS[1], language, sample_rate, timeout, prompt)
    r.raise_for_status()
    return str(r.json().get("text") or "").strip()


def test_connection(values: dict):
    """⚙ TEST CONNECTION: can this key list Groq's models?"""
    key = str(values.get("api_key") or "").strip()
    if not key:
        return False, "Enter a Groq API key (gsk_...) first."
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code == 401:
            return False, "Groq rejected the key (401)."
        r.raise_for_status()
        ids = {m.get("id") for m in r.json().get("data", [])}
        have = [m for m in MODELS if m in ids]
        if not have:
            return True, "Connected, but no Whisper model is listed for this key."
        return True, "Connected. Whisper available: " + ", ".join(have)
    except Exception as e:
        return False, f"Connection failed: {e}"


class GroqListener:
    """Feed it microphone blocks; it calls `on_text(text)` per utterance.

    `feed()` runs on the audio callback thread and only does arithmetic and a
    queue push. Transcription happens on one worker thread, in order, so two
    quick sentences never arrive swapped.
    """

    def __init__(self, api_key: str, model: str, language: str,
                 on_text: Callable[[str], None],
                 on_status: Callable[[str], None] | None = None,
                 sample_rate: int = 16000, prompt: str = "",
                 transcribe_fn: Callable[[bytes], str] | None = None,
                 engine_label: str = "Groq",
                 usage_key: str = "groq",
                 fallback_transcribe: Callable[[bytes], str] | None = None,
                 on_utterance: Callable[[str, bytes], None] | None = None,
                 precheck: Callable[[bytes], bool] | None = None,
                 on_lost: Callable[[str, str, float], None] | None = None,
                 on_gate_drop: Callable[[str, float], None] | None = None,
                 preroll_s: float | None = None,
                 max_utterance_s: float | None = None,
                 use_vad: bool | None = None,
                 on_partial: Callable[[str, float], None] | None = None,
                 on_final: Callable[[str, float], None] | None = None,
                 progressive_interval_s: float | None = None) -> None:
        self._key, self._model, self._lang = api_key, model, language
        self.prompt = prompt
        # Optional local transcriber (core.local_stt): same segmentation and
        # worker thread, no network call. None keeps the Groq HTTP path.
        self._transcribe_fn = transcribe_fn
        # The rung behind a local transcriber: if faster-whisper fails (model
        # gone, a package updated, the machine loaded) the same utterance goes
        # up to Groq instead of being lost. None means "no plan B".
        self._fallback_fn = fallback_transcribe
        self._fallback_used = False
        self.label = engine_label

        self._usage_key = usage_key
        self._on_text = on_text
        # Optional richer sink: receives the text AND the int16 PCM of the same
        # utterance, so the caller can diarize/identify the voice before the
        # turn reaches the brain. When None (the default), behavior is exactly
        # on_text(text) as before — main.py and core.local_stt keep working.
        self._on_utterance = on_utterance
        # Optional veto that runs on the STT worker BEFORE any transcription.
        # It receives the exact int16 PCM of the utterance and returns False to
        # drop it silently (the "modo série" gate: a TV line must cost no paid
        # transcription and must never reach the brain).
        self._precheck = precheck
        # Called when a captured utterance yielded no text at all: the owner's
        # rule is that speech never disappears in silence, so main.py answers
        # it with an activity.log item ("fala não aproveitada") and a short
        # "repete". Signature: (kind, reason, seconds), kind in
        # {"inicio", "audio"} — "inicio" means the retained head of a sentence.
        self._on_lost = on_lost
        # Called when a captured clip is THROWN AWAY because it never passed
        # the voice gate ("não é voz, é um transitório"). Signature
        # (reason, seconds); reason is one of core.groq_stt.VOICE_GATE_REASONS.
        # main.py uses it to log the discard instead of losing the trace, and
        # the same data is available without a callback through
        # `voice_gate_stats()`.
        self._on_gate_drop = on_gate_drop
        self._on_status = on_status or (lambda m: None)
        self._sr = sample_rate
        self._q: queue.Queue = queue.Queue()
        self._floor = 0.02
        self._active = False
        self._above = 0
        self._below = 0
        self._voiced = 0
        # ── voice gate state (see voice_proof above) ────────────────────────
        # Rolling window of the last candidate blocks, the count of blocks
        # that passed inside the current run/segment, whether the open segment
        # ever proved voice, and the public counters main.py reads.
        self._proof_buf: collections.deque[np.ndarray] = collections.deque(
            maxlen=_VOICE_WINDOW_BLOCKS)
        self._above_voice = 0
        self._seg_voice = 0
        self._voice_proven = False
        self._bypass_open = False
        self._last_proof: VoiceProof | None = None
        self._last_gate_drop: dict | None = None
        self._gate_stats: dict = {
            "blocks": 0,           # candidate blocks measured by the gate
            "voice": 0,            # ...that passed all three proofs
            "rejected": 0,         # ...that did not
            "by_reason": {reason: 0 for reason in VOICE_GATE_REASONS},
            "turns_opened": 0,
            "turns_opened_by_bypass": 0,   # sustained energy, no proof
            "clips_dropped": 0,            # emitted clips refused by the gate
        }
        # The pre-roll ring. It is written on EVERY block that reaches feed()
        # and discards the oldest frame by itself; the microphone callback
        # keeps feeding it while asleep (and during our own playback), so the
        # 1.5 s before the first phoneme is never lost. Bounded by construction.
        self._preroll_s = preroll_from_config({"preroll_s": preroll_s}) \
            if preroll_s is not None else _PREROLL_S
        self._ring_blocks = ring_blocks(self._preroll_s)
        self._pre: collections.deque[bytes] = collections.deque(
            maxlen=self._ring_blocks)
        self._seg: list[bytes] = []
        # Forced boundary for a live segment, in voiced 64 ms blocks. Voiced,
        # not elapsed: the pre-roll silence and the pauses inside a sentence do
        # not count against the owner, a TV that never stops talking does.
        self._utterance_s = utterance_cap_from_config(
            {"max_utterance_s": max_utterance_s}) \
            if max_utterance_s is not None else _UTTERANCE_CAP_S
        self._owner_cap_blocks = int(round(self._utterance_s / _BLOCK_SECONDS))
        self._non_owner_cap_blocks = int(round(
            min(self._utterance_s, _NON_OWNER_CAP_S) / _BLOCK_SECONDS))
        # Optional speaker awareness, set from outside ONCE (main.py): a
        # callable returning True while the voice on the microphone is the
        # owner's. Default None = owner-agnostic, only the time cap applies.
        # It runs on the audio callback thread: keep it cheap, no I/O.
        self.current_speaker_is_owner: Callable[[], bool] | None = None
        self._cap_hook_warned = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="groq-stt", daemon=True)
        self._worker.start()
        self.speaking = False          # True while an utterance is open
        self.assistant_speaking = False  # True while a reply is playing
        self._drop_segment = False
        # True while the assistant sleeps: utterances are still cut and
        # transcribed (locally only) so a sentence that calls the assistant by
        # name can wake it — but nothing leaves the machine, nothing reaches
        # the panel and the speakers are not ducked for room noise.
        self.sleeping = False
        # Silence (in blocks) that closes an utterance. Guided enrollment
        # raises it so a phrase is only delivered after ~1.8 s of real
        # end-of-speech, not on the first pause between words.
        self._end_blocks = _END_BLOCKS
        self._peak = 0.0
        self._peak_t = time.monotonic()
        # When the last microphone block arrived. A segment can only be closed
        # BY a block (700 ms of quiet), so "no block for a while" is the one
        # proof that an open segment will never close on its own.
        self._last_feed_at = time.monotonic()
        # Speech kept from a segment the closed microphone could not finish
        # (see hold_open). Emptied exactly once, on the first block after the
        # reopen, so the same audio can never reach the STT twice.
        self._held_pcm: bytes = b""
        self._held_voiced = 0
        # Did the retained segment pass the voice gate? True unless it opened
        # through the sustained-energy valve.
        self._held_proven = True
        # Score do "fim de turno" da última pausa medida; alimenta a cauda
        # adaptativa (`continue_window_for`). None = ainda não medido.
        self._endpoint_probability: float | None = None
        # ── Silero VAD: detecção de fala por modelo, não só por RMS ─────────
        # When the ONNX model is present, is_speech() replaces the RMS threshold
        # for start/end-of-utterance decisions.  When absent, the existing
        # level > thresh logic is the fallback — same behaviour as before.
        # `use_vad=False` forces the RMS path (for tests with synthetic audio).
        self._vad: silero_vad.SileroVAD | None = None
        self._vad_available = False
        if use_vad is not False and silero_vad.available():
            try:
                self._vad = silero_vad.SileroVAD()
                self._vad_available = True
            except Exception as _vad_err:
                print(f"[{engine_label}] Silero VAD indisponível "
                      f"({_vad_err.__class__.__name__}) — usando RMS puro")
        # ── Transcrição incremental ao vivo (UP-3) ───────────────────────────
        # A `core.stt_streaming.StreamingSession` opens per utterance and
        # receives the SAME blocks the segmenter receives — on a dedicated
        # thread, never on the 64 ms audio callback.  While the sentence is
        # still open it transcribes progressive windows and reports them
        # through `on_partial(text, ms_since_open)`; the definitive result
        # keeps going through the old path (worker + endpointing) and is
        # announced through `on_final(text, ms_since_open)`.  The session is
        # never finalized with its own transcription: that would run the
        # engine twice over the same audio.  Everything here is best-effort:
        # without `on_partial`, with JARVIS_STT_PARTIAL=0, or on any failure
        # the live path stays exactly what it was before this hook.
        self._on_partial_stream = on_partial
        self._on_final_stream = on_final
        self._partial_enabled = (on_partial is not None
                                 and os.environ.get(
                                     "JARVIS_STT_PARTIAL", "1") != "0")
        try:
            _interval = float(progressive_interval_s
                              if progressive_interval_s is not None
                              else stt_streaming.DEFAULT_PROGRESSIVE_INTERVAL_S)
        except (TypeError, ValueError):
            _interval = stt_streaming.DEFAULT_PROGRESSIVE_INTERVAL_S
        self._partial_interval_s = min(
            stt_streaming.MAX_PROGRESSIVE_INTERVAL_S,
            max(stt_streaming.MIN_PROGRESSIVE_INTERVAL_S, _interval))
        self._partial_q: queue.Queue = queue.Queue()
        self._partial_thread: threading.Thread | None = None
        self._partial_dead = False
        # When the CURRENT utterance opened: the ms_since_open clock of both
        # partial and final events.
        self._segment_opened_at = 0.0
        # Set while the worker is transcribing (or has a clip waiting): the
        # partial steps aside instead of stealing the engine from the final.
        self._transcribing = threading.Event()

    @property
    def local(self) -> bool:
        """True when transcription happens on this machine (core.local_stt)."""
        return self._transcribe_fn is not None

    @property
    def preroll_seconds(self) -> float:
        """Configured pre-roll, seconds before the first voiced frame."""
        return self._preroll_s

    @property
    def preroll_blocks(self) -> int:
        """Ring capacity in frames; also the hard ceiling tests assert on."""
        return self._ring_blocks

    @property
    def preroll_bytes(self) -> int:
        """Upper bound of the ring in bytes (one channel, int16)."""
        return self._ring_blocks * int(round(_BLOCK_SECONDS * self._sr)) * 2

    @property
    def utterance_seconds(self) -> float:
        """Voiced seconds allowed in one live segment before a forced cut."""
        return self._utterance_s

    def set_enrollment(self, enabled: bool) -> None:
        """Guided enrollment: wait ~1.8 s of silence before closing a phrase.

        The normal 700 ms closes on the pause between words; during the five
        prompted phrases that turned half a sentence into an "attempt" and a
        ghost into a failure. Only the segmenter's end-of-speech window
        changes — VAD, thresholds and devices stay untouched.
        """
        self._end_blocks = _ENROLL_END_BLOCKS if enabled else _END_BLOCKS

    def set_assistant_speaking(self, value: bool) -> None:
        """Called by the playback path: a reply is (not) coming out now."""
        value = bool(value)
        if value == self.assistant_speaking:
            # The playback loop calls this once per ~200 ms batch. Re-arming
            # the pending drop on every call wiped whatever the owner had
            # started to say during the reply: the segment could never grow
            # into an utterance, so voice barge-in was effectively deaf.
            # Only the START of a reply invalidates a half-open segment.
            return
        self.assistant_speaking = value
        if value:
            # Drop any stale segment on the next mic block instead of touching
            # the segment list from the playback thread. A reply only starts
            # after a turn, but no mixed-voice segment may survive into it.
            self._drop_segment = True

    def mic_idle_seconds(self) -> float:
        """Seconds since the last microphone block reached `feed()`."""
        return max(0.0, time.monotonic() - self._last_feed_at)

    def speaking_is_stale(self,
                          max_idle_s: float = _STALE_SPEAKING_S) -> bool:
        """An open segment that has received no audio for `max_idle_s`.

        This is the proof that it can never close by itself: only `feed()`
        can close an utterance, and nothing is being fed.
        """
        if not self.speaking:
            return False
        return self.mic_idle_seconds() > float(max_idle_s)

    def discard_open(self) -> bool:
        """Throw away a half-open segment WITHOUT transcribing it.

        The no-retention variant: used when nothing may become a turn even
        later — the owner himself silenced the microphone while asleep (there
        the ears are the wake word's, and a stale fragment must not wake her).
        Everything the owner says with the mic OPEN goes through `hold_open`
        instead. Nothing is emitted here: no ghost transcription, no barge-in,
        no status line. The room volumes come back the same way an ordinary
        end of utterance releases them.

        Runs on the microphone thread (same as `feed()`), which is what makes
        it race-free; the only other caller is `main.py`'s stale check, which
        fires after `_STALE_SPEAKING_S` with no block at all — the one moment
        `feed()` is not running — and a lost race there costs one restarted
        utterance, never mixed audio.
        """
        if not (self._active or self.speaking or self._seg):
            return False
        self._seg, self._active, self.speaking = [], False, False
        self._above = self._below = self._voiced = 0
        self._reset_voice_state()
        self._partial_end()
        # The ring holds audio from before this instant. After a mute it is
        # stale, and prepending it to the next sentence would paste words said
        # with the microphone closed onto the front of it.
        self._pre.clear()
        if not self.assistant_speaking:
            _duck.up()
        return True

    def hold_open(self) -> dict:
        """Keep the audio of a segment the closed microphone cannot finish.

        Returns ``{"held": bool, "seconds": float, "voiced": int,
        "lost": bool}``. The audio becomes the pending HEAD of the next
        utterance: it is handed to the worker on the first block after the
        reopen (see `feed`) and merged with the continuation by the same
        endpointing window every pause already uses. Nothing is transcribed
        here, nothing reaches the brain now, and no barge-in is raised — the
        owner may still be in the middle of the sentence.

        A segment with too little voice to ever be transcribed is not kept:
        it becomes an explicit "fala não aproveitada" through ``on_lost``, so
        the owner is asked to repeat instead of being met with silence.
        """
        if not (self._active or self.speaking or self._seg):
            return {"held": False, "seconds": 0.0, "voiced": 0, "lost": False}
        seg, voiced = list(self._seg), self._voiced
        self._seg, self._active, self.speaking = [], False, False
        self._above = self._below = self._voiced = 0
        self._reset_voice_state()
        self._partial_end()
        self._pre.clear()
        if not self.assistant_speaking:
            _duck.up()
        if not seg:
            return {"held": False, "seconds": 0.0, "voiced": 0, "lost": False}
        pcm = b"".join(seg)
        secs = len(pcm) / (2 * self._sr)
        if voiced < _HELD_MIN_VOICED or secs < _HELD_MIN_SECONDS:
            print(f"[{self.label}] segmento aberto irrecuperável "
                  f"({voiced} quadros de voz, {secs:.2f}s) — nada transcrevível")
            self._lost("inicio", "curto demais para transcrever", secs)
            return {"held": False, "seconds": secs, "voiced": voiced,
                    "lost": True}
        self._held_pcm = pcm
        self._held_voiced = voiced
        self._held_proven = bool(self._voice_proven)
        return {"held": True, "seconds": secs, "voiced": voiced, "lost": False}

    @property
    def held_seconds(self) -> float:
        """Seconds of speech retained for the next microphone opening."""
        return len(self._held_pcm) / (2 * self._sr) if self._held_pcm else 0.0

    def has_held_speech(self) -> bool:
        """True while a retained segment waits for the microphone to reopen."""
        return bool(self._held_pcm)

    def _lost(self, kind: str, reason: str, seconds: float) -> None:
        """Report a captured-but-untranscribable utterance (never silence)."""
        if self._on_lost is None:
            return
        try:
            self._on_lost(str(kind), str(reason), float(seconds))
        except Exception as e:
            print(f"[{self.label}] aviso de fala perdida falhou ({e!r})")

    def _audible_voice(self, pcm) -> bool:
        """Was there enough voice here for "nothing heard" to be a loss?

        The segmenter already required voiced blocks to emit the clip, so this
        is the second piece of evidence — a clip that only held a door, a fan
        or a blip is not the owner's speech and must not become a "repete".
        A quiet, distant microphone (the 02/11 captures) still qualifies: its
        original RMS clears `_THRESH_MIN` even when no frame reaches the loud
        voice level.
        """
        secs = len(pcm) / (2 * self._sr)
        if secs < _LOST_MIN_SECONDS:
            return False
        speech_s = getattr(pcm, "speech_s", None)
        if speech_s is not None and float(speech_s) >= _LOST_MIN_SPEECH_S:
            return True
        pre_rms = getattr(pcm, "pre_rms", None)
        return pre_rms is not None and float(pre_rms) >= _THRESH_MIN

    def _release_held(self) -> None:
        """Hand the retained head to the worker as pending, exactly once."""
        pcm, voiced = self._held_pcm, self._held_voiced
        proven = self._held_proven
        self._held_pcm, self._held_voiced = b"", 0
        self._held_proven = True
        if not pcm:
            return
        secs = len(pcm) / (2 * self._sr)
        if voiced < _HELD_MIN_VOICED or secs < _HELD_MIN_SECONDS:
            print(f"[{self.label}] fala retida irrecuperável "
                  f"({voiced} quadros de voz, {secs:.2f}s)")
            self._lost("inicio", "curto demais para transcrever", secs)
            return
        payload = _boost_quiet(pcm)
        chunk = _HeldChunk(
            payload,
            boost_gain=getattr(payload, "boost_gain", 1.0),
            pre_rms=getattr(payload, "pre_rms", None),
            speech_s=getattr(payload, "speech_s", None))
        chunk.voice_proven = bool(proven)
        self._q.put(chunk)
        print(f"[{self.label}] fala retida durante o mute devolvida ao STT "
              f"({secs:.1f}s) — o agregador decide se junta com a continuação")

    # ── audio thread ────────────────────────────────────────────────────────

    def feed(self, block, preroll_only: bool = False) -> None:
        x = np.asarray(block, dtype=np.float32).ravel()
        if x.size == 0:
            return
        self._last_feed_at = time.monotonic()
        # ── RNNoise pre-filter (FASE2-TASK1) ─────────────────────────────
        # Apply noise reduction to each 64 ms block BEFORE the VAD/RMS
        # threshold and before storing in the ring buffer.  When the filter
        # is unavailable, ``filter_block`` is a transparent passthrough.
        raw_unfiltered = np.asarray(block, dtype=np.int16).tobytes()
        raw = _noise.filter_block(raw_unfiltered, self._sr) if _noise.available() else raw_unfiltered
        # Record FIRST, before any gate below can return: the ring is the only
        # copy of the audio that precedes a detection, and it must keep running
        # while the stream is open (asleep, echo tail, our own playback).
        self._pre.append(raw)
        if preroll_only:
            # Only the ring — used while the assistant's own voice is in the
            # monitor, so the segmenter does not open on our echo, but the
            # owner's first phoneme still survives for a barge-in. A retained
            # head is NOT released here: our own echo must never be what wakes
            # the owner's words up.
            return
        if self._held_pcm:
            # The microphone is open again with real (non-echo) audio: that is
            # the reopen, and the retained speech goes back into the worker
            # where the endpointing window joins it with this same phrase.
            self._release_held()
        # Level is computed from the FILTERED audio so the RMS threshold
        # benefits from the noise suppression (FASE2-TASK1).
        x_filtered = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        level = float(np.sqrt(np.mean(x_filtered * x_filtered))) / 32768.0

        if self._drop_segment:
            # The reply started: whatever was half-open belongs to the previous
            # turn, not to a mixed utterance with the assistant's own voice.
            self._seg, self._active, self.speaking = [], False, False
            self._above = self._below = self._voiced = 0
            self._reset_voice_state()
            self._drop_segment = False
            # The partial session of that discarded segment must die with it.
            self._partial_end()

        if not self._active:
            # Track the room while nobody speaks; follow drops quickly, rises slowly.
            if level < self._floor:
                self._floor = 0.90 * self._floor + 0.10 * level
            else:
                self._floor = 0.995 * self._floor + 0.005 * level
            self._floor = max(_FLOOR_MIN, self._floor)
        thresh = max(_THRESH_MIN, self._floor * _FLOOR_MULT)

        # A level line every few seconds on the console: the one number that
        # says whether the microphone is even reaching us, and how it compares
        # to the threshold, so sensitivity can be tuned from a log instead of
        # a guess.
        self._peak = max(self._peak, level)
        now = time.monotonic()
        if now - self._peak_t >= _LEVEL_LOG_S:
            print(f"[Groq] mic peak {self._peak:.4f}  floor {self._floor:.4f}  "
                  f"threshold {thresh:.4f}  {'SPEECH' if self._active else 'quiet'}")
            self._peak, self._peak_t = 0.0, now

        if not self._active:
            # Determine if this block contains speech: Silero VAD when the
            # model is loaded, RMS threshold as fallback.
            if self._vad_available:
                _speech = self._vad.is_speech(raw)
            else:
                _speech = level > thresh
            if _speech:
                self._above += 1
                # Energy is not voice. Every candidate frame is measured by the
                # transient gate; only frames that pass count towards opening
                # the turn, so a key/click/impact cannot manufacture one.
                # With VAD the gate still runs for the voice_proven flag, but
                # the START decision uses VAD-positive blocks directly.
                if self._probe_voice(raw).ok:
                    self._above_voice += 1
                if self._vad_available:
                    # VAD replaces both RMS and voice-proof for START: 3
                    # consecutive VAD-positive blocks open the turn.
                    if self._above >= _START_BLOCKS:
                        self._open_segment()
                else:
                    # Fallback: original RMS + voice-proof logic.
                    if (self._above_voice >= _START_BLOCKS
                            or self._above >= _VOICE_BYPASS_BLOCKS):
                        self._open_segment()
            else:
                self._above = 0
                self._above_voice = 0
                # A real pause ends the proof window: a click after silence
                # must not borrow the spectrum of the sentence before it.
                self._proof_buf.clear()
                # Reset VAD state on silence so the next utterance starts clean.
                if self._vad is not None:
                    self._vad.reset()
            return

        self._seg.append(raw)
        # Determine if this block contains speech: Silero VAD when the model
        # is loaded, RMS threshold as fallback.  This is the same criterion
        # used for START — the segment closes when VAD (or RMS) says "no
        # speech" for `_end_blocks` consecutive blocks.
        if self._vad_available:
            _speech = self._vad.is_speech(raw)
        else:
            _speech = level > thresh
        if _speech:
            self._voiced += 1
            self._below = 0
            if self._probe_voice(raw).ok:
                self._seg_voice += 1
                if self._seg_voice >= _EMIT_MIN_VOICE_BLOCKS:
                    self._voice_proven = True
            # The duck opened with this utterance; a block with actual voice
            # in it is what says the owner is speaking RIGHT NOW (a float
            # assignment — never a dropout). Silence inside an open segment
            # and the assistant's own tail must not refill the beat: the
            # watchdog in core/duck.py gives the volumes back when nothing is
            # voiced, and _close() releases them the moment the sentence ends.
            _duck.heartbeat()
        else:
            self._below += 1
            self._proof_buf.clear()
        # UP-3: the same block also feeds the live partial session (only
        # queued here — transcription runs on the partial thread, never on
        # this callback). Asleep, the ear is the wake word's: no partials.
        if self._partial_enabled and not self.sleeping:
            self._partial_enqueue(("block", raw, bool(_speech)))
        if self.assistant_speaking:
            cap = _SPEAK_CAP_BLOCKS
        else:
            cap = _SLEEP_CAP_BLOCKS if self.sleeping else _MAX_BLOCKS
        if self._below >= self._end_blocks or len(self._seg) >= cap:
            self._close(capped=len(self._seg) >= cap)
            return
        if not self.assistant_speaking and not self.sleeping:
            # Nobody paused: hand over a transcribable chunk instead of letting
            # the utterance run to the 38 s hard cap (see _cut_segment).
            voiced_cap = self._soft_cap_blocks()
            if self._voiced >= voiced_cap:
                self._cut_segment(voiced_cap)

    # ── voice gate ──────────────────────────────────────────────────────────

    def _probe_voice(self, raw: bytes) -> VoiceProof:
        """Measure the transient gate on the last few candidate blocks."""
        self._proof_buf.append(np.frombuffer(raw, dtype=np.int16))
        proof = voice_proof(self._proof_buf, self._sr)
        self._last_proof = proof
        stats = self._gate_stats
        stats["blocks"] += 1
        if proof.ok:
            stats["voice"] += 1
        else:
            stats["rejected"] += 1
            by_reason = stats["by_reason"]
            by_reason[proof.reason] = by_reason.get(proof.reason, 0) + 1
        return proof

    def _reset_voice_state(self) -> None:
        """Forget voice proof of a segment that is being dropped or closed."""
        self._above_voice = 0
        self._seg_voice = 0
        self._voice_proven = False
        self._bypass_open = False
        self._proof_buf.clear()

    def _open_segment(self) -> None:
        """Open an utterance on the current block (proof already counted)."""
        self._active = True
        self.speaking = True
        # The ring already holds the full pre-roll plus the blocks the detector
        # needed to fire: hand the whole thing to the utterance, then start a
        # fresh ring for the continuation.
        self._seg = list(self._pre)
        self._pre.clear()
        self._voiced = self._above
        self._seg_voice = self._above_voice
        self._below = 0
        self._voice_proven = self._above_voice >= _START_BLOCKS
        self._bypass_open = not self._voice_proven
        stats = self._gate_stats
        stats["turns_opened"] += 1
        if self._bypass_open:
            # Safety valve: sustained energy that never passed the proof is
            # not a transient (see _VOICE_BYPASS_BLOCKS). Never silent.
            stats["turns_opened_by_bypass"] += 1
            print(f"[{self.label}] energia sustentada por "
                  f"{self._above * _BLOCK_SECONDS:.2f}s sem prova de voz — "
                  f"abrindo o turno mesmo assim")
        # UP-3: open the live partial session of THIS utterance. It is primed
        # with the whole segment as it stands — the ring's 1.5 s of preroll
        # plus the blocks that fired the detector — so the first progressive
        # window already carries the beginning of the phrase.
        self._segment_opened_at = time.monotonic()
        if self._partial_enabled and not self.sleeping:
            self._partial_start()
            self._partial_drain()
            self._partial_enqueue(("open", self._segment_opened_at,
                                   list(self._seg)))
        # The room includes the speakers: while a sentence is open the music
        # drops, and comes back when it closes — without this, Whisper
        # transcribes the lyrics along with the user. Asleep, every cough would
        # pump the volume: don't.
        if not self.sleeping:
            _duck.down()

    def voice_gate_stats(self) -> dict:
        """Public snapshot of the transient gate, for main.py to log.

        Returns counters plus the last measurement and the last refused clip::

            {"blocks", "voice", "rejected", "by_reason": {...},
             "turns_opened", "turns_opened_by_bypass", "clips_dropped",
             "last_proof": {"ok", "reason", "band_frac", "envelope_s",
                            "entropy"} | None,
             "last_drop": {"reason", "seconds", "at"} | None}

        Reasons are the strings in `VOICE_GATE_REASONS`; a delta of
        ``clips_dropped`` between two calls is exactly the number of clips that
        would otherwise have reached the voiceprint.
        """
        stats = {key: (dict(value) if isinstance(value, dict) else value)
                 for key, value in self._gate_stats.items()}
        proof = self._last_proof
        stats["last_proof"] = None if proof is None else {
            "ok": proof.ok,
            "reason": proof.reason,
            "band_frac": round(proof.band_frac, 3),
            "envelope_s": round(proof.envelope_s, 3),
            "entropy": round(proof.entropy, 3),
        }
        stats["last_drop"] = (None if self._last_gate_drop is None
                              else dict(self._last_gate_drop))
        return stats

    def _note_gate_drop(self, reason: str, seconds: float) -> None:
        """Record (and announce) a clip refused for having no voice proof."""
        self._gate_stats["clips_dropped"] += 1
        self._last_gate_drop = {"reason": str(reason),
                                "seconds": float(seconds),
                                "at": time.monotonic()}
        print(f"[{self.label}] clipe descartado sem prova de voz "
              f"({reason}, {seconds:.2f}s) — nada vai ao STT nem ao voiceprint")
        if self._on_gate_drop is not None:
            try:
                self._on_gate_drop(str(reason), float(seconds))
            except Exception as e:
                print(f"[{self.label}] aviso de descarte falhou ({e!r})")

    def _soft_cap_blocks(self) -> int:
        """Voiced blocks this live segment may hold before a forced boundary.

        `current_speaker_is_owner` is the injected hook (main.py may set it):
        when it says the voice is NOT the owner's, the cap tightens from the
        configured `max_utterance_s` (15 s) to `_NON_OWNER_CAP_S` (8 s). The
        hook is only asked once the segment is already long enough to matter,
        so short turns never pay for it.

        A hook that raises fails OPEN: the owner/agnostic cap stays, which
        costs longer chunks at worst — never a chopped owner sentence.
        """
        if self.current_speaker_is_owner is None:
            return self._owner_cap_blocks
        if self._voiced < self._non_owner_cap_blocks:
            return self._owner_cap_blocks
        try:
            is_owner = bool(self.current_speaker_is_owner())
        except Exception as e:
            if not self._cap_hook_warned:
                self._cap_hook_warned = True
                print(f"[{self.label}] hook de locutor falhou ({e!r}) — "
                      f"valendo só o teto de tempo")
            return self._owner_cap_blocks
        return self._owner_cap_blocks if is_owner else self._non_owner_cap_blocks

    def _cut_segment(self, cap_blocks: int) -> None:
        """Soft boundary: hand the captured audio over and keep listening.

        Unlike `_close()`, the utterance stays OPEN — VAD, duck, `_below` and
        `speaking` are untouched — and the next block simply starts a fresh
        segment. That is what guarantees no frame is dropped, repeated or
        reordered at the boundary: every block fed belongs to exactly one
        emitted chunk, in the order it arrived.
        """
        seg, voiced = self._seg, self._voiced
        proven, bypass = self._voice_proven, self._bypass_open
        self._seg, self._voiced = [], 0
        # The utterance stays OPEN: its voice proof belongs to the whole
        # utterance, not to this chunk. Only the per-chunk counter restarts.
        self._seg_voice = 0
        print(f"[{self.label}] fala contínua de {len(seg) * _BLOCK_SECONDS:.1f}s — "
              f"cortando em trechos transcrevíveis (teto de "
              f"{cap_blocks * _BLOCK_SECONDS:.0f}s de voz)")
        self._emit(seg, voiced, forced=True, proven=proven, bypass=bypass)
        # UP-3: the partial session restarts with the new chunk — a monologue
        # is transcribed progressively in pieces, never over one growing blob.
        self._partial_enqueue(("end",))
        if self._partial_enabled and not self.sleeping:
            self._segment_opened_at = time.monotonic()
            self._partial_drain()
            self._partial_enqueue(("open", self._segment_opened_at, []))

    def _close(self, capped: bool = False) -> None:
        seg, voiced = self._seg, self._voiced
        proven, bypass = self._voice_proven, self._bypass_open
        self._seg, self._active, self.speaking = [], False, False
        self._above = self._below = self._voiced = 0
        self._reset_voice_state()
        # The closed utterance is already going to the STT: its frames must not
        # become the pre-roll of the next sentence (that would duplicate words).
        self._pre.clear()
        # Cut by time while asleep: the tail becomes the opening of the next
        # window, so "Gino" ending at the boundary is still heard once.
        if capped and self.sleeping and len(seg) > self._ring_blocks:
            self._pre.extend(seg[-self._ring_blocks:])
        # Owner's spec (31/10): the volumes come back when the SPEECH stops,
        # never waiting on the reply or on background jobs. If a reply is
        # already playing the assistant is still talking — its playback keeps
        # the duck alive (each batch re-asserts the beat) and the drain
        # release in main.py closes the exchange when the speakers go quiet.
        # The next sentence starting to play ducks again from fresh originals.
        if not self.assistant_speaking:
            _duck.up()
        if capped and not self.sleeping:
            # A long answer is transcribed, but the tail may be missing. Say so
            # on the console: the user's request was cut mid-sentence once and
            # the model answered the fragment as if it were complete.
            print(f"[{self.label}] utterance hit the {_MAX_BLOCKS}-block cap — "
                  f"transcribing the captured part only")
        self._emit(seg, voiced, proven=proven, bypass=bypass)
        # UP-3: the partial session of this utterance is over. It is dropped,
        # NOT finalized: the definitive transcription is the worker's job and
        # running the engine twice on the same audio would only make the turn
        # slower. The `stt_final` event comes from `_process_utterance`.
        self._partial_end()

    def _emit(self, seg: list[bytes], voiced: int,
              forced: bool = False, proven: bool = False,
              bypass: bool = False) -> None:
        """Send one finished chunk to the STT worker, or drop it as noise.

        ``proven`` says the segment this chunk came from carried voice proof
        from the transient gate (`voice_proof`). Without it the clip is NOT
        emitted: it would reach the transcription AND the voiceprint in main.py
        (``analyze_utterance``) as if a voice had produced it. That was the
        second defect — a click with enough energy to cross the VAD opened a
        segment and its PCM still arrived at the speaker identification. The
        discard is counted and announced (`voice_gate_stats`), never silent.

        ``bypass`` is the sustained-energy valve: the clip IS emitted (throwing
        away 1.3 s of possible speech would be a silent loss) but it travels
        with ``voice_proven=False`` so main.py can log that at the voiceprint.
        """
        if voiced >= _MIN_SPEECH and not (proven or bypass):
            pcm = b"".join(seg)
            secs = len(pcm) / (2 * self._sr)
            last = self._last_proof
            reason = last.reason if (last is not None and not last.ok) else "sem_prova"
            self._note_gate_drop(reason, secs)
            if not self.assistant_speaking:
                _duck.up()      # the segment opened an utterance (and ducked)
            return
        if voiced >= _MIN_SPEECH:
            pcm = b"".join(seg)
            secs = len(pcm) / (2 * self._sr)
            if self.sleeping and secs > _SLEEP_MAX_S:
                # Asleep, a wake call is a short sentence; eight seconds of
                # "speech" is the song or the TV, and transcribing it would burn
                # CPU to learn that nobody called her.
                print(f"[Groq] asleep — ignored a {secs:.0f}s utterance "
                      f"(wake calls are short)")
            else:
                if not self.sleeping:
                    self._on_status(f"Ouvi {secs:.1f}s — transcrevendo no {self.label}…")
                payload = _boost_quiet(pcm)
                # Per-clip proof, for main.py's voiceprint logging: a clip
                # from the sustained-energy valve says so itself.
                payload.voice_proven = bool(proven)
                if forced:
                    payload = _ForcedChunk(
                        payload,
                        boost_gain=getattr(payload, "boost_gain", 1.0),
                        pre_rms=getattr(payload, "pre_rms", None),
                        speech_s=getattr(payload, "speech_s", None))
                    payload.voice_proven = bool(proven)
                self._q.put(payload)
        else:
            print(f"[Groq] dropped a {voiced}-block blip as noise")
            _duck.up()          # it opened an utterance (and ducked) for nothing

    def _is_prompt_echo(self, text: str) -> bool:
        """True when the transcription is really a piece of the vocabulary hint.

        Compared fragment by fragment: the hint is a paragraph, so a whole-text
        ratio would dilute a single regurgitated sentence below any threshold.

        This runs asleep too. It used to be disabled there because the hint
        was built from wake phrases ("<name>, tá aí?") — an honest call was
        character-for-character a fragment of the prompt and got dropped. The
        hint now carries the name ONCE (main._stt_name_hint), so a short
        fragment cannot match it; what still matches is a long sentence
        regurgitated from the hint, which is exactly the false wake.
        """
        import difflib
        import re as _re
        hint = (self.prompt or "").lower()
        if not hint or len(text) < 8:
            return False
        norm = " ".join(text.lower().split())
        for frag in _re.split(r"[.?!]+", hint):
            frag = " ".join(frag.split())
            if len(frag) < 8:
                continue
            if difflib.SequenceMatcher(None, norm, frag).ratio() > 0.8:
                return True
        return False

    def flush(self) -> None:
        """Push-to-talk released: whatever is open is an utterance."""
        if self._active:
            self._close()

    def transcribe_pcm(self, pcm: bytes) -> str:
        """Transcribe one PCM slice without segmentation or echo filtering.

        Used by full diarization to re-transcribe each speaker segment. It runs
        on the same worker thread that called ``on_utterance``, so it never
        races the listener's own model.
        """
        try:
            if self._transcribe_fn is not None:
                return str(self._transcribe_fn(pcm) or "").strip()
            if self._key:
                return str(transcribe(pcm, self._key, self._model, self._lang,
                                      self._sr, prompt=self.prompt) or "").strip()
            if self._fallback_fn is not None:
                return str(self._fallback_fn(pcm) or "").strip()
        except Exception as e:
            print(f"[{self.label}] segment transcription failed: {e}")
        return ""

    # ── transcrição incremental ao vivo (UP-3) ──────────────────────────────

    def _partial_start(self) -> None:
        """Start the dedicated partial thread on the first partial utterance.

        Lazily, so a listener nobody wired to `on_partial` pays nothing; if
        the thread cannot start (a machine without threads to spare) the
        partial path dies quietly and the old path keeps answering turns.
        """
        if self._partial_thread is not None or self._partial_dead:
            return
        try:
            thread = threading.Thread(target=self._run_partial,
                                      name="stt-partial", daemon=True)
            thread.start()
            self._partial_thread = thread
        except Exception as e:
            self._partial_dead = True
            print(f"[{self.label}] streaming parcial indisponível ({e!r}) — "
                  f"só o caminho antigo de fim de frase daqui em diante")

    def _partial_enqueue(self, item: tuple) -> None:
        """Best-effort: queue one open/block/end token for the partial thread.

        Runs on the audio callback thread; `put_nowait` on an unbounded queue
        is a plain list append — no engine, no I/O, no lock worth mentioning.
        Any failure kills the partial path alone (`_partial_dead`), never the
        turn: the old end-of-utterance transcription does not depend on it.
        """
        if not self._partial_enabled or self._partial_dead:
            return
        try:
            self._partial_q.put_nowait(item)
        except Exception as e:
            self._partial_dead = True
            print(f"[{self.label}] fila parcial indisponível ({e!r}) — "
                  f"só o caminho antigo de fim de frase daqui em diante")

    def _partial_end(self) -> None:
        """Retire the partial session of the segment that just ended/died."""
        self._partial_enqueue(("end",))

    def _partial_drain(self) -> None:
        """Forget tokens of a previous utterance if the partial thread lags."""
        try:
            while True:
                self._partial_q.get_nowait()
        except queue.Empty:
            pass

    def _run_partial(self) -> None:
        """The dedicated partial thread: owns the current StreamingSession.

        One session per utterance, created on the "open" token and fed with
        every subsequent block.  Transcription calls happen HERE, off the
        audio callback, and are skipped whenever the real worker has a clip
        waiting (`_partial_transcribe`) — the partial never steals the engine
        from the final result.  A dying session costs one log line: the turn
        itself is still produced by the old path.
        """
        session: stt_streaming.StreamingSession | None = None
        while not self._stop.is_set():
            try:
                item = self._partial_q.get(timeout=0.25)
            except queue.Empty:
                continue
            kind = item[0]
            try:
                if kind == "open":
                    _, started_at, blocks = item
                    session = stt_streaming.StreamingSession(
                        transcribe_fn=self._partial_transcribe,
                        on_partial=lambda text, is_final: (
                            None if (is_final or not text)
                            else self._note_partial(text, started_at)),
                        # The preroll already lives in the segment (the
                        # listener's own ring primed this session), so the
                        # session must not keep a second one.
                        preroll_s=0.0,
                        progressive_interval_s=self._partial_interval_s,
                        sample_rate=self._sr)
                    for blk in blocks:
                        session.feed_pcm16(blk, is_speech=True)
                elif kind == "block":
                    if session is not None:
                        session.feed_pcm16(item[1], is_speech=bool(item[2]))
                else:  # "end"
                    session = None
            except Exception as e:
                print(f"[{self.label}] transcrição parcial falhou "
                      f"({e!r}) — seguindo só pelo caminho antigo")
                session = None

    def _partial_transcribe(self, pcm: bytes) -> str:
        """Engine call for a progressive window; returns "" when it must step
        aside.  Never raises: `transcribe_pcm` already swallows the engine's
        errors, and the guard below keeps the partial from competing with a
        real transcription the worker is doing or about to do."""
        if self.sleeping or self._transcribing.is_set() or not self._q.empty():
            return ""
        try:
            return self.transcribe_pcm(bytes(pcm))
        except Exception as e:
            print(f"[{self.label}] parcial falhou ({e!r}) — seguindo")
            return ""

    def _note_partial(self, text: str, started_at: float) -> None:
        """Report one progressive window: (text, ms since the utterance opened)."""
        if self._on_partial_stream is None or not text:
            return
        ms = max(0.0, (time.monotonic() - started_at) * 1000.0)
        try:
            self._on_partial_stream(str(text), ms)
        except Exception as e:
            print(f"[{self.label}] aviso de parcial falhou ({e!r})")

    def _note_final(self, text: str) -> None:
        """Announce the definitive transcription on the same ms_since_open
        clock as the partials, from the worker thread that produced it.
        Asleep, the ear is the wake word's and nothing is announced: same
        rule that keeps the partial windows off while sleeping."""
        if self._on_final_stream is None or not self._segment_opened_at:
            return
        if self.sleeping:
            return
        ms = max(0.0, (time.monotonic() - self._segment_opened_at) * 1000.0)
        try:
            self._on_final_stream(str(text), ms)
        except Exception as e:
            print(f"[{self.label}] aviso de final falhou ({e!r})")

    # ── worker thread ───────────────────────────────────────────────────────

    def _endpoint_complete(self, pcm: bytes) -> bool:
        """True when the utterance is a complete turn, not a mid-sentence pause.

        O score do "fim de turno" fica em `_endpoint_probability` para a cauda
        adaptativa (`continue_window_for`) decidir entre 1,2 s e 3,0 s quando
        não há transcrição local para ler a palavra.
        """
        secs = len(pcm) / (2 * self._sr)
        self._endpoint_probability = None
        if self.sleeping or secs < _ENDPOINT_MIN_S or secs >= _MAX_PENDING_S:
            return True
        try:
            from core import smart_turn
            if not smart_turn.available():
                return True
            result = smart_turn.predict_endpoint(pcm)
            probability = float(result.get("probability", 1.0))
            self._endpoint_probability = probability
            if probability < _ENDPOINT_THRESHOLD:
                print(f"[{self.label}] pausa no meio da fala "
                      f"(smart turn {probability:.2f}) — aguardando continuação")
                return False
        except Exception as e:
            # A broken endpoint model must not make the assistant deaf: fall
            # back to the fixed silence rule.
            print(f"[{self.label}] smart turn falhou "
                  f"({e.__class__.__name__}) — fechando o turno")
        return True

    # ── cauda adaptativa ────────────────────────────────────────────────────

    def _tail_hint(self, pcm: bytes) -> str:
        """Palavras do trecho em pausa, quando a transcrição local existe.

        A única forma de ler a frase ANTES de ela virar turno é o transcritor
        local (core.local_stt): a rede (Groq) não pode ser chamada aqui. Sem
        transcritor local, ou dormindo, a cauda é decidida só pelo score — o
        mesmo comportamento de antes, nunca mais lento que os 3 s.
        """
        if self.sleeping or self._transcribe_fn is None:
            return ""
        try:
            if len(pcm) / (2 * self._sr) > 10.0:
                return ""
            return str(self._transcribe_fn(bytes(pcm)) or "").strip()
        except Exception:
            return ""

    def _continue_wait_for(self, pcm: bytes) -> float:
        return continue_window_for(self._tail_hint(pcm),
                                   self._endpoint_probability)

    def _run(self) -> None:
        pending = b""
        pending_at = 0.0
        pending_held = False
        pending_wait = _CONTINUE_WINDOW_S
        while not self._stop.is_set():
            try:
                pcm = self._q.get(timeout=0.25)
            except queue.Empty:
                wait = _HELD_WAIT_S if pending_held else pending_wait
                if pending and time.monotonic() - pending_at >= wait:
                    print(f"[{self.label}] continuação não veio — transcrevendo o trecho")
                    self._process_utterance(pending)
                    pending, pending_at = b"", 0.0
                    pending_held = False
                    pending_wait = _CONTINUE_WINDOW_S
                continue
            if self._stop.is_set():
                break
            if self.sleeping:
                self._process_utterance(pcm)
                continue
            if isinstance(pcm, _HeldChunk):
                # Speech retained when the microphone closed mid-sentence. It
                # is never a turn by itself: it waits longer for the tail that
                # completes the same phrase, and the merge below hands ONE
                # transcript to the brain (no double answer, no double echo).
                if pending:
                    self._process_utterance(pending)
                    pending, pending_at = b"", 0.0
                    pending_held = False
                    pending_wait = _CONTINUE_WINDOW_S
                pending, pending_at = pcm, time.monotonic()
                pending_held = True
                continue
            if isinstance(pcm, _ForcedChunk):
                # Closed by the time boundary, not by a pause: it is already a
                # complete piece, and gluing it to the next one would rebuild
                # exactly the long blob the boundary exists to avoid.
                if pending:
                    self._process_utterance(pending)
                    pending, pending_at = b"", 0.0
                    pending_held = False
                    pending_wait = _CONTINUE_WINDOW_S
                self._process_utterance(pcm)
                continue

            now = time.monotonic()
            if pending:
                age = now - pending_at
                if age <= (_HELD_WAIT_S if pending_held else pending_wait):
                    gap = b"\x00\x00" * int(0.20 * self._sr)
                    merged = HeardClip(
                        pending + gap + pcm,
                        boost_gain=max(getattr(pending, "boost_gain", 1.0),
                                       getattr(pcm, "boost_gain", 1.0)),
                        pre_rms=_min_pre_rms(pending, pcm),
                        speech_s=_sum_speech_s(pending, pcm))
                    # A merge keeps the WEAKEST proof: if either half came
                    # from the sustained-energy valve, the clip says so.
                    merged.voice_proven = bool(
                        getattr(pending, "voice_proven", True)
                        and getattr(pcm, "voice_proven", True))
                    if getattr(pending, "held_head", False):
                        # The retained head survives the merge, so a clip that
                        # still transcribes to nothing is announced as "perdi
                        # o começo" and not as a generic failure.
                        merged.held_head = True
                    pending = b""
                    pending_held = False
                    if (len(merged) < int(_MAX_PENDING_S * 2 * self._sr)
                            and not self._endpoint_complete(merged)):
                        pending, pending_at = merged, now
                        # A cauda do trecho fundido se re-mede: a nova palavra
                        # pode ter deixado a frase sensível (3 s) ou comum
                        # (1,2 s).
                        pending_wait = self._continue_wait_for(merged)
                        continue
                    pending_wait = _CONTINUE_WINDOW_S
                    self._process_utterance(merged)
                    continue
                # The user waited too long to continue: the old fragment is a
                # real turn. Send it before evaluating the new one.
                self._process_utterance(pending)
                pending, pending_at = b"", 0.0
                pending_held = False
                pending_wait = _CONTINUE_WINDOW_S

            if (len(pcm) < int(_MAX_PENDING_S * 2 * self._sr)
                    and not self._endpoint_complete(pcm)):
                pending, pending_at = pcm, now
                pending_wait = self._continue_wait_for(pcm)
                continue
            self._process_utterance(pcm)

    def _process_utterance(self, pcm: bytes) -> None:
        t0 = time.monotonic()
        # Debug aid: keep the last asleep utterance on disk so a misheard
        # wake call can be re-transcribed offline with and without hints.
        if self.sleeping and os.environ.get("JARVIS_DUMP_SLEEP_UTTERANCE"):
            try:
                import wave as _wave
                from pathlib import Path as _Path
                p = _Path(__file__).resolve().parents[1] / "logs" / "ultima-fala-dormindo.wav"
                with _wave.open(str(p), "wb") as _w:
                    _w.setnchannels(1)
                    _w.setsampwidth(2)
                    _w.setframerate(self._sr)
                    _w.writeframes(pcm)
            except Exception:
                pass
        # Awake only: asleep, the local path is the wake word's ear and must
        # keep transcribing name calls. Awake, this is the first and cheapest
        # place to drop a TV line — before Whisper and before the brain.
        if self._precheck is not None and not self.sleeping:
            try:
                keep = bool(self._precheck(pcm))
            except Exception as e:
                # A broken gate must not make the assistant deaf: fail open,
                # but say so on the console.
                print(f"[{self.label}] pré-portão falhou ({e!r}) — seguindo")
                keep = True
            if not keep:
                _duck.up()
                return
        self._transcribing.set()
        try:
            try:
                if self._transcribe_fn is not None:
                    try:
                        text = self._transcribe_fn(pcm)
                    except Exception as e:
                        # Asleep, room audio neither leaves the machine nor
                        # reaches the panel: a local failure drops quietly.
                        if self.sleeping:
                            print(f"[{self.label}] asleep — local STT failed "
                                  f"({e.__class__.__name__}), utterance dropped")
                            return
                        # Local model failed. Say so once, then keep listening
                        # through the network rung instead of dropping speech.
                        if self._fallback_fn is None:
                            raise
                        if not self._fallback_used:
                            self._fallback_used = True
                            self._on_status(f"{self.label} falhou ({e.__class__.__name__}) — "
                                            "transcrevendo no Groq até resolver.")
                        text = self._fallback_fn(pcm)
                else:
                    text = transcribe(pcm, self._key, self._model, self._lang,
                                      self._sr, prompt=self.prompt)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                self._on_status(f"{self.label}: transcrição falhou ({code}).")
                _duck.up()
                return
            except Exception as e:
                if self.sleeping:
                    print(f"[{self.label}] asleep — utterance dropped "
                          f"({e.__class__.__name__})")
                    return
                self._on_status(
                    f"{self.label}: transcrição falhou: {e.__class__.__name__}.")
                _duck.up()
                return
        finally:
            # UP-3: whatever way this left (result or early return), the
            # partial windows may use the engine again from here on.
            self._transcribing.clear()
        clean = text.strip()
        secs = len(pcm) / (2 * self._sr)
        # A real sentence the user spoke is never a prompt echo. Whisper
        # regurgitates the hint on silence/tail blips, which are short; a
        # 3-second utterance that happens to match a hint fragment is the
        # user talking to us (that case was being thrown away).
        if secs < 1.5 and self._is_prompt_echo(clean):
            # Whisper regurgitates its conditioning prompt when the audio is
            # silence or our own reverberant tail: the "utterance" comes back
            # as a sentence of the name hint, over and over. Drop it before
            # it reaches the blocklist, the gate or the brain.
            print(f"[{self.label}] discarded a prompt echo: {clean[:60]!r}")
            _duck.up()
            return
        if len(clean) < 2 or clean.lower() in _NOISE:
            if not self.sleeping:
                self._on_status(f"{self.label} não reconheceu nada nisso.")
                if len(clean) < 2 and self._audible_voice(pcm):
                    # The owner's rule (02/11/2026): eight captures vanished
                    # here with nothing but this status line. A clip with
                    # audible voice that Whisper could not read is an item,
                    # not a discard — main.py logs "fala não aproveitada" and
                    # asks him to repeat.
                    self._lost("inicio" if getattr(pcm, "held_head", False)
                               else "audio",
                               "o STT não reconheceu nada", secs)
                _duck.up()  # no text reaches main: close the exchange here
            return
        print(f"[{self.label}] {secs:.1f}s of speech → {len(clean)} chars in "
              f"{time.monotonic() - t0:.2f}s")
        try:
            from core import usage as _usage
            _usage.note(self._usage_key, utterances=1, audio_s=secs)
        except Exception:
            pass
        # UP-3: the definitive result on the partial path's own clock, logged
        # next to the partials it replaced. The turn itself continues through
        # the handler below exactly as before.
        self._note_final(clean)
        # The handler runs the whole conversation (gates, brain, tools).
        # An exception there used to end this thread, and a dead consumer
        # means the assistant keeps hearing audio forever and answers
        # nothing — the microphone meters in the log stay alive, so it looks
        # like "it only listens". One bad turn must cost one turn.
        try:
            if self._on_utterance is not None:
                self._on_utterance(clean, pcm)
            else:
                self._on_text(clean)
        except Exception as e:
            print(f"[{self.label}] handler falhou ({e!r}) — seguindo vivo")
            self._on_status("Tive um erro neste turno (detalhes no terminal).")
            # The exchange died inside the handler: nobody downstream will
            # close the duck the segmenter opened. Release it here unless a
            # reply is actively playing — in that case the playback heartbeat
            # and the host speech probe keep the apps ducked until the audio
            # really ends (main._duck_check_drain / set_speaking give it back).
            if not self.assistant_speaking:
                _duck.up()

    def stop(self) -> None:
        self._stop.set()
        # A retained fragment belongs to a session that is ending: nothing can
        # reopen its microphone, so it must not survive into the next listener.
        # Say it on the console instead of letting it disappear unseen.
        if self._held_pcm:
            print(f"[{self.label}] fala retida descartada no desligamento "
                  f"({self.held_seconds:.1f}s) — microfone desta sessão fechou")
        self._held_pcm, self._held_voiced = b"", 0
        _duck.up()
