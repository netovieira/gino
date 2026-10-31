"""Microphone capture: quiet clips are boosted, self-audio is capped short."""
from __future__ import annotations

import time
import unittest

import numpy as np

from core import groq_stt


class BoostQuietTest(unittest.TestCase):
    def _rms(self, pcm: bytes) -> float:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(x * x)))

    def test_quiet_clip_is_raised(self):
        quiet = np.full(16000, 500, dtype=np.int16).tobytes()
        boosted = groq_stt._boost_quiet(quiet)
        self.assertGreater(self._rms(boosted), self._rms(quiet) * 2)
        self.assertLessEqual(self._rms(boosted), 0.1)

    def test_loud_clip_is_untouched(self):
        loud = np.full(16000, 8000, dtype=np.int16).tobytes()
        self.assertEqual(groq_stt._boost_quiet(loud), loud)

    def test_digital_silence_is_untouched(self):
        silent = bytes(32000)
        self.assertEqual(groq_stt._boost_quiet(silent), silent)

    def test_boost_keeps_headroom_on_a_spike(self):
        x = np.full(16000, 300, dtype=np.int16)
        x[8000] = 25000
        boosted = np.frombuffer(groq_stt._boost_quiet(x.tobytes()),
                                dtype=np.int16)
        self.assertLessEqual(int(np.max(np.abs(boosted))), int(0.96 * 32767))


class SpeakingCapTest(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None

    def tearDown(self):
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down

    def _listener(self):
        return groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda _t: None,
            on_utterance=lambda _t, _p: None,
            on_status=lambda _m: None,
            sample_rate=16000,
            transcribe_fn=lambda _pcm: "",
        )

    def test_reply_cuts_self_audio_instead_of_38_seconds(self):
        listener = self._listener()
        listener.set_assistant_speaking(True)
        block = np.full(1024, 9000, dtype=np.int16)
        try:
            for _ in range(groq_stt._SPEAK_CAP_BLOCKS + 2):
                listener.feed(block)
                time.sleep(0.001)
            self.assertFalse(listener.speaking,
                             "the self-audio segment was not cut at the cap")
        finally:
            listener.stop()

    def test_reply_start_drops_a_half_open_segment(self):
        listener = self._listener()
        block = np.full(1024, 9000, dtype=np.int16)
        try:
            for _ in range(5):
                listener.feed(block)
            self.assertTrue(listener.speaking)
            listener.set_assistant_speaking(True)
            listener.feed(block)
            self.assertFalse(listener.speaking)
        finally:
            listener.stop()


if __name__ == "__main__":
    unittest.main()
