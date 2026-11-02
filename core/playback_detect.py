"""Playback-vs-live classifier for one speech segment (cheap DSP, CPU only).

The microphone hears the room, and the room includes the TV, a series or a
song. Speaker identification alone is not enough: a series voice is simply an
*unknown* voice, and an unknown voice is also what a bad microphone produces.

This module adds one independent piece of evidence — does the segment carry
the acoustic fingerprints of a loudspeaker playing a produced mix, rather than
a person talking in the room? No model, no GPU, no network: a handful of numpy
features on the same PCM the segmenter already cut.

Features (all documented because calibration matters more than the code):

    sub_bass       energy below 80 Hz / total. Produced mixes carry bass a
                   close-talking microphone does not pick up from a voice.
    air            energy above 8 kHz / speech-band energy. Lossy playback and
                   TV speakers roll the top off; a live voice close to a good
                   mic keeps some air.
    crest_db       peak/RMS in dB. Broadcast/streaming compression flattens
                   dynamics (roughly 6–10 dB); live speech is more peaky.
    clip_ratio     fraction of samples at the waveform's own peak. Clipping
                   and heavy limiting leave flat tops and harmonic distortion.
    f0_cv          coefficient of variation of the frame-wise F0. A produced
                   tonal bed is steadier than a speaking voice.
    speech_like    True when the energy sits in the speech band with a
                   voice-like zero-crossing rate. A pure bass line is NOT
                   classified as voice, by design.

The verdict is deliberately conservative: ``playback`` only when the weighted
score passes ``threshold`` AND at least two features are individually telling.
On doubt the label is ``live`` — the voice decides, the artefact only weighs
against.
"""
from __future__ import annotations

from typing import Any

import numpy as np

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_PLAYBACK_THRESHOLD = 0.68

_FRAME = 512          # 32 ms at 16 kHz
_HOP = 256            # 16 ms
_EPS = 1e-12
# Below this duration the spectral shape is not trustworthy.
_MIN_SECONDS = 0.20

# Weights sum to 1.0. Compression and the missing sub-bass / missing air pair
# are the strongest discriminators on real room recordings.
_WEIGHTS = {
    "compression": 0.30,
    "sub_bass": 0.25,
    "air": 0.15,
    "distortion": 0.15,
    "f0_stability": 0.15,
}


