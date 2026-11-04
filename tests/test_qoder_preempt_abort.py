"""Barge-in abort is not a failure: preemption silences, real error still speaks."""
from __future__ import annotations

import threading
import time
import unittest

import main


class _Fake:
    """Minimal JarvisLive surface used by _qoder_on_error."""

    def __init__(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_turns = {"t1": "resume o texto da persona"}
        obj._qoder_spoken = {}
        obj._qoder_meta = {}
        obj._qoder_voice_preempts = {}
        obj._qoder_ignore = False
        obj._qoder_resume_session = False
        obj._qoder_session_reset_tried = False
        obj._memory_librarian = None
        obj.said = []
        obj.finished = []
        obj.retried = []
        obj._qoder_retry_used = False
        obj._qoder_live_ready = lambda: True
        obj._qoder_hold = lambda *a, **k: obj.said.append(("HOLD", a, k))
        obj._qoder_turn = lambda *a, **k: obj.retried.append((a, k))
        obj._qoder_flush_pending = lambda: None
        obj._say_now = lambda text, **kwargs: obj.said.append((text, kwargs))
        obj._qoder_finish = (
            lambda turn_id, flush=True: obj.finished.append((turn_id, flush)))
        self.obj = obj


def _spoken(obj):
    return [item[0] for item in obj.said]


class QoderPreemptAbortTest(unittest.TestCase):
    def test_voice_preemption_abort_is_silent(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_current_id = "t1"
        main.JarvisLive._mark_qoder_voice_preempt(obj)
        self.assertIn("t1", obj._qoder_voice_preempts)

        main.JarvisLive._qoder_on_error(obj, "t1", "Operation aborted")

        self.assertEqual(_spoken(obj), [])
        self.assertEqual(obj.finished, [("t1", False)])
        self.assertNotIn("t1", obj._qoder_voice_preempts)

    def test_real_error_after_barge_in_is_retried_with_the_same_words(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_current_id = "t1"
        main.JarvisLive._mark_qoder_voice_preempt(obj)

        main.JarvisLive._qoder_on_error(obj, "t1", "provider falhou: 500")

        self.assertEqual(len(obj.retried), 1)
        args, kwargs = obj.retried[0]
        self.assertEqual(args[0], "resume o texto da persona")
        self.assertTrue(kwargs.get("retry"))
        self.assertFalse(any("pode repetir" in text.lower() for text in _spoken(obj)),
                         "o dono nunca é convidado a repetir")

    def test_abort_without_preemption_is_retried(self):
        fake = _Fake()
        obj = fake.obj

        main.JarvisLive._qoder_on_error(obj, "t1", "Operation aborted")

        self.assertEqual(len(obj.retried), 1)
        self.assertFalse(any("pode repetir" in text.lower() for text in _spoken(obj)))

    def test_old_turn_failure_is_silent_when_a_newer_turn_is_live(self):
        # 02/11 13:16: the owner spoke, the sidecar aborted the old turn and
        # the app relaunched it over the new question ("turno falhou —
        # relançando a mesma fala"), adding 20–49 s of stale work.
        fake = _Fake()
        obj = fake.obj
        obj._qoder_current_id = "t2"

        main.JarvisLive._qoder_on_error(obj, "t1", "Operation aborted")

        self.assertEqual(obj.retried, [])
        self.assertEqual(_spoken(obj), [])
        self.assertEqual(obj.finished, [("t1", False)])

    def test_current_turn_failure_still_retries(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_current_id = "t1"

        main.JarvisLive._qoder_on_error(obj, "t1", "provider falhou: 500")

        self.assertEqual(len(obj.retried), 1)

    def test_second_failure_is_held_instead_of_asking_to_repeat(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_retry_used = True

        main.JarvisLive._qoder_on_error(obj, "t1", "provider falhou: 500")

        self.assertEqual(obj.retried, [])
        self.assertFalse(any("pode repetir" in text.lower() for text in _spoken(obj)))
        self.assertTrue(any(item[0] == "HOLD" for item in obj.said))

    def test_stale_preempt_mark_expires(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_voice_preempts = {"t1": time.monotonic() - 3600.0}
        obj._qoder_current_id = "t2"

        main.JarvisLive._mark_qoder_voice_preempt(obj)

        self.assertNotIn("t1", obj._qoder_voice_preempts)
        self.assertIn("t2", obj._qoder_voice_preempts)


if __name__ == "__main__":
    unittest.main()
