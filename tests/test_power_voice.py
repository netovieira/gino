"""Voice gates for shutdown/suspend/lock/unlock (no real power commands)."""
from __future__ import annotations

import unittest

import main


class FakePower:
    def __init__(self):
        self.pending_value = None
        self.requested = []
        self.confirmed = []
        self.cancelled = 0
        self.locked = 0

    def pending(self):
        return self.pending_value

    def request(self, kind, reason=""):
        self.requested.append((kind, reason))
        self.pending_value = {"kind": kind, "phrase": f"confirmo {kind}"}
        return "pending"

    def confirm(self, phrase):
        self.confirmed.append(phrase)
        self.pending_value = None
        return f"executado: {phrase}"

    def cancel(self):
        self.cancelled += 1
        self.pending_value = None
        return "cancelado"

    def lock(self):
        self.locked += 1
        return "Tela bloqueada."


class FakeSpeaker:
    owner_name = "Anthero"

    def __init__(self, owner=True):
        self.owner = owner

    def embedding(self, _pcm):
        return [1.0, 0.0] if self.owner else [0.0, 1.0]

    def identify(self, _emb, device=None):
        if self.owner:
            return {"name": "Anthero", "score": 1.0, "known": True,
                    "authorized": True}
        return {"name": None, "score": 0.0, "known": False,
                "authorized": False}


class PowerVoiceTest(unittest.TestCase):
    def _obj(self, owner=True, pending=None):
        obj = object.__new__(main.JarvisLive)
        obj._power = FakePower()
        obj._power.pending_value = pending
        obj._speaker = FakeSpeaker(owner)
        obj._restart = None
        obj._qoder_live = None
        obj._qoder_ignore = False
        obj._current_pcm = b"\x01\x00" * 16000
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj.interrupt = lambda: None
        return obj

    def test_owner_requests_each_action(self):
        for text, kind in [
            ("Desliga o computador", "shutdown"),
            ("Suspende o computador", "suspend"),
            ("Desbloqueia a tela", "unlock"),
        ]:
            obj = self._obj()
            assert main.JarvisLive._power_gate(obj, text)
            self.assertEqual(obj._power.requested[-1][0], kind)
            self.assertTrue(obj.said)

    def test_lock_is_immediate(self):
        obj = self._obj()
        self.assertTrue(main.JarvisLive._power_gate(obj, "Bloqueia a tela"))
        self.assertEqual(obj._power.locked, 1)
        self.assertEqual(obj._power.requested, [])

    def test_guest_cannot_request_power_action(self):
        obj = self._obj(owner=False)
        self.assertTrue(main.JarvisLive._power_gate(obj, "Desliga o computador"))
        self.assertEqual(obj._power.requested, [])
        self.assertIn("Só o dono", obj.said[0])

    def test_confirmation_and_cancel(self):
        obj = self._obj(pending={"kind": "shutdown", "phrase": "confirmo desligar"})
        self.assertTrue(main.JarvisLive._handle_power_confirmation(
            obj, "confirmo desligar", b"\x01\x00" * 16000))
        self.assertEqual(obj._power.confirmed, ["confirmo desligar"])
        obj2 = self._obj(pending={"kind": "suspend", "phrase": "confirmo suspender"})
        self.assertTrue(main.JarvisLive._handle_power_confirmation(
            obj2, "cancela", b"\x01\x00" * 16000))
        self.assertEqual(obj2._power.cancelled, 1)

    def test_guest_cannot_confirm(self):
        obj = self._obj(owner=False, pending={"kind": "shutdown", "phrase": "confirmo desligar"})
        self.assertTrue(main.JarvisLive._handle_power_confirmation(
            obj, "confirmo desligar", b"\x01\x00" * 16000))
        self.assertEqual(obj._power.confirmed, [])
        self.assertEqual(obj._power.cancelled, 1)


if __name__ == "__main__":
    unittest.main()