def _audio_float(value: Any) -> np.ndarray:
    """int16 PCM bytes/array or float samples → mono float32 in [-1, 1]."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if len(raw) % 2:
            raw = raw[:-1]
        if not raw:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    arr = np.asarray(value)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.size == 0:
        return np.zeros(0, dtype=np.float32)
    if arr.dtype.kind in "iu":
        return arr.astype(np.float32) / 32768.0
    out = arr.astype(np.float32)
    if float(np.max(np.abs(out))) > 1.5:
        out = out / 32768.0
    return out


def _frames(x: np.ndarray) -> np.ndarray:
    if x.size < _FRAME:
        x = np.pad(x, (0, _FRAME - x.size))
    count = 1 + (x.size - _FRAME) // _HOP
    index = np.arange(_FRAME)[None, :] + _HOP * np.arange(count)[:, None]
    return x[index]


def _band(spectrum: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs < hi)
    return float(spectrum[:, mask].sum())


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return float(min(hi, max(lo, value)))


def _f0_cv(frames: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """(median F0 Hz, coefficient of variation) from autocorrelation frames.

    Only frames with a clear periodicity contribute; 40 frames is enough and
    keeps the whole call in the low-millisecond range.
    """
    rms = np.sqrt(np.mean(frames * frames, axis=1) + _EPS)
    loud = rms >= max(1e-4, 0.25 * float(np.percentile(rms, 90)))
    chosen = np.flatnonzero(loud)
    if chosen.size > 40:
        chosen = chosen[:: max(1, chosen.size // 40)][:40]
    lag_min = max(2, int(sample_rate / 350))
    lag_max = min(frames.shape[1] - 1, int(sample_rate / 70))
    values: list[float] = []
    for i in chosen:
        frame = frames[i] - float(np.mean(frames[i]))
        energy = float(np.dot(frame, frame))
        if energy <= _EPS:
            continue
        corr = np.correlate(frame, frame, mode="full")[frames.shape[1] - 1:]
        corr = corr / energy
        window = corr[lag_min:lag_max + 1]
        if window.size == 0:
            continue
        peak = int(np.argmax(window))
        if float(window[peak]) < 0.30:
            continue
        values.append(sample_rate / float(lag_min + peak))
    if len(values) < 3:
        return 0.0, 0.0
    arr = np.asarray(values, dtype=np.float64)
    mean = float(np.mean(arr))
    if mean <= 0.0:
        return 0.0, 0.0
    return float(np.median(arr)), float(np.std(arr) / mean)


def classify(value: Any, sample_rate: int = DEFAULT_SAMPLE_RATE,
             threshold: float = DEFAULT_PLAYBACK_THRESHOLD) -> dict:
    """Label one segment ``live`` / ``playback`` / ``not_voice``.

    ``play`` is the weighted playback score in [0, 1]; ``confidence`` repeats
    it for the caller. ``features`` is exposed so a real recording can be
    calibrated from the log without guessing.
    """
    x = _audio_float(value)
    sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
    empty = {
        "label": "unknown", "confidence": 0.0, "play": 0.0,
        "speech_like": False, "features": {},
    }
    if x.size < int(_MIN_SECONDS * sample_rate):
        return empty
    x = x - float(np.mean(x))
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x * x) + _EPS))
    if peak <= 1e-5 or rms <= 1e-6:
        return empty
    x = x / max(peak, _EPS)      # level-independent features

    frames = _frames(x)
    window = np.hanning(_FRAME).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1)) ** 2
    freqs = np.fft.rfftfreq(_FRAME, 1.0 / sample_rate)
    total = float(spectrum.sum()) + _EPS
    sub_bass = _band(spectrum, freqs, 20, 80) / total
    air = _band(spectrum, freqs, 8000, sample_rate / 2) / total
    speech = _band(spectrum, freqs, 300, 3400) / total
    # Share of the NON-bass energy that sits in the speech band. A voice over a
    # music bed is still voice-like even when the bass dominates the mix.
    speech_share = speech / max(1.0 - sub_bass, _EPS)
    air_ratio = air / max(speech, _EPS)

    crest_db = 20.0 * float(np.log10(max(peak, _EPS) / max(rms, _EPS)))
    # A clipped/limited waveform sits at its own peak for a visible fraction of
    # the samples; a natural voice crosses it only at the rare transient.
    clip_ratio = float(np.mean(np.abs(x) >= 0.985))
    f0_median, f0_cv = _f0_cv(frames, sample_rate)
    zcr = float(np.mean(x[1:] * x[:-1] < 0.0))

    speech_like = bool(speech_share >= 0.08 and 0.008 <= zcr <= 0.35
                       and (70.0 <= f0_median <= 380.0 or speech_share >= 0.35))

    compression_ev = _clamp((16.0 - crest_db) / 8.0)
    sub_ev = _clamp((sub_bass - 0.02) / 0.25)
    # At 0.015 air/speech the top is gone; above 0.12 it is clearly open.
    air_ev = _clamp((0.015 - air_ratio) / 0.015)
    distortion_ev = _clamp(clip_ratio / 0.02)
    f0_ev = _clamp((0.06 - f0_cv) / 0.06) if f0_cv > 0.0 else 0.0

    evidence = {
        "compression": compression_ev,
        "sub_bass": sub_ev,
        "air": air_ev,
        "distortion": distortion_ev,
        "f0_stability": f0_ev,
    }
    score = sum(_WEIGHTS[name] * value for name, value in evidence.items())
    telling = sum(1 for value in evidence.values() if value >= 0.35)

    if not speech_like:
        label = "not_voice"
        confidence = 1.0 - _clamp(speech / 0.20)
    elif score >= float(threshold) and telling >= 2:
        label = "playback"
        confidence = score
    else:
        label = "live"
        confidence = 1.0 - score

    return {
        "label": label,
        "confidence": float(round(confidence, 4)),
        "play": float(round(score, 4)),
        "speech_like": speech_like,
        "features": {
            "sub_bass": round(sub_bass, 4),
            "air_ratio": round(air_ratio, 4),
            "speech_ratio": round(speech, 4),
            "crest_db": round(crest_db, 2),
            "clip_ratio": round(clip_ratio, 4),
            "f0_hz": round(f0_median, 1),
            "f0_cv": round(f0_cv, 4),
            "zcr": round(zcr, 4),
            "evidence": {k: round(v, 3) for k, v in evidence.items()},
        },
    }
