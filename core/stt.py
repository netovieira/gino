"""
Speech-to-Text engines for MARK XL.

Whisper  – offline transcription via faster-whisper (VAD-buffered)
Vosk     – offline streaming transcription (lighter)
"""
import json
import numpy as np

from core import silero_vad as _silero_vad


# ── Silero VAD pre-filter ────────────────────────────────────────────────────
# Before sending accumulated audio to Whisper, run Silero VAD to strip silence
# and keep only speech segments.  Falls back to RMS-based detection when the
# ONNX model is absent.

_VAD_BLOCK_SAMPLES = 16000  # 1 second at 16 kHz
_VAD_RMS_THRESHOLD = 0.01   # fallback RMS threshold for speech detection


def vad_filter_audio(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Filter audio using Silero VAD (or RMS fallback).

    Returns the audio with silence stripped from the beginning and end.
    If Silero VAD is available, uses it for accurate speech detection.
    Otherwise, falls back to RMS-based detection.

    Args:
        audio: float32 mono audio array at sample_rate Hz
        sample_rate: audio sample rate (default 16000)

    Returns:
        Filtered audio array (silence stripped)
    """
    if audio.size == 0:
        return audio

    block_size = int(sample_rate * 0.032)  # 32 ms blocks for VAD

    if _silero_vad.available():
        # Use Silero VAD for accurate speech detection
        try:
            vad = _silero_vad.SileroVAD(threshold=0.5)
            # Find first and last speech blocks
            first_speech = 0
            last_speech = audio.size
            for i in range(0, audio.size, block_size):
                block = audio[i:i + block_size]
                if block.size < block_size // 2:
                    break
                if vad.is_speech(block):
                    if first_speech == 0 and i > 0:
                        first_speech = max(0, i - block_size)
                    last_speech = min(audio.size, i + block_size + block_size)
            return audio[first_speech:last_speech]
        except Exception:
            # VAD failed, fall back to RMS
            pass

    # RMS fallback: strip silence from beginning and end
    rms_threshold = _VAD_RMS_THRESHOLD
    first_speech = 0
    last_speech = audio.size

    for i in range(0, audio.size, block_size):
        block = audio[i:i + block_size]
        if block.size == 0:
            break
        rms = float(np.sqrt(np.mean(block * block)))
        if rms >= rms_threshold:
            if first_speech == 0 and i > 0:
                first_speech = max(0, i - block_size)
            last_speech = min(audio.size, i + block_size + block_size)
            break

    # Find last speech block
    for i in range(audio.size - block_size, 0, -block_size):
        block = audio[i:i + block_size]
        if block.size == 0:
            continue
        rms = float(np.sqrt(np.mean(block * block)))
        if rms >= rms_threshold:
            last_speech = min(audio.size, i + block_size + block_size)
            break

    return audio[first_speech:last_speech]


class WhisperSTT:
    """Offline transcription using faster-whisper."""

    def __init__(self, model_name: str = "base", language: str | None = None):
        import os
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        try:
            self._model = WhisperModel(model_name, device=device, compute_type=compute)
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print(f"[STT] Whisper '{model_name}' not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._model = WhisperModel(model_name, device=device, compute_type=compute)
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Whisper '{model_name}' model download failed.\n"
                        f"Internet access is required the first time to download the speech model (~75–290 MB).\n"
                        f"After the first download it runs fully offline.\n"
                        f"Details: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        print(f"[STT] Whisper '{model_name}' ready ({device})")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns transcript string."""
        try:
            # Pre-filter with Silero VAD (or RMS fallback) to strip silence
            # before sending to Whisper.  This reduces hallucinations on
            # room noise and improves transcription accuracy.
            audio = vad_filter_audio(audio)
            if audio.size == 0:
                return ""
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # greedy — 2-3x faster
                best_of=1,
                condition_on_previous_text=False,  # no hallucinations, faster
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            print(f"[STT] Transcription error: {e}")
            raise


class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False
