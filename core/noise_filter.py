"""RNNoise-inspired pre-filter for audio before STT.

Provides three entry points:

* ``filter_block`` -- lightweight spectral gate for real-time 64 ms blocks.
  Uses a short FFT + Wiener-style gain curve.  Targets < 1 ms per block on
  commodity hardware.  Applied in ``core/groq_stt.py`` on every microphone
  frame BEFORE the RMS / VAD threshold.

* ``filter_array`` -- full ``noisereduce`` spectral subtraction for accumulated
  audio (float32 array).  Applied in ``core/stt.py`` on the whole utterance
  BEFORE sending to Whisper.  Latency is not critical here because the model
  inference dominates.

* ``reset_noise_estimate`` -- forget what the gate learned about the room.  The
  estimate is a running average, so it belongs to the microphone that produced
  it: ``core/groq_stt.py`` calls it whenever the input stream reopens (a mute
  window, a device switch, a new listener) -- see ``[STT] noise estimate
  reset motivo=...`` on the console.  A gate left running on another device's
  estimate multiplies every block down to the spectral floor and the assistant
  is deaf while the microphone is still delivering audio.

Design constraints:
  * Must not corrupt clean audio (high-energy signals pass through unchanged).
  * Graceful fallback: if ``noisereduce`` is not installed, ``filter_array``
    uses the lightweight gate instead; ``filter_block`` always works (numpy).
"""
from __future__ import annotations

import threading
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
# Guarda da voz: quantas vezes o nível médio do bloco tem de ficar acima do
# piso de ruído aprendido para o bloco ser considerado fala de verdade e passar
# SEM nenhum corte (byte por byte).  Medido em 10/11/2026 com os sinais do
# banco de testes (`tests/audio_fixtures.voice_block`, 64 ms, 16 kHz):
#   voz cheia / voz baixa / repetição do mesmo quadro -> 3,14 a 3,56
#   ruído branco puro (qualquer nível)               -> 1,65 a 1,78
#   voz soterrada em ruído (+-46 dB)                 -> 2,31
# O número é adimensional (razão entre duas médias do mesmo espectro), então
# não depende do ganho do microfone.  2,6 fica acima de qualquer ruído e
# abaixo de qualquer voz: o corte só acontece onde não há fala a perder.
_PROTECT_SNR = 2.6


# Running noise estimate (module-level exponential average).
#
# One microphone stream writes it from the audio callback thread on every
# 64 ms block; a stream reopen happens on a different thread. The lock exists
# for that pair: without it a `reset_noise_estimate()` landing between the read
# and the write of `filter_block` leaves the gate running on a torn (or None)
# estimate, and an estimate learned from a device that no longer exists is
# exactly the "deaf microphone" this module is blamed for.
_noise_estimate: np.ndarray | None = None
_estimate_lock = threading.Lock()
# Observability for the next debug cycle: how many times the estimate was
# forgotten in this process and why. Written under `_estimate_lock`.
_reset_count = 0
_last_reset_reason = ""


def reset_noise_estimate(reason: str = "") -> bool:
    """Forget the running noise floor estimate (e.g. after a mic switch).

    Thread-safe and idempotent: calling it twice, or on an already-empty
    estimate, is the same state as calling it once.  Returns True when there
    WAS an estimate to forget, which is what the callers use to decide whether
    the reset was a real event or a duplicate of a previous one.

    After a reset the very next block re-seeds the estimate from its own
    spectrum, so the gate is valid again immediately -- a reset never means
    "unfiltered", it means "stop trusting the old room".
    """
    global _noise_estimate, _reset_count, _last_reset_reason
    with _estimate_lock:
        had = _noise_estimate is not None
        _noise_estimate = None
        _reset_count += 1
        _last_reset_reason = str(reason or "")
    return had


def has_noise_estimate() -> bool:
    """True once the gate has measured at least one block since the last reset."""
    with _estimate_lock:
        return _noise_estimate is not None


