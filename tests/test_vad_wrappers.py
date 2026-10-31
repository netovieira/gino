"""Smoke tests for the two ONNX wrappers. They skip when the models are absent
(the ONNX files are git-ignored and downloaded by Phase 0)."""
from __future__ import annotations

import unittest

import numpy as np

from core import silero_vad, smart_turn


@unittest.skipUnless(smart_turn.available(), "Smart Turn model not downloaded")
class SmartTurnTest(unittest.TestCase):
    def test_silence_probability_is_a_probability(self):
        result = smart_turn.predict_endpoint(np.zeros(16_000, dtype=np.float32))
        self.assertIn(result["prediction"], (0, 1))
        self.assertGreaterEqual(result["probability"], 0.0)
        self.assertLessEqual(result["probability"], 1.0)

    def test_accepts_int16_bytes_and_truncates_long_audio(self):
        result = smart_turn.predict_endpoint(b"\x00\x00" * (16_000 * 9))
        self.assertIn(result["prediction"], (0, 1))
        self.assertGreaterEqual(result["probability"], 0.0)
        self.assertLessEqual(result["probability"], 1.0)


@unittest.skipUnless(silero_vad.available(), "Silero VAD model not downloaded")
class SileroVadTest(unittest.TestCase):
    def test_silence_stays_below_threshold(self):
        vad = silero_vad.SileroVAD(threshold=0.5)
        best = max(vad.process(np.zeros(512, dtype=np.float32)) for _ in range(10))
        self.assertLess(best, 0.1)

    def test_accepts_int16_bytes_and_resets(self):
        vad = silero_vad.SileroVAD()
        value = vad.process(b"\x00\x00" * 512)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)
        vad.reset()
        self.assertEqual(vad.process(b""), 0.0)


if __name__ == "__main__":
    unittest.main()
