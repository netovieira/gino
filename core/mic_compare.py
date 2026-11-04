"""
core/mic_compare.py — decide WHICH microphone is hearing the person, comparing
the separate signals instead of mixing them.

WHY THIS EXISTS
    Two microphones cannot simply be summed: a webcam mic 40 cm away and a
    headset mic on the person's ear pick up the same voice with different
    delays, so summing them comb-filters the speech into a hollow, less
    intelligible signal — and it also doubles the room noise. The owner's
    instruction is exact: one stream per microphone, never mixed. So the second
    microphone is not there to be added, it is there to be *compared*: if both
    signals look the same, what is being heard is the room (the TV, the fan, a
    conversation in the next room) and no microphone is close to anyone; if one
    signal is much louder and uncorrelated with the other, that microphone is
    near the speaker and should be the one the assistant listens to.

WHAT IS MEASURED, PER 32 ms FRAME (512 samples at 16 kHz)
    * level difference     ΔdB = 20·log10(rms_a / rms_b)
    * normalized cross-correlation over the whole frame, with the lag searched
      inside ±10 ms — the same sound arriving at two microphones a few
      centimetres apart is shifted by at most that
    * band coherence, 300–3400 Hz (the telephone band where speech lives), over
      a sliding 1 s window: |Sxy|² / (Sxx·Syy), 1.0 = the same sound at both,
      near 0 = two unrelated noises

    The lag search and the band projection are plain matrix products against
    buffers allocated ONCE in __init__; nothing allocates per block. That
    matters because this runs inside the audio callback's time budget, ~31
    times per second, next to the STT and the UI.

VERDICT
    diffuse    |ΔdB| < 3, correlation > 0.6, |lag| < 2 ms — the same sound at
               the same level and the same instant at both microphones: the
               room, not a person in front of either.
    near       |ΔdB| > 6 and correlation < 0.45 — one microphone carries a
               signal the other does not: whoever is talking is in front of it.
    ambiguous  everything else (a difference too small to be one voice, or a
               correlation too high to be two independent sources).

    The near rule is deliberately symmetric on the sign of ΔdB: the caller
    cannot know in advance whether channel A or B is the close one, so
    ``reading.near`` says which side is louder. Requiring a positive ΔdB would
    make the detector blind whenever the array lists the quiet mic first.

This module imports numpy and nothing else from the project — no ``main``, no
sounddevice, no PipeWire. It is fed arrays by whoever owns the streams.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# What the voice pipeline already runs at, so a caller can hand us raw blocks.
SAMPLE_RATE = 16000
FRAME = 512                      # 32 ms at 16 kHz
MAX_LAG_MS = 10.0
WINDOW_SECONDS = 1.0
BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 3400.0

# Verdict thresholds — see the module docstring for why these numbers.
DIFFUSE_MAX_LEVEL_DB = 3.0
DIFFUSE_MIN_CORRELATION = 0.6
DIFFUSE_MAX_LAG_MS = 2.0
NEAR_MIN_LEVEL_DB = 6.0
NEAR_MAX_CORRELATION = 0.45

DIFFUSE = "diffuse"
NEAR = "near"
AMBIGUOUS = "ambiguous"

VERDICT_LABELS = {
    DIFFUSE: "difuso (sala/TV)",
    NEAR: "locutor próximo",
    AMBIGUOUS: "ambíguo",
}
LABELS_FALLBACK = VERDICT_LABELS[AMBIGUOUS]

# Digital silence has rms 0, which would make ΔdB infinite (or a ZeroDivision).
# One LSB of a 16-bit sample squared is the floor: anything below it is silence
# and the difference is capped so a log never returns inf into the UI.
_ENERGY_FLOOR = 1.0 / (1 << 16) ** 2
_LEVEL_LIMIT_DB = 120.0

# Coherence of two signals that carry nothing in the speech band is not 0/0
# that numpy can be trusted with: a 6 kHz tone projects to ~1e-10 of numerical
# leakage in every band bin, and 1e-10/1e-10 reads as perfect coherence. Below
# this per-frame band energy (rms ~ -60 dBFS inside 300–3400 Hz) there is no
# speech to compare, so coherence is reported as 0 instead of noise.
_BAND_ENERGY_FLOOR = 1e-6


@dataclass(slots=True)
class PairReading:
    """One frame's comparison of two microphones.

    ``level_db`` is A relative to B (positive = A louder). ``lag_ms`` is how
    much B is delayed relative to A (positive = B arrives later). ``coherence``
    is the band coherence over the trailing 1 s window (0..1) and
    ``window_frames`` how many frames that window currently holds.

    ``PairComparator.push`` returns the SAME object every call, updated in
    place — no allocation per block. Copy the fields you need to keep.
    """

    level_db: float = 0.0
    correlation: float = 0.0
    lag_ms: float = 0.0
    coherence: float = 0.0
    window_frames: int = 0
    near: str = ""
    verdict: str = AMBIGUOUS
    silent: bool = True


def verdict_for(level_db: float, correlation: float, lag_ms: float) -> str:
    """Classify one comparison. Pure, so the thresholds can be tested alone."""
    level = float(level_db)
    corr = float(correlation)
    lag = abs(float(lag_ms))
    if (abs(level) < DIFFUSE_MAX_LEVEL_DB
            and corr > DIFFUSE_MIN_CORRELATION
            and lag < DIFFUSE_MAX_LAG_MS):
        return DIFFUSE
    if abs(level) > NEAR_MIN_LEVEL_DB and corr < NEAR_MAX_CORRELATION:
        return NEAR
    return AMBIGUOUS


def label_for(verdict: str) -> str:
    """Portuguese sentence fragment for the UI/log ('difuso (sala/TV)')."""
    return VERDICT_LABELS.get(str(verdict or ""), VERDICT_LABELS[AMBIGUOUS])


class PairComparator:
    """Compares the same 32 ms frame of two microphones, block by block.

    Usage::

        cmp = PairComparator()
        while True:
            reading = cmp.push(block_a, block_b)
            if reading.verdict == mic_compare.NEAR:
                ...

    Buffers (frame, lag search, band projection, 1 s coherence ring) are built
    in ``__init__``; ``push`` only writes into them.
    """

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame: int = FRAME,
        max_lag_ms: float = MAX_LAG_MS,
        window_seconds: float = WINDOW_SECONDS,
        band: tuple[float, float] = (BAND_LOW_HZ, BAND_HIGH_HZ),
    ) -> None:
        self.sample_rate = int(sample_rate) or SAMPLE_RATE
        self.frame = int(frame) or FRAME
        if self.frame < 16:
            raise ValueError("frame curto demais")
        self.max_lag = max(1, int(round(self.sample_rate * float(max_lag_ms) / 1000.0)))
        self.window_frames = max(
            1, int(round(float(window_seconds) * self.sample_rate / self.frame)))
        self.band = (float(band[0]), float(band[1]))

        # ── two current frames (float32, -1..1) ────────────────────────────
        self._a = np.zeros(self.frame, dtype=np.float32)
        self._b = np.zeros(self.frame, dtype=np.float32)

        # ── lag search: b padded by max_lag zeros on both sides, so the
        # sliding window view gives every shift in [-max_lag, +max_lag] ─────
        self._lead = self.max_lag
        self._b_pad = np.zeros(self.frame + 2 * self.max_lag, dtype=np.float32)
        self._windows = sliding_window_view(self._b_pad, self.frame)
        self._corr = np.zeros(2 * self.max_lag + 1, dtype=np.float32)
        self._corr_abs = np.zeros_like(self._corr)

        # ── band projection: a DFT matrix for the speech band only, one
        # matmul per frame instead of an FFT that would allocate ────────────
        self._bins = self._band_bins()
        self._basis = self._dft_basis()
        self._spec_a = np.zeros(len(self._bins), dtype=np.complex64)
        self._spec_b = np.zeros(len(self._bins), dtype=np.complex64)
        self._mag = np.zeros(len(self._bins), dtype=np.float32)

        # ── sliding 1 s coherence window (ring of band spectra) ─────────────
        self._ring_a = np.zeros((self.window_frames, len(self._bins)), dtype=np.complex64)
        self._ring_b = np.zeros((self.window_frames, len(self._bins)), dtype=np.complex64)
        self._mag_ring = np.zeros((self.window_frames, len(self._bins)), dtype=np.float32)
        self._cross_ring = np.zeros((self.window_frames, len(self._bins)),
                                    dtype=np.complex64)
        self._sxx = np.zeros(len(self._bins), dtype=np.float32)
        self._syy = np.zeros(len(self._bins), dtype=np.float32)
        self._sxy = np.zeros(len(self._bins), dtype=np.complex64)
        self._den = np.zeros(len(self._bins), dtype=np.float32)
        self._coh = np.zeros(len(self._bins), dtype=np.float32)
        self._ratio = np.zeros(len(self._bins), dtype=np.float32)
        self._pos = 0
        self._filled = 0

        self.reading = PairReading()
        self._coherence = 0.0

    # ── construction helpers ────────────────────────────────────────────────

    def _band_bins(self) -> np.ndarray:
        """DFT bins whose centre falls inside the speech band (>= 1 bin)."""
        low = int(math.ceil(self.band[0] * self.frame / self.sample_rate))
        high = int(math.floor(self.band[1] * self.frame / self.sample_rate))
        low = max(low, 1)
        high = min(high, self.frame // 2)
        if high < low:
            low = high = min(max(1, low), self.frame // 2)
        return np.arange(low, high + 1, dtype=np.float32)

    def _dft_basis(self) -> np.ndarray:
        n = np.arange(self.frame, dtype=np.float64)
        phase = np.outer(self._bins.astype(np.float64) / float(self.frame), n)
        return np.exp(-2j * np.pi * phase).astype(np.complex64)

    # ── input ───────────────────────────────────────────────────────────────

    def _fill(self, buf: np.ndarray, data: object) -> None:
        """Copy (and scale) one frame into a preallocated float32 buffer."""
        arr = np.asarray(data)
        if arr.ndim == 2:
            arr = arr[:, 0]
        elif arr.ndim > 2:
            arr = arr.reshape(-1)
        if arr.size < self.frame:
            raise ValueError(
                f"bloco curto: {arr.size} amostras, esperado {self.frame}")
        seg = arr[: self.frame]
        if seg.dtype == np.int16:
            np.divide(seg, 32768.0, out=buf, casting="unsafe")
        else:
            np.copyto(buf, seg, casting="unsafe")
        if not np.isfinite(buf).all():
            np.nan_to_num(buf, copy=False, nan=0.0, posinf=0.0, neginf=0.0)

    def reset(self) -> None:
        """Forget the 1 s window (a stream was reopened). Keeps the buffers."""
        self._ring_a.fill(0)
        self._ring_b.fill(0)
        self._pos = 0
        self._filled = 0
        self._coherence = 0.0
        self.reading = PairReading()

    # ── the comparison ──────────────────────────────────────────────────────

    def push(self, a: object, b: object) -> PairReading:
        """Compare one frame of channel A with one frame of channel B.

        Accepts int16 (scaled by 1/32768), float arrays in -1..1, or a
        2-D block whose first column is taken as mono. Returns the persistent
        :class:`PairReading` (same object every call).
        """
        self._fill(self._a, a)
        self._fill(self._b, b)

        energy_a = float(np.dot(self._a, self._a))
        energy_b = float(np.dot(self._b, self._b))
        silent = energy_a <= _ENERGY_FLOOR and energy_b <= _ENERGY_FLOOR
        level_db = 10.0 * math.log10(
            max(energy_a, _ENERGY_FLOOR) / max(energy_b, _ENERGY_FLOOR))
        level_db = max(-_LEVEL_LIMIT_DB, min(_LEVEL_LIMIT_DB, level_db))

        # Normalized cross-correlation for every candidate lag at once.
        self._b_pad[self._lead:self._lead + self.frame] = self._b
        np.matmul(self._windows, self._a, out=self._corr)
        denom = math.sqrt(max(energy_a, _ENERGY_FLOOR)
                          * max(energy_b, _ENERGY_FLOOR))
        if denom > 0.0:
            np.multiply(self._corr, 1.0 / denom, out=self._corr)
        np.abs(self._corr, out=self._corr_abs)
        peak = int(np.argmax(self._corr_abs))
        # float32 rounding can push a perfect match a hair past 1.0.
        correlation = max(-1.0, min(1.0, float(self._corr[peak])))
        lag_ms = (peak - self.max_lag) * 1000.0 / self.sample_rate

        # Band energies of this frame, then the sliding 1 s coherence.
        np.matmul(self._basis, self._a, out=self._spec_a)
        np.matmul(self._basis, self._b, out=self._spec_b)
        self._ring_a[self._pos] = self._spec_a
        self._ring_b[self._pos] = self._spec_b
        self._pos = (self._pos + 1) % self.window_frames
        self._filled = min(self._filled + 1, self.window_frames)
        self._coherence = self._band_coherence()

        near = ""
        if level_db > 0.0:
            near = "a"
        elif level_db < 0.0:
            near = "b"

        reading = self.reading
        reading.level_db = level_db
        reading.correlation = correlation
        reading.lag_ms = lag_ms
        reading.coherence = self._coherence
        reading.window_frames = self._filled
        reading.near = near
        reading.verdict = verdict_for(level_db, correlation, lag_ms)
        reading.silent = silent
        return reading

    def _band_coherence(self) -> float:
        """|Σ A·conj(B)|² / (Σ|A|² · Σ|B|²) over the trailing window, 0..1."""
        np.abs(self._ring_a, out=self._mag_ring)
        np.square(self._mag_ring, out=self._mag_ring)
        np.sum(self._mag_ring, axis=0, out=self._sxx)
        np.abs(self._ring_b, out=self._mag_ring)
        np.square(self._mag_ring, out=self._mag_ring)
        np.sum(self._mag_ring, axis=0, out=self._syy)
        np.conjugate(self._ring_b, out=self._cross_ring)
        np.multiply(self._ring_a, self._cross_ring, out=self._cross_ring)
        np.sum(self._cross_ring, axis=0, out=self._sxy)

        np.abs(self._sxy, out=self._coh)
        np.square(self._coh, out=self._coh)
        np.multiply(self._sxx, self._syy, out=self._den)
        self._ratio.fill(0.0)
        floor = _BAND_ENERGY_FLOOR * max(1, self._filled)
        if (float(np.add.reduce(self._sxx)) < floor
                or float(np.add.reduce(self._syy)) < floor):
            return 0.0
        np.divide(self._coh, self._den, out=self._ratio, where=self._den > 0.0)
        value = float(np.mean(self._ratio))
        if not math.isfinite(value):
            return 0.0
        return min(1.0, max(0.0, value))

    # ── introspection for logs/tests ────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """True once the coherence window is full (1 s of audio compared)."""
        return self._filled >= self.window_frames
