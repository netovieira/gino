"""core/source_class.py — what is the microphone hearing right now?

Pure numpy, no model files, no network, no GPU. One chunk of mono PCM goes in;
one of ``silencio``, ``ruido``, ``pessoa``, ``tv`` or ``musica`` comes out,
together with a confidence and the numbers that produced it.

The features are deliberately cheap enough to run beside the capture callback:
RMS, the 300–3400 Hz voice band (and what falls outside it), spectral flatness
and entropy, zero-crossing rate, envelope variation, spectral flux/stability,
peak concentration (tonality) and autocorrelation periodicity.

    from core import source_class
    reading = source_class.classify(block, sample_rate=16000)
    reading.label, reading.confidence, reading.features

A single 10–25 ms block is too short to see syllables or pauses. For a stream,
keep one :class:`SourceClassifier` and feed every block — it classifies the
trailing 0.5 s at most every 100 ms and returns the cached reading in between.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SILENCE = "silencio"
NOISE = "ruido"
PERSON = "pessoa"
TV = "tv"
MUSIC = "musica"
CLASSES = (SILENCE, NOISE, PERSON, TV, MUSIC)

SAMPLE_RATES = (16000, 48000)
DEFAULT_SAMPLE_RATE = 16000
VOICE_LOW_HZ = 300.0
VOICE_HIGH_HZ = 3400.0
FRAME_SECONDS = 0.032
SILENCE_DBFS = -58.0

_EPS = 1e-10


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _ramp(value: float, low: float, high: float) -> float:
    """0 below ``low``, 1 above ``high``, linear in between."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    return _clamp((value - low) / (high - low))


