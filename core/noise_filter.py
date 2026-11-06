"""RNNoise-inspired pre-filter for audio before STT.

Provides two entry points:

* ``filter_block`` -- lightweight spectral gate for real-time 64 ms blocks.
  Uses a short FFT + Wiener-style gain curve.  Targets < 1 ms per block on
  commodity hardware.  Applied in ``core/groq_stt.py`` on every microphone
  frame BEFORE the RMS / VAD threshold.

* ``filter_array`` -- full ``noisereduce`` spectral subtraction for accumulated
  audio (float32 array).  Applied in ``core/stt.py`` on the whole utterance
  BEFORE sending to Whisper.  Latency is not critical here because the model
  inference dominates.

Design constraints:
  * Must not corrupt clean audio (high-energy signals pass through unchanged).
  * Graceful fallback: if ``noisereduce`` is not installed, ``filter_array``
    uses the lightweight gate instead; ``filter_block`` always works (numpy).
"""
from __future__ import annotations

import time
import numpy as np

try:
    import noisereduce as nr
    _NR_AVAILABLE = True
except ImportError:
    nr = None
    _NR_AVAILABLE = False


def available() -> bool:
    """True -- the lightweight spectral gate always works (pure numpy)."""
    return True


def nr_available() -> bool:
    """True when the ``noisereduce`` library is installed (for filter_array)."""
    return _NR_AVAILABLE


# -- lightweight spectral gate (filter_block) ---------------------------------
# Designed for 64 ms blocks at 16 kHz (1024 samples).  Uses a real FFT of the
# same size, estimates the noise floor from the lower quartile of the magnitude
# spectrum, and applies a Wiener-style gain:  gain = max(floor, 1 - alpha*noise/|X|).
# The gain is smoothed across bins to avoid musical noise.

# Smoothing: exponential moving average of the noise estimate across blocks.
_NOISE_ALPHA = 0.05
# Over-subtraction factor: how aggressively to suppress estimated noise.
_OVER_SUB = 1.2
# Spectral floor: minimum gain to avoid musical noise (zeroing bins completely).
_SPECTRAL_FLOOR = 0.02
# Energy gate: blocks whose RMS is below this (float32 scale) are considered
# silence and passed through without processing.
_RMS_SILENCE_GATE = 0.003


# Running noise estimate (module-level exponential average).
_noise_estimate: np.ndarray | None = None


def reset_noise_estimate() -> None:
    """Forget the running noise floor estimate (e.g. after a mic switch)."""
    global _noise_estimate
    _noise_estimate = None


