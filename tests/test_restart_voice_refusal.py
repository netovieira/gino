"""Recusa de confirmação por voz devolve motivo e prazo de expiração (defeito 2).

Em 05/11/2026 o dono disse "confirmo reiniciar" quatro vezes; cada recusa do
voiceprint foi respondida com uma frase genérica sem prazo, e o pedido
morreu em silêncio. A recusa agora tem que dizer: voz não reconhecida, pode
repetir quando for reconhecido, e o pedido expira às HH:MM. A política de
segurança não muda — reinício continua só com a fala do dono (senha digitada
é decisão pendente dele).
"""
from __future__ import annotations

import re
import unittest

import main


class FakeRestart:
    def __init__(self, pending):
        self._pending = pending
        self.confirmed: list[str] = []

    def pending(self):
        return self._pending

    def confirm(self, phrase):
        self.confirmed.append(phrase)
        return "Nothing was done."

    def cancel(self):
        return "cancelled"


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


def _obj(owner=True, expires_in=600):
    obj = object.__new__(main.JarvisLive)
    obj._restart = FakeRestart({"kind": "assistant",
                                "phrase": "confirmo reiniciar",
                                "expires_in": expires_in})
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


class RestartVoiceRefusalTest(unittest.TestCase):
    PHRASE = "confirmo reiniciar"

    def test_refusal_names_the_reason_the_retry_and_the_deadline(self):
        obj = _obj(owner=False)
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, self.PHRASE, b"\x01\x00" * 16000))
        said = " ".join(obj.said)
        self.assertIn("Não reconheci sua voz como dono agora", said)
        self.assertIn("repetir", said)
        self.assertIn("expira às", said)
        self.assertRegex(said, r"expira às \d{2}:\d{2}")
        # nada foi confirmado nem cancelado — o pedido segue vivo para repetição
        self.assertEqual(obj._restart.confirmed, [])

    def test_refusal_without_expires_in_still_speaks_a_deadline(self):
        obj = _obj(owner=False)
        obj._restart._pending = {"kind": "assistant",
                                 "phrase": self.PHRASE}  # sem expires_in
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, self.PHRASE, b"\x01\x00" * 16000))
        self.assertRegex(" ".join(obj.said), r"expira às \d{2}:\d{2}")

    def test_owner_voice_still_confirms_unchanged(self):
        obj = _obj(owner=True)
        self.assertTrue(main.JarvisLive._handle_restart_confirmation(
            obj, self.PHRASE, b"\x01\x00" * 16000))
        self.assertEqual(obj._restart.confirmed, [self.PHRASE])


if __name__ == "__main__":
    unittest.main()
