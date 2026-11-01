"""The live conversation has no client-side turn timeout.

The owner's rule: a heavy task runs in a background subagent and the chat never
waits or gets aborted by the client. These tests encode the removal of the old
watchdog, not its behaviour.
"""
from __future__ import annotations

import threading
import unittest

import main


class FakeLive:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1


class NoTurnTimeoutTest(unittest.TestCase):
    def test_no_client_side_turn_timeout_exists(self):
        self.assertFalse(hasattr(main.JarvisLive, "_qoder_turn_timeout"))
        self.assertFalse(hasattr(main.JarvisLive, "_arm_qoder_watchdog"))

    def test_touch_only_records_activity(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_last_activity = 0.0
        main.JarvisLive._qoder_touch(obj)
        self.assertGreater(obj._qoder_last_activity, 0.0)

    def test_new_turn_preempts_the_busy_one_without_interrupt(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_current_id = "t1"
        obj._qoder_live = FakeLive()
        obj.finished = []
        obj._qoder_finish = lambda turn_id, flush=True: obj.finished.append(turn_id)
        main.JarvisLive._qoder_preempt(obj, "t1")
        self.assertEqual(obj.finished, ["t1"])
        # The sidecar aborts the old turn when ask(priority=now) arrives; the
        # client never calls interrupt() on a timer.
        self.assertEqual(obj._qoder_live.interrupted, 0)


if __name__ == "__main__":
    unittest.main()
