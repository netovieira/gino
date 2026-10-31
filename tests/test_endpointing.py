"""Endpointing tests: an unfinished sentence must be merged, not sent early."""
from __future__ import annotations

import time
import unittest

from core import groq_stt


class EndpointingTest(unittest.TestCase):
    def setUp(self):
        self._orig_up = groq_stt._duck.up
        self._orig_down = groq_stt._duck.down
        groq_stt._duck.up = lambda: None
        groq_stt._duck.down = lambda: None

    def tearDown(self):
        groq_stt._duck.up = self._orig_up
        groq_stt._duck.down = self._orig_down

    def _listener(self, got, endpoint=None):
        listener = groq_stt.GroqListener(
            "", "test", "pt",
            on_text=lambda text: got.append(("text", text, 0)),
            on_utterance=lambda text, pcm: got.append(("utterance", text, len(pcm))),
            on_status=lambda _m: None,
            sample_rate=16000,
            transcribe_fn=lambda pcm: f"len={len(pcm)}",
        )
        if endpoint is not None:
            listener._endpoint_complete = endpoint
        return listener

    def test_unfinished_segment_is_merged_with_continuation(self):
        got = []
        sequence = iter([False, True])
        listener = self._listener(got, endpoint=lambda _pcm: next(sequence, True))
        first = b"\x01\x00" * int(0.8 * 16000)
        second = b"\x01\x00" * int(0.8 * 16000)
        try:
            listener._q.put(first)
            time.sleep(0.1)
            listener._q.put(second)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not got:
                time.sleep(0.02)
        finally:
            listener.stop()
        self.assertTrue(got, "the merged utterance was never emitted")
        kind, text, size = got[0]
        self.assertEqual(kind, "utterance")
        self.assertGreater(size, len(first) + len(second))
        self.assertTrue(text.startswith("len="))

    def test_incomplete_fragment_flushes_after_the_continue_window(self):
        old_window = groq_stt._CONTINUE_WINDOW_S
        groq_stt._CONTINUE_WINDOW_S = 0.3
        got = []
        listener = self._listener(got, endpoint=lambda _pcm: False)
        fragment = b"\x01\x00" * int(0.8 * 16000)
        try:
            listener._q.put(fragment)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not got:
                time.sleep(0.02)
        finally:
            listener.stop()
            groq_stt._CONTINUE_WINDOW_S = old_window
        self.assertTrue(got, "the pending fragment was never flushed")
        self.assertEqual(got[0][0], "utterance")
        self.assertEqual(got[0][2], len(fragment))

    def test_short_command_closes_immediately(self):
        got = []
        listener = self._listener(got)
        short = b"\x01\x00" * int(0.3 * 16000)
        try:
            listener._q.put(short)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not got:
                time.sleep(0.02)
        finally:
            listener.stop()
        self.assertTrue(got)
        self.assertEqual(got[0][0], "utterance")


if __name__ == "__main__":
    unittest.main()
