"""Owner's rule (02/11/2026 ~19h): a failed turn is redone in SILENCE.

The old retry path spoke "Deu um tropeço aqui; já estou refazendo." over the
owner's question and made him wait for the proviso before the answer. These
tests pin the silence: no announcement may reach `_say_now`, the phrase may not
exist in main.py, the rule must live in the conversation skill, and the sidecar
must redo an abort-killed turn itself instead of reporting it as an error.
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path

import main

_BASE = Path(main.__file__).resolve().parent


class _Fake:
    """Minimal JarvisLive surface used by _qoder_on_error."""

    def __init__(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_lock = threading.RLock()
        obj._qoder_turns = {"t1": "que horas são"}
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


class SilentRetryTest(unittest.TestCase):
    def test_failed_turn_is_relaunched_without_any_spoken_warning(self):
        fake = _Fake()
        obj = fake.obj

        main.JarvisLive._qoder_on_error(obj, "t1", "provider falhou: 500")

        self.assertEqual(len(obj.retried), 1, "a fala tem que ser refeita")
        self.assertTrue(obj.retried[0][1].get("retry"))
        self.assertEqual(obj.said, [],
                         "o dono não pode ouvir proviso antes da resposta")

    def test_second_failure_does_not_announce_a_stumble(self):
        fake = _Fake()
        obj = fake.obj
        obj._qoder_retry_used = True

        main.JarvisLive._qoder_on_error(obj, "t1", "provider falhou: 500")

        spoken = [item[0] for item in obj.said if isinstance(item[0], str)]
        self.assertFalse(any("tropeço" in text.lower() for text in spoken))
        self.assertFalse(any("refazendo" in text.lower() for text in spoken))

    def test_main_py_has_no_stumble_announcement(self):
        source = (_BASE / "main.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("deu um tropeço", source)
        self.assertNotIn("já estou refazendo", source)

    def test_conversation_skill_bans_announced_stumbles(self):
        skill = (_BASE / "skills" / "conversation" / "SKILL.md").read_text(
            encoding="utf-8").lower()
        self.assertIn("proibido proviso", skill)
        self.assertIn("nunca anuncie tropeço", skill)
        self.assertIn("em silêncio", skill)

    def test_sidecar_redoes_abort_killed_turn_silently(self):
        sidecar = (_BASE / "qoder_live" / "index.mjs").read_text(
            encoding="utf-8")
        self.assertIn("refazendo em silêncio", sidecar)
        self.assertIn("ABORT_SETTLE_MS", sidecar)


if __name__ == "__main__":
    unittest.main()