def noise_estimate_mean() -> float | None:
    """Mean of the current noise estimate (None when there is none).

    Debug aid: a value far above the microphone's own spectrum is the signature
    of the stuck gate -- the estimate belongs to another device or to a louder
    moment of the room, and every block is being multiplied down to the
    spectral floor.
    """
    with _estimate_lock:
        estimate = _noise_estimate
        return None if estimate is None else float(estimate.mean())


def reset_stats() -> dict:
    """How many estimate resets this process has seen, and the last reason."""
    with _estimate_lock:
        return {"count": _reset_count, "last_reason": _last_reset_reason}


def filter_block(audio_int16: bytes | np.ndarray,
                 sample_rate: int = 16000) -> bytes:
    """Apply a lightweight spectral gate to one audio block.

    A block that carries a real signal comes back BYTE FOR BYTE as it
    arrived: the noise estimate is still learned from it, but nothing is
    subtracted (see ``_PROTECT_SNR``).  Only blocks whose level sits at the
    learned noise floor are gated.

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
    # Sem janela de análise: este filtro processa UM bloco de cada vez e não há
    # sobreposição alguma para somar de volta.  Multiplicar por uma Hann na
    # entrada e de novo na saída (o que estava aqui antes) deixa o bloco
    # pesado por w(t)^2 — medido: RMS 0,129 -> 0,060, ou seja -6,5 dB de uma
    # voz limpa que não tinha ruído nenhum para tirar.  O corte de ruído é o
    # ganho espectral; a janela só corrompia.
    spec = np.fft.rfft(work)
    magnitude = np.abs(spec).astype(np.float32)
    n_bins = magnitude.size

    # Estimate the noise floor from the lower quartile of the magnitude spectrum.
    q25 = float(np.percentile(magnitude, 25))
    if q25 < 1e-10:
        q25 = 1e-10

    # Update the running noise estimate (exponential moving average). The lock
    # only covers the estimate itself -- the FFT above stays lock-free -- and
    # the gain below uses the LOCAL copy, so a `reset_noise_estimate()` from
    # the reopen path can never tear the value this block is about to use.
    with _estimate_lock:
        if _noise_estimate is None or _noise_estimate.size != n_bins:
            estimate = np.full(n_bins, q25, dtype=np.float32)
        else:
            estimate = ((1.0 - _NOISE_ALPHA) * _noise_estimate
                        + _NOISE_ALPHA * q25)
        _noise_estimate = estimate

    # ── guarda da voz ───────────────────────────────────────────────────────
    # O piso aprendido é uma fração fixa do espectro do próprio bloco, então
    # subtrair dele num bloco que tem sinal em cima derruba o sinal junto.
    # Onde o nível do bloco está bem acima do piso, não há ruído a tirar: o
    # bloco volta cru, e é isso que "não corromper áudio limpo" significa em
    # practice (a restrição que este módulo sempre declarou no docstring).
    floor = float(np.mean(estimate))
    level = float(np.mean(magnitude))
    if floor > 0.0 and level >= floor * _PROTECT_SNR:
        return raw

    # Wiener-style gain: suppress bins that are close to the noise floor.
    noise_scaled = _OVER_SUB * estimate
    gain = np.maximum(_SPECTRAL_FLOOR, 1.0 - noise_scaled / (magnitude + 1e-10))
    # Smooth the gain across bins to reduce musical noise (3-tap average).
    kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
    gain_padded = np.pad(gain, (1, 1), mode="edge")
    gain_smooth = np.convolve(gain_padded, kernel, mode="valid")
    if bool(np.all(gain_smooth >= 1.0)):
        # Nada foi subtraído: devolve os bytes originais em vez da ida e volta
        # pela FFT, que deixaria um resíduo de arredondamento no áudio guardado.
        return raw

    cleaned = np.fft.irfft(spec * gain_smooth, n=fft_size).astype(np.float32)
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