def _as_mono(chunk: object) -> np.ndarray:
    """Accept int16, int32, float, bytes or a 2-D block; return float32 mono."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        raw = np.frombuffer(chunk, dtype=np.int16)
        arr = raw.astype(np.float32) / 32768.0
    else:
        arr = np.asarray(chunk)
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        elif arr.dtype.kind in "iu":
            bits = np.iinfo(arr.dtype).max
            arr = arr.astype(np.float32) / float(bits)
        elif arr.dtype.kind == "f":
            arr = arr.astype(np.float32, copy=False)
        else:
            raise ValueError(f"tipo de amostra não suportado: {arr.dtype}")
    if arr.ndim == 2:
        arr = arr[:, 0]
    elif arr.ndim > 2:
        arr = arr.reshape(-1)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return np.ascontiguousarray(arr, dtype=np.float32)


def _frame_matrix(x: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """View of consecutive frames; a short block is zero-padded to one frame."""
    if x.size <= frame:
        padded = np.zeros(frame, dtype=np.float32)
        padded[: x.size] = x
        return padded[None, :]
    count = 1 + (x.size - frame) // hop
    starts = np.arange(count)[:, None] * hop
    return x[starts + np.arange(frame)[None, :]]


def _autocorrelation_peak(x: np.ndarray, sample_rate: int) -> float:
    """Largest normalized autocorrelation in the 60–400 Hz pitch range."""
    usable = x[: min(x.size, sample_rate * 2)]
    if usable.size <= 64:
        return 0.0
    usable = usable - float(usable.mean())
    nfft = 1 << int(math.ceil(math.log2(max(2, usable.size * 2))))
    spectrum = np.fft.rfft(usable, nfft)
    acf = np.fft.irfft(spectrum * np.conjugate(spectrum), nfft)[: usable.size]
    if float(acf[0]) <= 0.0:
        return 0.0
    acf = acf / float(acf[0])
    low = max(1, int(sample_rate / 400.0))
    high = min(usable.size - 1, int(sample_rate / 60.0))
    if high <= low:
        return 0.0
    return _clamp(float(np.max(acf[low: high + 1])), -1.0, 1.0)


def _features(x: np.ndarray, sample_rate: int) -> dict[str, float]:
    n = int(x.size)
    duration = n / float(sample_rate)
    rms = float(np.sqrt(np.mean(x * x))) if n else 0.0
    rms_db = 20.0 * math.log10(max(rms, 1e-9))
    peak = float(np.max(np.abs(x))) if n else 0.0
    crest = peak / (rms + _EPS)

    frame = max(64, int(round(FRAME_SECONDS * sample_rate)))
    hop = max(1, frame // 2)
    frames = _frame_matrix(x, frame, hop)
    window = np.hanning(frame).astype(np.float32)
    spectra = np.fft.rfft(frames * window, axis=1)
    power = spectra.real ** 2 + spectra.imag ** 2 + _EPS
    freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)

    frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    mean_rms = float(np.mean(frame_rms))
    env_cv = float(np.std(frame_rms) / (mean_rms + _EPS))
    env_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-9))
    dyn_range = (float(np.percentile(env_db, 90) - np.percentile(env_db, 10))
                 if frame_rms.size > 2 else 0.0)
    threshold = max(float(np.max(frame_rms)) * 0.1, 1e-6)
    active_ratio = float(np.mean(frame_rms >= threshold))
    # Pauses are part of speech, not part of its spectrum: spectral shape is
    # measured only on frames that carry sound.
    active = frame_rms >= max(float(np.max(frame_rms)) * 0.08, 1e-4)
    if not bool(np.any(active)):
        active = np.ones_like(active, dtype=bool)

    total = np.sum(power[:, 1:], axis=1)
    voice = np.sum(power[:, (freqs >= VOICE_LOW_HZ) & (freqs <= VOICE_HIGH_HZ)], axis=1)
    low = np.sum(power[:, (freqs > 0) & (freqs < VOICE_LOW_HZ)], axis=1)
    high = np.sum(power[:, freqs > VOICE_HIGH_HZ], axis=1)
    band_total = total + 3.0 * _EPS
    voice_ratio = float(np.mean((voice / band_total)[active]))
    low_ratio = float(np.mean((low / band_total)[active]))
    high_ratio = float(np.mean((high / band_total)[active]))

    bins = power[:, 1:][active]
    flatness = float(np.mean(
        np.exp(np.mean(np.log(bins), axis=1)) / np.mean(bins, axis=1)))
    norm = bins / (np.sum(bins, axis=1, keepdims=True) + _EPS)
    entropy = float(np.mean(
        -np.sum(norm * np.log(norm + _EPS), axis=1) / math.log(bins.shape[1])))

    sign = np.signbit(x)
    zcr = (float(np.count_nonzero(sign[1:] != sign[:-1]) / (n - 1))
           if n > 1 else 0.0)

    magnitude = np.sqrt(power)
    magnitude /= (np.linalg.norm(magnitude, axis=1, keepdims=True) + _EPS)
    pairs = active[1:] & active[:-1]
    flux = (float(np.mean(
        1.0 - np.sum(magnitude[1:] * magnitude[:-1], axis=1)[pairs]))
        if bool(np.any(pairs)) else 0.0)
    stability = _clamp(1.0 - flux)

    top = np.sort(bins, axis=1)[:, -5:]
    tonal = float(np.mean(np.sum(top, axis=1) / (np.sum(bins, axis=1) + _EPS)))
    dominant = np.argmax(bins, axis=1)
    median_bin = float(np.median(dominant))
    peak_stability = float(np.mean(np.abs(dominant - median_bin) <= 2.0))

    syllabic = 0.0
    if frame_rms.size >= 8:
        envelope = frame_rms - mean_rms
        env_power = np.abs(np.fft.rfft(envelope)) ** 2
        env_freqs = np.fft.rfftfreq(frame_rms.size, hop / float(sample_rate))
        band = env_power[(env_freqs >= 2.0) & (env_freqs <= 8.0)]
        syllabic = float(np.sum(band) / (np.sum(env_power[1:]) + _EPS))

    harmonicity = _autocorrelation_peak(x, sample_rate)

    # A digital-silent frame has only the EPS floor in every bin, so the band
    # ratios above describe numerical noise; zero them instead of lying.
    if rms_db < -80.0:
        voice_ratio = low_ratio = high_ratio = 0.0

    return {
        "duration_s": duration,
        "rms": rms,
        "rms_db": rms_db,
        "peak": peak,
        "crest": crest,
        "voice_ratio": voice_ratio,
        "low_ratio": low_ratio,
        "high_ratio": high_ratio,
        "flatness": flatness,
        "entropy": entropy,
        "zcr": zcr,
        "env_cv": env_cv,
        "dyn_range_db": dyn_range,
        "active_ratio": active_ratio,
        "flux": flux,
        "stability": stability,
        "tonal": tonal,
        "peak_stability": peak_stability,
        "harmonicity": harmonicity,
        "syllabic": syllabic,
        "frames": float(frames.shape[0]),
        "sample_rate": float(sample_rate),
    }


def _scores(f: dict[str, float]) -> dict[str, float]:
    """Heuristic evidence per class, each 0..1 (the winner is picked in
    ``classify``)."""
    flat = f["flatness"]
    zcr = f["zcr"]
    crest = f["crest"]
    active = f["active_ratio"]

    # Keyboard clicks/impacts are short, peaky, spectrally flat and wideband.
    impulsive = _ramp(crest, 6.0, 14.0) * _ramp(0.55 - active, 0.0, 0.4)
    noise = max(_ramp(flat, 0.40, 0.70), impulsive)
    if f["tonal"] > 0.45 or f["harmonicity"] > 0.6:
        noise *= 0.35
    if zcr > 0.18:
        noise = max(noise, _ramp(zcr, 0.18, 0.50) * _ramp(flat, 0.40, 0.60))
    if f["voice_ratio"] > 0.65 and flat < 0.35:
        noise *= 0.25                 # syllables, not clicks: energy stays in-band

    # Stable harmonics, strong peaks and little frame-to-frame change = music.
    tonal = max(_ramp(f["tonal"], 0.22, 0.55),
                _ramp(f["harmonicity"], 0.45, 0.75))
    steady = 0.35 + 0.65 * _ramp(f["stability"], 0.80, 0.96)
    dry = 0.35 + 0.65 * _ramp(0.40 - f["env_cv"], 0.0, 0.30)
    clean = _ramp(0.60 - flat, 0.0, 0.35)
    music = tonal * steady * dry * clean

    # Speech lives in 300–3400 Hz and its envelope rises and falls in syllables.
    voiced = _ramp(f["voice_ratio"], 0.42, 0.78)
    syllabic = _ramp(f["env_cv"], 0.22, 0.70)
    changing = 0.35 + 0.65 * _ramp(0.97 - f["stability"], 0.0, 0.25)
    person = voiced * syllabic * changing * (0.35 + 0.65 * _ramp(flat, 0.01, 0.12))
    if f["harmonicity"] > 0.85 and f["env_cv"] < 0.30:
        person *= 0.45                      # steady sung tone is music, not talk

    # TV = voice band plus broadband bed, compressed: little dynamic range.
    broad = max(_ramp(f["high_ratio"], 0.06, 0.40),
                _ramp(f["low_ratio"], 0.10, 0.40))
    present = max(_ramp(f["voice_ratio"], 0.15, 0.45),
                  0.7 * _ramp(f["high_ratio"], 0.30, 0.70))
    compressed = _ramp(0.80 - f["env_cv"], 0.0, 0.45)
    tv = present * compressed * (0.20 + 0.80 * broad) * (
        0.25 + 0.75 * _ramp(0.94 - f["stability"], 0.0, 0.20))
    if f["tonal"] > 0.45:
        tv *= 0.55
    if noise > 0.55:
        tv *= 0.35
    if zcr > 0.45 or flat > 0.60:
        tv *= 0.40                    # white/hiss is noise, not a program

    silence = 0.0
    if f["rms_db"] <= SILENCE_DBFS:
        silence = 1.0
    elif f["rms_db"] < -45.0:
        silence = 0.5 * _ramp(-45.0 - f["rms_db"], 0.0, 13.0)
    if f["peak"] > 0.02:
        silence *= 0.2
    if silence > 0.8:
        scale = 1.0 - 0.85 * silence
        noise *= scale
        person *= scale
        tv *= scale
        music *= scale

    return {
        SILENCE: _clamp(silence),
        NOISE: _clamp(noise),
        PERSON: _clamp(person),
        TV: _clamp(tv),
        MUSIC: _clamp(music),
    }


@dataclass(slots=True)
class SourceReading:
    """One classification. ``features`` is plain floats, safe to log as JSON."""

    label: str
    confidence: float
    features: dict[str, float] = field(default_factory=dict)

    @property
    def is_silence(self) -> bool:
        return self.label == SILENCE


def _reading(label: str, confidence: float, f: dict[str, float]) -> SourceReading:
    return SourceReading(label=label, confidence=_clamp(confidence, 0.0, 0.99),
                         features=f)


def classify(chunk: object, sample_rate: int = DEFAULT_SAMPLE_RATE) -> SourceReading:
    """Classify one PCM block. ``chunk`` is mono float (-1..1) or int16.

    A block of at least ~0.25 s sees syllables and pauses; shorter blocks still
    get an answer, but ``confidence`` is lower because the envelope is blind.
    """
    rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
    if rate < 4000 or rate > 192000:
        raise ValueError(f"sample_rate fora da faixa: {sample_rate}")
    x = _as_mono(chunk)
    if x.size == 0:
        f = _features(x, rate)
        return _reading(SILENCE, 0.99, f)

    features = _features(x, rate)
    scores = _scores(features)
    label = max(CLASSES, key=lambda name: (scores[name], -CLASSES.index(name)))
    top = scores[label]
    second = max(v for k, v in scores.items() if k != label)
    confidence = 0.50 + 0.49 * top * (0.55 + 0.45 * _ramp(top - second, 0.0, 0.35))
    if features["duration_s"] < 0.25:
        confidence *= 0.75
    if top <= 0.05:
        label, confidence = NOISE, 0.33
    features.update({f"score_{name}": score for name, score in scores.items()})
    return _reading(label, confidence, features)


class SourceClassifier:
    """Rolling classifier for a live stream: feed 10–25 ms blocks, get labels.

    The trailing ``window_ms`` is kept and re-analysed at most every
    ``interval_ms``; between those moments ``push`` returns the cached reading.
    This is what keeps the audio callback cheap.
    """

    def __init__(self, *, window_ms: float = 500.0, interval_ms: float = 100.0,
                 sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        self.window_ms = max(100.0, float(window_ms))
        self.interval_ms = max(20.0, float(interval_ms))
        self.sample_rate = int(sample_rate or DEFAULT_SAMPLE_RATE)
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._since_analysis = 0
        self._rate = self.sample_rate
        self._reading = _reading(SILENCE, 0.5, _features(np.zeros(0), self._rate))

    def reset(self) -> None:
        self._chunks.clear()
        self._samples = 0
        self._since_analysis = 0
        self._rate = self.sample_rate
        self._reading = _reading(
            SILENCE, 0.5, _features(np.zeros(0), self._rate))

    @property
    def window_samples(self) -> int:
        return max(16, int(round(self._rate * self.window_ms / 1000.0)))

    def push(self, chunk: object,
             sample_rate: int | None = None) -> SourceReading:
        """Add one block; returns the current (possibly cached) reading."""
        rate = int(sample_rate or self._rate or self.sample_rate)
        if rate != self._rate:
            self._chunks.clear()
            self._samples = 0
            self._since_analysis = 0
            self._rate = rate
        x = _as_mono(chunk)
        if x.size == 0:
            return self._reading
        self._chunks.append(x)
        self._samples += x.size
        self._since_analysis += x.size
        limit = self.window_samples
        while self._samples > limit and self._chunks:
            excess = self._samples - limit
            first = self._chunks[0]
            if first.size <= excess:
                self._samples -= first.size
                self._chunks.pop(0)
            else:
                self._chunks[0] = first[excess:]
                self._samples -= excess
                break
        interval = max(16, int(self._rate * self.interval_ms / 1000.0))
        if self._since_analysis < interval:
            return self._reading
        if self._samples < max(16, int(self._rate * 0.08)):
            return self._reading
        self._since_analysis = 0
        self._reading = classify(np.concatenate(self._chunks), self._rate)
        return self._reading
