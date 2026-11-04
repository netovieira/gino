"""Synthetic-source classification (core/source_class.py).

Every signal is generated with a fixed seed: no microphone, no model, no GPU.
The labels are the contract; the feature values may move a little with numpy,
but a stable harmonic tone must stay ``musica`` and a white burst ``ruido``.
"""
from __future__ import annotations

import unittest

import numpy as np

from core import source_class


def _tone(sample_rate: int) -> np.ndarray:
    t = np.arange(sample_rate) / float(sample_rate)
    signal = sum(amplitude * np.sin(2 * np.pi * 220 * harmonic * t)
                 for harmonic, amplitude in
                 ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25), (5, 0.2)))
    return (0.2 * signal).astype(np.float32)


def _white_burst(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    signal = np.zeros(sample_rate, dtype=np.float32)
    length = int(0.12 * sample_rate)
    signal[:length] = 0.4 * rng.standard_normal(length)
    return signal


def _speech_like(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    t = np.arange(sample_rate) / float(sample_rate)
    white = rng.standard_normal(sample_rate)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(sample_rate, 1.0 / sample_rate)
    spectrum[(freqs < 300) | (freqs > 3400)] = 0.0
    voiced = np.fft.irfft(spectrum, sample_rate)
    voiced /= np.max(np.abs(voiced))
    syllables = (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)) ** 2
    syllables *= np.sin(2 * np.pi * 0.8 * t) > -0.3
    return (0.35 * voiced * syllables).astype(np.float32)


def _tv_like(sample_rate: int) -> np.ndarray:
    rng = np.random.default_rng(42)
    mixed = _speech_like(sample_rate) + 0.25 * _tone(sample_rate)
    mixed += 0.08 * rng.standard_normal(sample_rate)
    compressed = np.tanh(2.0 * mixed) / np.tanh(2.0) * 0.4
    return compressed.astype(np.float32)


def _silence(sample_rate: int) -> np.ndarray:
    return np.zeros(sample_rate, dtype=np.float32)


def _steady_white(sample_rate: int) -> np.ndarray:
    return (0.1 * np.random.default_rng(9).standard_normal(sample_rate)
            ).astype(np.float32)


CASES = {
    "musica": _tone,
    "ruido": _white_burst,
    "pessoa": _speech_like,
    "tv": _tv_like,
    "silencio": _silence,
}


class SourceClassTest(unittest.TestCase):
    def test_each_synthetic_source_at_16k(self):
        for expected, make in CASES.items():
            with self.subTest(expected):
                reading = source_class.classify(make(16000), 16000)
                self.assertEqual(reading.label, expected)
                self.assertGreater(reading.confidence, 0.5)

    def test_each_synthetic_source_at_48k(self):
        for expected, make in CASES.items():
            with self.subTest(expected):
                reading = source_class.classify(make(48000), 48000)
                self.assertEqual(reading.label, expected)
                self.assertGreater(reading.confidence, 0.5)

    def test_steady_white_noise_is_noise_not_tv(self):
        for rate in (16000, 48000):
            with self.subTest(rate):
                self.assertEqual(
                    source_class.classify(_steady_white(rate), rate).label,
                    source_class.NOISE)

    def test_short_transient_is_noise(self):
        rate = 16000
        reading = source_class.classify(_white_burst(rate), rate)
        self.assertEqual(reading.label, source_class.NOISE)
        self.assertGreater(reading.features["crest"], 5.0)
        self.assertLess(reading.features["active_ratio"], 0.4)

    def test_features_are_finite_and_serialisable(self):
        reading = source_class.classify(_tone(16000), 16000)
        for key, value in reading.features.items():
            self.assertIsInstance(key, str)
            self.assertTrue(np.isfinite(value), key)
        self.assertIn("rms_db", reading.features)
        self.assertIn("voice_ratio", reading.features)
        self.assertIn("flatness", reading.features)
        self.assertIn("harmonicity", reading.features)
        for name in source_class.CLASSES:
            self.assertIn(f"score_{name}", reading.features)

    def test_int16_and_2d_blocks_are_accepted(self):
        signal = _tone(16000)
        pcm = np.clip(signal * 32767, -32768, 32767).astype(np.int16)
        reading = source_class.classify(pcm.reshape(-1, 1), 16000)
        self.assertEqual(reading.label, source_class.MUSIC)
        raw = source_class.classify(pcm.tobytes(), 16000)
        self.assertEqual(raw.label, source_class.MUSIC)

    def test_silence_and_empty_block(self):
        self.assertEqual(
            source_class.classify(np.zeros(0, np.float32), 16000).label,
            source_class.SILENCE)
        very_quiet = (1e-5 * np.random.default_rng(1).standard_normal(16000)
                      ).astype(np.float32)
        self.assertEqual(
            source_class.classify(very_quiet, 16000).label,
            source_class.SILENCE)

    def test_invalid_sample_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            source_class.classify(_tone(16000), 10)

    def test_rolling_classifier_tracks_the_last_half_second(self):
        for expected in ("musica", "pessoa", "tv"):
            with self.subTest(expected):
                rate = 16000
                signal = CASES[expected](rate)
                classifier = source_class.SourceClassifier(window_ms=500)
                reading = None
                for start in range(0, rate, rate // 50):
                    reading = classifier.push(signal[start:start + rate // 50],
                                              rate)
                self.assertIsNotNone(reading)
                self.assertEqual(reading.label, expected)
                self.assertLessEqual(reading.features["duration_s"], 0.55)

    def test_rolling_classifier_returns_silence_before_any_audio(self):
        classifier = source_class.SourceClassifier()
        reading = classifier.push(np.zeros(0, np.float32), 16000)
        self.assertEqual(reading.label, source_class.SILENCE)
        self.assertEqual(reading.confidence, 0.5)


if __name__ == "__main__":
    unittest.main()
