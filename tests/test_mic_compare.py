"""Two microphones compared, never summed (core/mic_compare.py).

Every signal here is synthesized with a fixed seed: no microphone, no GPU, no
PortAudio. The numbers are the same on any machine, which is the point — the
verdict thresholds are the contract, and they must not drift silently.
"""
from __future__ import annotations

import math
import unittest
from pathlib import Path

import numpy as np

from core import mic_compare

RATE = 16000
FRAME = 512


def _tone(freq: float, amp: float = 0.3, frame: int = FRAME,
          phase: float = 0.0) -> np.ndarray:
    t = np.arange(frame) / RATE
    return (amp * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)


def _tone_at(index: int, freq: float = 440.0, amp: float = 0.3) -> np.ndarray:
    """Consecutive frames of one continuous tone (phase advances by 512)."""
    return _tone(freq, amp, phase=2 * np.pi * freq * index * FRAME / RATE)


def _stream(gen, frames: int = 40) -> mic_compare.PairReading:
    comparator = mic_compare.PairComparator()
    reading = None
    for index in range(frames):
        a, b = gen(index)
        reading = comparator.push(a, b)
    return reading


class VerdictThresholdsTest(unittest.TestCase):
    def test_documented_thresholds(self):
        self.assertEqual(mic_compare.verdict_for(0.0, 0.7, 0.0),
                         mic_compare.DIFFUSE)
        self.assertEqual(mic_compare.verdict_for(7.0, 0.2, 0.0),
                         mic_compare.NEAR)
        # The same difference with B louder is still a near speaker.
        self.assertEqual(mic_compare.verdict_for(-8.0, 0.1, 0.0),
                         mic_compare.NEAR)
        # Too big for diffuse, too correlated for a single voice.
        self.assertEqual(mic_compare.verdict_for(4.0, 0.5, 0.0),
                         mic_compare.AMBIGUOUS)
        # Correlation must be strictly above 0.6 for diffuse.
        self.assertEqual(mic_compare.verdict_for(0.0, 0.6, 0.0),
                         mic_compare.AMBIGUOUS)
        # A delay at the edge of the room window breaks the diffuse verdict.
        self.assertEqual(mic_compare.verdict_for(0.0, 0.9, 2.5),
                         mic_compare.AMBIGUOUS)

    def test_labels_are_portuguese_and_total(self):
        self.assertIn("difuso", mic_compare.label_for(mic_compare.DIFFUSE))
        self.assertIn("locutor", mic_compare.label_for(mic_compare.NEAR))
        self.assertEqual(mic_compare.label_for("nonsense"),
                         mic_compare.LABELS_FALLBACK)


class SameSoundIsDiffuseTest(unittest.TestCase):
    def test_equal_tone_in_both_channels(self):
        reading = _stream(lambda i: (_tone_at(i), _tone_at(i)))
        self.assertEqual(reading.verdict, mic_compare.DIFFUSE)
        self.assertLess(abs(reading.level_db), 0.1)
        self.assertGreater(reading.correlation, 0.95)
        self.assertLess(abs(reading.lag_ms), 0.1)
        self.assertGreater(reading.coherence, 0.9)
        self.assertFalse(reading.silent)

    def test_same_noise_walked_through_the_window(self):
        rng = np.random.default_rng(11)

        def gen(_index):
            frame = (0.1 * rng.standard_normal(FRAME)).astype(np.float32)
            return frame, frame

        reading = _stream(gen)
        self.assertEqual(reading.verdict, mic_compare.DIFFUSE)
        self.assertGreaterEqual(reading.coherence, 0.99)


class NearSpeakerTest(unittest.TestCase):
    def test_energy_only_in_channel_a(self):
        reading = _stream(lambda i: (_tone_at(i), np.zeros(FRAME, np.float32)), 1)
        self.assertEqual(reading.verdict, mic_compare.NEAR)
        self.assertEqual(reading.near, "a")
        self.assertGreater(reading.level_db, 6.0)
        self.assertLess(reading.correlation, 0.45)

    def test_energy_only_in_channel_b(self):
        reading = _stream(lambda i: (np.zeros(FRAME, np.float32), _tone_at(i)), 1)
        self.assertEqual(reading.verdict, mic_compare.NEAR)
        self.assertEqual(reading.near, "b")
        self.assertLess(reading.level_db, -6.0)
        self.assertLess(reading.correlation, 0.45)


class AmbiguousTest(unittest.TestCase):
    def test_difference_too_small_for_a_near_speaker_and_signals_unrelated(self):
        # 4 dB: above the diffuse level gate, below the near one.
        a = _tone(440.0, amp=0.4)
        b = _tone(632.0, amp=0.4 * 10 ** (-4.0 / 20.0))
        reading = _stream(lambda _i: (a, b), 1)
        self.assertGreater(abs(reading.level_db), 3.0)
        self.assertLess(abs(reading.level_db), 6.0)
        self.assertEqual(reading.verdict, mic_compare.AMBIGUOUS)

    def test_correlated_but_not_loud_enough_to_separate(self):
        a = _tone(440.0, amp=0.4)
        b = _tone(440.0, amp=0.25)
        reading = _stream(lambda _i: (a, b), 1)
        self.assertGreater(reading.correlation, 0.95)
        self.assertGreater(abs(reading.level_db), 3.0)
        self.assertEqual(reading.verdict, mic_compare.AMBIGUOUS)

    def test_digital_silence_is_ambiguous_not_nan(self):
        zeros = np.zeros(FRAME, np.float32)
        reading = _stream(lambda _i: (zeros, zeros), 1)
        self.assertTrue(reading.silent)
        self.assertEqual(reading.verdict, mic_compare.AMBIGUOUS)
        for value in (reading.level_db, reading.correlation, reading.lag_ms,
                      reading.coherence):
            self.assertTrue(math.isfinite(value))