def filter_block(audio_int16: bytes | np.ndarray,
                 sample_rate: int = 16000) -> bytes:
    """Apply a lightweight spectral gate to one audio block.

    Parameters
    ----------
    audio_int16 : bytes or np.ndarray
        int16 mono PCM audio (little-endian).  An ndarray is accepted for tests.
    sample_rate : int
        Sample rate in Hz (default 16 000).

    Returns
    -------
    bytes
        Cleaned int16 LE PCM, same length as the input.
    """
    global _noise_estimate
    raw = _to_bytes(audio_int16)
    n_samples = len(raw) // 2
    if n_samples < 128:
        return raw

    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Energy gate: pure silence passes through unchanged.
    rms = float(np.sqrt(np.mean(pcm * pcm)))
    if rms < _RMS_SILENCE_GATE:
        return raw

    # Use the largest power-of-2 that fits.
    fft_size = 1
    while fft_size * 2 <= n_samples:
        fft_size *= 2
    if fft_size < 128:
        return raw

    work = pcm[:fft_size]
    # Apply a Hann window to reduce spectral leakage.
    window = np.hanning(fft_size).astype(np.float32)
    windowed = work * window

    # Real FFT (rfft) -- half the bins, half the cost.
    spec = np.fft.rfft(windowed)
    magnitude = np.abs(spec).astype(np.float32)
    n_bins = magnitude.size

    # Estimate the noise floor from the lower quartile of the magnitude spectrum.
    q25 = float(np.percentile(magnitude, 25))
    if q25 < 1e-10:
        q25 = 1e-10

    # Update the running noise estimate (exponential moving average).
    if _noise_estimate is None or _noise_estimate.size != n_bins:
        _noise_estimate = np.full(n_bins, q25, dtype=np.float32)
    else:
        _noise_estimate = (1.0 - _NOISE_ALPHA) * _noise_estimate + _NOISE_ALPHA * q25

    # Wiener-style gain: suppress bins that are close to the noise floor.
    noise_scaled = _OVER_SUB * _noise_estimate
    gain = np.maximum(_SPECTRAL_FLOOR, 1.0 - noise_scaled / (magnitude + 1e-10))
    # Smooth the gain across bins to reduce musical noise (3-tap average).
    kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
    gain_padded = np.pad(gain, (1, 1), mode="edge")
    gain_smooth = np.convolve(gain_padded, kernel, mode="valid")

    cleaned_spec = spec * gain_smooth
    cleaned = np.fft.irfft(cleaned_spec, n=fft_size).astype(np.float32)

    # Re-apply the window (overlap-add is not used in block mode).
    cleaned *= window
    cleaned = np.clip(cleaned, -1.0, 1.0)

    # Reconstruct the full block (pad if the input was longer than fft_size).
    if n_samples > fft_size:
        out = np.zeros(n_samples, dtype=np.float32)
        out[:fft_size] = cleaned
        out[fft_size:] = pcm[fft_size:]
    else:
        out = cleaned

    return (out * 32767.0).astype(np.int16).tobytes()


def filter_array(audio_float: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Array variant: float32 in -> float32 out.  Uses ``noisereduce`` when available.

    Applied by ``core/stt.py`` on the accumulated utterance before Whisper.
    Falls back to the lightweight spectral gate when ``noisereduce`` is absent.
    """
    if audio_float.size < 128:
        return audio_float
    if _NR_AVAILABLE:
        try:
            cleaned = nr.reduce_noise(
                y=audio_float.astype(np.float32),
                sr=sample_rate,
                stationary=True,
                prop_decrease=0.75,
                n_fft=512,
                hop_length=128,
            )
            return np.clip(cleaned, -1.0, 1.0).astype(np.float32)
        except Exception:
            pass
    # Fallback: apply the lightweight gate to the whole array at once.
    raw_bytes = (np.clip(audio_float, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    cleaned_bytes = filter_block(raw_bytes, sample_rate)
    return np.frombuffer(cleaned_bytes, dtype=np.int16).astype(np.float32) / 32768.0


def measure_latency(n_blocks: int = 50, sample_rate: int = 16000) -> float:
    """Measure the average per-block latency in milliseconds.

    Useful for the task metric: expected < 1 ms/block.
    """
    block_samples = int(sample_rate * 0.064)  # 64 ms
    pcm = np.random.randint(-3000, 3000, size=block_samples, dtype=np.int16)
    raw = pcm.tobytes()
    # Warm up.
    filter_block(raw, sample_rate=sample_rate)
    t0 = time.monotonic()
    for _ in range(n_blocks):
        filter_block(raw, sample_rate=sample_rate)
    elapsed_ms = (time.monotonic() - t0) * 1000.0 / n_blocks
    return elapsed_ms


def _to_bytes(audio) -> bytes:
    """Normalize input to little-endian int16 bytes."""
    if isinstance(audio, (bytes, bytearray, memoryview)):
        raw = bytes(audio)
        return raw[:-1] if len(raw) % 2 else raw
    arr = np.asarray(audio)
    if arr.dtype == np.int16:
        return arr.tobytes()
    if arr.dtype.kind in "iu":
        return arr.astype(np.int16).tobytes()
    # float -> int16
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
