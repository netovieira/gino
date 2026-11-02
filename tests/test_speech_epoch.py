"""Interrupting the owner must stop not only the current playback but every
sentence already queued for TTS. The speech epoch is what makes that true."""
from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest import mock

import main


class _UI:
    def holo_transcript(self, _text):
        pass

    def write_log(self, _text):
        pass


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class SpeechEpochTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._interrupted = False
        obj._recent_spoken_text = ""
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj._say_thread = None
        obj.audio_in_queue = _Queue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        return obj

    def test_interrupt_drops_tts_synthesised_after_the_floor_change(self):
        obj = self._obj()
        started = threading.Event()
        release = threading.Event()

        def slow_synth(_text, _settings):
            started.set()
            release.wait(timeout=5.0)
            return b"\x00" * 4800

        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k", side_effect=slow_synth):
            thread = threading.Thread(
                target=obj._say_blocking, args=("frase antiga",),
                kwargs={"epoch": 0}, daemon=True)
            thread.start()
            self.assertTrue(started.wait(timeout=2.0))
            obj._speech_epoch += 1       # the owner took the floor
            release.set()
            thread.join(timeout=2.0)

        self.assertEqual(obj.audio_in_queue.items, [])

    def test_current_epoch_is_still_spoken(self):
        obj = self._obj()
        obj._speech_epoch = 3
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           return_value=b"\x00" * 4800):
            obj._say_blocking("frase nova", epoch=3)
        self.assertEqual(len(obj.audio_in_queue.items), 2)

    def test_queued_items_from_an_old_epoch_are_skipped(self):
        obj = self._obj()
        obj._speech_epoch = 1
        obj._say_q = queue.Queue()
        calls = []
        obj._say_blocking = lambda text, log=True, epoch=None, voice_id=None, label=None, **kw: calls.append(
            (text, epoch))
        obj._ensure_say_worker()
        obj._say_q.put_nowait(("antiga", True, 0))
        obj._say_q.put_nowait(("nova", True, 1))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not calls:
            time.sleep(0.01)
        self.assertEqual(calls, [("nova", 1)])


if __name__ == "__main__":
    unittest.main()