class LagTest(unittest.TestCase):
    def test_delay_is_reported_in_milliseconds(self):
        rng = np.random.default_rng(5)
        frame = (0.1 * rng.standard_normal(FRAME)).astype(np.float32)
        delayed = np.roll(frame, 5).astype(np.float32)   # B arrives 312 us later
        reading = _stream(lambda _i: (frame, delayed), 1)
        expected = 5 * 1000.0 / RATE
        self.assertAlmostEqual(reading.lag_ms, expected, delta=1000.0 / RATE)
        self.assertGreater(reading.correlation, 0.9)

    def test_a_delay_beyond_the_window_is_clamped_not_invented(self):
        rng = np.random.default_rng(6)
        frame = (0.1 * rng.standard_normal(FRAME)).astype(np.float32)
        far = np.roll(frame, 400).astype(np.float32)     # 25 ms — outside ±10
        reading = _stream(lambda _i: (frame, far), 1)
        self.assertLessEqual(abs(reading.lag_ms), mic_compare.MAX_LAG_MS + 1e-6)


class CoherenceTest(unittest.TestCase):
    def test_independent_noise_has_no_coherence(self):
        rng = np.random.default_rng(23)

        def gen(_index):
            return ((0.1 * rng.standard_normal(FRAME)).astype(np.float32),
                    (0.1 * rng.standard_normal(FRAME)).astype(np.float32))

        reading = _stream(gen)
        self.assertLess(reading.coherence, 0.2)
        self.assertNotEqual(reading.verdict, mic_compare.DIFFUSE)
        self.assertEqual(reading.window_frames,
                         mic_compare.PairComparator().window_frames)

    def test_energy_outside_the_speech_band_is_not_coherent(self):
        def gen(index):
            frame = _tone_at(index, freq=6000.0)
            return frame, frame

        reading = _stream(gen)
        self.assertLess(reading.coherence, 0.05)

    def test_window_fills_to_one_second(self):
        comparator = mic_compare.PairComparator()
        frame = _tone(440.0)
        for _ in range(comparator.window_frames - 1):
            comparator.push(frame, frame)
        self.assertFalse(comparator.ready)
        comparator.push(frame, frame)
        self.assertTrue(comparator.ready)
        reading = comparator.push(frame, frame)
        self.assertEqual(reading.window_frames, comparator.window_frames)

    def test_reset_empties_the_window(self):
        comparator = mic_compare.PairComparator()
        frame = _tone(440.0)
        for _ in range(10):
            comparator.push(frame, frame)
        comparator.reset()
        reading = comparator.push(frame, frame)
        self.assertEqual(reading.window_frames, 1)
        self.assertFalse(comparator.ready)


class InputHandlingTest(unittest.TestCase):
    def test_int16_is_scaled_like_float(self):
        tone = _tone(440.0, amp=0.5)
        pcm = (tone * 32767).astype(np.int16)
        comparator = mic_compare.PairComparator()
        reading = comparator.push(pcm, pcm)
        self.assertLess(abs(reading.level_db), 0.01)
        self.assertGreater(reading.correlation, 0.99)
        self.assertFalse(reading.silent)

    def test_two_dimensional_block_uses_the_first_channel(self):
        tone = _tone(440.0, amp=0.5)
        block = np.stack([tone, tone], axis=1)
        comparator = mic_compare.PairComparator()
        reading = comparator.push(block, tone)
        self.assertLess(abs(reading.level_db), 0.01)

    def test_short_block_is_refused(self):
        comparator = mic_compare.PairComparator()
        with self.assertRaises(ValueError):
            comparator.push(np.zeros(100, np.float32), np.zeros(100, np.float32))

    def test_non_finite_samples_do_not_poison_the_reading(self):
        bad = _tone(440.0)
        bad[10] = float("nan")
        bad[20] = float("inf")
        reading = _stream(lambda _i: (bad, bad), 1)
        self.assertTrue(math.isfinite(reading.level_db))
        self.assertTrue(math.isfinite(reading.correlation))


class AllocationTest(unittest.TestCase):
    def test_buffers_and_reading_are_reused_across_blocks(self):
        comparator = mic_compare.PairComparator()
        frame = _tone(440.0)
        probe = (comparator._a, comparator._b, comparator._corr,
                 comparator._basis, comparator._ring_a, comparator._sxx)
        addresses = [buf.ctypes.data for buf in probe]
        first_reading = comparator.push(frame, frame)
        for _ in range(200):
            reading = comparator.push(frame, frame)
        self.assertIs(reading, first_reading)
        self.assertEqual(addresses, [buf.ctypes.data for buf in probe])
        oversized = np.full(FRAME * 4, 0.1, np.float32)
        comparator.push(oversized, oversized)       # still a buffer copy
        self.assertEqual(addresses, [buf.ctypes.data for buf in probe])


class IsolationTest(unittest.TestCase):
    def test_module_never_imports_main(self):
        source = Path(mic_compare.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("import main"),
                             "mic_compare must not import main")
            self.assertFalse(stripped.startswith("from main"),
                             "mic_compare must not import main")


if __name__ == "__main__":
    unittest.main()
