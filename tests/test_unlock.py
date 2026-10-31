"""Tests for the opt-in delegated unlock (no real keyring/lock screen)."""
from __future__ import annotations

import unittest

from core import unlock


class UnlockTest(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "session_locked": unlock.session_locked,
            "get_secret": unlock.get_secret,
            "type_secret": unlock.type_secret,
            "_run": unlock._run,
        }

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(unlock, name, fn)

    def test_logind_unlocks_without_typing(self):
        states = iter([True, False])
        unlock.session_locked = lambda: next(states, False)
        unlock._run = lambda *a, **k: None
        unlock.get_secret = lambda: "pw"
        typed = []
        unlock.type_secret = lambda s: typed.append(s)
        self.assertIn("desbloqueada", unlock.unlock())
        self.assertEqual(typed, [])

    def test_typing_is_used_when_logind_leaves_it_locked(self):
        states = iter([True, True, False])
        unlock.session_locked = lambda: next(states, False)
        calls = []
        unlock._run = lambda cmd, **k: calls.append(cmd)
        unlock.get_secret = lambda: "segredo"
        typed = []
        unlock.type_secret = lambda s: typed.append(s)
        self.assertIn("desbloqueada", unlock.unlock())
        self.assertEqual(typed, ["segredo"])
        self.assertTrue(any(cmd[:2] == ["loginctl", "unlock-session"] for cmd in calls))

    def test_missing_credential_is_reported(self):
        states = iter([True, True])
        unlock.session_locked = lambda: next(states, True)
        unlock._run = lambda *a, **k: None
        unlock.get_secret = lambda: ""
        with self.assertRaises(RuntimeError) as ctx:
            unlock.unlock()
        self.assertIn("não configurado", str(ctx.exception))

    def test_unknown_lock_state_never_types_the_secret(self):
        unlock.session_locked = lambda: None
        unlock._run = lambda *a, **k: None
        unlock.get_secret = lambda: "segredo"
        typed = []
        unlock.type_secret = lambda s: typed.append(s)
        with self.assertRaises(RuntimeError) as ctx:
            unlock.unlock()
        self.assertIn("não consegui confirmar", str(ctx.exception))
        self.assertEqual(typed, [])

    def test_secret_is_sent_on_stdin_not_argv(self):
        run = []
        unlock._run = lambda cmd, input_text=None, **k: run.append((cmd, input_text))
        orig_which = unlock.shutil.which
        unlock.shutil.which = lambda name: "/usr/bin/ydotool" if name == "ydotool" else None
        try:
            unlock.type_secret("minha-senha")
        finally:
            unlock.shutil.which = orig_which
        self.assertEqual(run[0][0][:4], ["ydotool", "type", "--file", "-"])
        self.assertEqual(run[0][1], "minha-senha")
        self.assertNotIn("minha-senha", " ".join(run[0][0]))

    def test_configured_uses_secret_lookup(self):
        unlock.get_secret = lambda: "x"
        self.assertTrue(unlock.configured())
        unlock.get_secret = lambda: ""
        self.assertFalse(unlock.configured())


if __name__ == "__main__":
    unittest.main()
