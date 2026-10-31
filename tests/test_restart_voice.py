"""Voice gates for assistant/PC restart (no real restart)."""
from __future__ import annotations

import unittest

import main


class FakeRestart:
    def __init__(self):
        self.pending_value = None
        self.requested = []
        self.confirmed = []
        self.cancelled = 0

    def pending(self):
        return self.pending_value

    def request_assistant(self, reason=""):
        self.requested.append(("assistant", reason))
        self.pending_value = {"kind": "assistant", "phrase": "confirmo reiniciar"}
        return "pending"

    def request_pc(self, reason=""):
        self.requested.append(("pc", reason))
        self.pending_value = {"kind": "pc", "phrase": "confirmo reiniciar"}
        return "pending"

    def confirm(self, phrase):
        self.confirmed.append(phrase)
        self.pending_value = None
        return "Restarting the assistant now."

    def cancel(self):
        self.cancelled += 1
        self.pending_value = None
        return "cancelled"


class FakeSpeaker:
    owner_name = "Anthero"

    def __init__(self, owner=True):
        self.owner = owner

    def embedding(self, _pcm):
        return [1.0, 0.0] if self.owner else [0.0, 1.0]

    def identify(self, _emb):
        if self.owner:
            return {"name": "Anthero", "score": 1.0, "known": True,
                    "authorized": True}
        return {"name": None, "score": 0.0, "known": False,
                "authorized": False}


class RestartVoiceTest(unittest.TestCase):
    def _obj(self, owner=True, pending=None):
        obj = object.__new__(main.JarvisLive)
        obj._restart = FakeRestart()
        obj._restart.pending_value = pending
        obj._restart_enabled = True
        obj._power = None
        obj._speaker = FakeSpeaker(owner)
        obj._qoder_live = None
        obj._qoder_ignore = False
        obj._current_pcm = b"\x01\x00" * 16000
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj.interrupt = lambda: None
        return obj

    def test_owner_requests_assistant_and_pc(self):
        obj = self._obj()
        self.assertTrue(main.JarvisLive._restart_gate(obj, "Reinicie você"))
        self.assertEqual(obj._restart.requested[-1][0], "assistant")
        obj2 = self._obj()
        self.assertTrue(main.JarvisLive._restart_gate(obj2, "Reinicie o computador"))
        self.assertEqual(obj2._restart.requested[-1][0], "pc")

    def test_guest_cannot_request(self):
        obj = self._obj(owner=False)
        self.assertTrue(main.JarvisLive._restart_gate(obj, "Reinicie você"))
        self.assertEqual(obj._restart.requested, [])
        self.assertIn("Só o dono", obj.said[0])

    def test_confirmation_and_cancel(self):
        obj = self._obj(pending={"kind": "assistant", "phrase": "confirmo reiniciar"})
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, "confirmo reiniciar", b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.confirmed, ["confirmo reiniciar"])
        obj2 = self._obj(pending={"kind": "pc", "phrase": "confirmo reiniciar"})
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj2, "cancela", b"\x01\x00" * 16000))
        self.assertEqual(obj2._restart.cancelled, 1)

    def test_guest_cannot_confirm(self):
        obj = self._obj(owner=False, pending={"kind": "assistant", "phrase": "confirmo reiniciar"})
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, "confirmo reiniciar", b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.confirmed, [])
        self.assertEqual(obj._restart.cancelled, 0)   # kept for a retry
        self.assertIn("Não reconheci", obj.said[0])

    def test_single_owner_fallback_accepts_the_exact_phrase(self):
        obj = self._obj(owner=False,
                        pending={"kind": "assistant",
                                 "phrase": "confirmo reiniciar"})

        class SingleOwner(FakeSpeaker):
            def list(self):
                return [{"name": "Anthero", "owner": True, "authorized": True}]

        obj._speaker = SingleOwner(owner=False)
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, "confirmo reiniciar", b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.confirmed, ["confirmo reiniciar"])

    def test_unrelated_speech_keeps_the_request_pending(self):
        obj = self._obj(pending={"kind": "assistant", "phrase": "confirmo reiniciar"})
        self.assertFalse(main.JarvisLive._handle_restart_confirmation(
            obj, "que horas são?", b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.cancelled, 0)
        self.assertEqual(obj._restart.confirmed, [])

    def test_pc_confirmation_still_needs_the_voiceprint(self):
        obj = self._obj(owner=False,
                        pending={"kind": "pc", "phrase": "confirmo reiniciar"})

        class SingleOwner(FakeSpeaker):
            def list(self):
                return [{"name": "Anthero", "owner": True, "authorized": True}]

        obj._speaker = SingleOwner(owner=False)
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, "confirmo reiniciar", b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.confirmed, [])
        self.assertEqual(obj._restart.cancelled, 0)


if __name__ == "__main__":
    unittest.main()
