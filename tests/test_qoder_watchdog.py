"""The Qoder turn watchdog falls back locally instead of hanging forever."""
from __future__ import annotations

import threading
import unittest

import main
from core import metrics


class FakeLive:
    def __init__(self):
        self.interrupted = 0
    def interrupt(self):
        self.interrupted += 1


class FakeQueue:
    def __init__(self):
        self.items = []
    def put_nowait(self, item):
        self.items.append(item)


class QoderWatchdogTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def _obj(self, current="t1"):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_current_id = current
        obj._qoder_turns = {"t1": "texto original"}
        obj._qoder_ignore = False
        obj._qoder_live = FakeLive()
        obj._qoder_turn_timeout_s = 45.0
        obj._brain_q = FakeQueue()
        obj.said = []
        obj.finished = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._qoder_finish = lambda turn_id: obj.finished.append(turn_id)
        return obj

    def test_timeout_interrupts_and_falls_back(self):
        obj = self._obj()
        main.JarvisLive._qoder_turn_timeout(obj, "t1")
        self.assertEqual(obj._qoder_live.interrupted, 1)
        self.assertEqual(obj.finished, ["t1"])
        self.assertEqual(obj._brain_q.items, ["texto original"])
        self.assertTrue(obj.said)
        self.assertEqual(metrics.snapshot()["counters"].get("qoder.timeout"), 1)

    def test_stale_timeout_does_nothing(self):
        obj = self._obj(current="t2")
        main.JarvisLive._qoder_turn_timeout(obj, "t1")
        self.assertEqual(obj._qoder_live.interrupted, 0)
        self.assertEqual(obj.finished, [])
        self.assertEqual(obj._brain_q.items, [])

    def test_recent_progress_rearms_instead_of_falling_back(self):
        import time
        obj = self._obj()
        obj._qoder_turn_deadline = time.monotonic() + 60
        obj._qoder_last_activity = time.monotonic()
        armed = []
        obj._arm_qoder_watchdog = lambda turn_id: armed.append(turn_id)
        main.JarvisLive._qoder_turn_timeout(obj, "t1")
        self.assertEqual(armed, ["t1"])
        self.assertEqual(obj._qoder_live.interrupted, 0)
        self.assertEqual(obj.finished, [])

    def test_hard_deadline_falls_back_even_with_progress(self):
        import time
        obj = self._obj()
        obj._qoder_turn_deadline = time.monotonic() - 1
        obj._qoder_last_activity = time.monotonic()
        main.JarvisLive._qoder_turn_timeout(obj, "t1")
        self.assertEqual(obj._qoder_live.interrupted, 1)
        self.assertEqual(obj.finished, ["t1"])


if __name__ == "__main__":
    unittest.main()
