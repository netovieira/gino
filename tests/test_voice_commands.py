"""Voice-management commands handled before the brain (no audio/Qt)."""
from __future__ import annotations

import time
import unittest

import main


class FakeSpeaker:
    owner_name = "Anthero"

    def __init__(self, records=None):
        self.records = records or [
            {"name": "Anthero", "owner": True, "authorized": True, "samples": 3},
        ]
        self.authorized = []
        self.removed = []
        self.enrolled = []

    def list(self):
        return list(self.records)

    def authorize(self, name):
        self.authorized.append(name)
        return {"name": name, "authorized": True}

    def remove(self, name):
        self.removed.append(name)
        return True

    def enroll(self, name, sample, authorized=False):
        self.enrolled.append((name, sample, authorized))
        return {"name": name, "authorized": authorized}


class VoiceCommandsTest(unittest.TestCase):
    def _obj(self, speaker, unknown_emb=None, unknown_pcm=None):
        obj = object.__new__(main.JarvisLive)
        obj._voice_id_enabled = True
        obj._speaker = speaker
        obj._last_unknown_emb = unknown_emb
        obj._last_unknown_pcm = unknown_pcm
        obj._last_unknown_at = time.monotonic()
        obj._last_known_emb = None
        obj._last_known_at = 0.0
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        # The legacy self-enroll path now requires a voiceprint confirmed as
        # the owner (same decision table as the guided gate). The confirmation
        # itself is covered in test_voice_training.py.
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            True, 0.9, "voz=Anthero")
        return obj

    def test_lists_known_voices(self):
        obj = self._obj(FakeSpeaker())
        hit = main.JarvisLive._voice_enroll_gate(obj, "Quais vozes você conhece?")
        self.assertTrue(hit)
        self.assertIn("Anthero (dono)", obj.said[0])

    def test_authorizes_a_known_voice(self):
        sp = FakeSpeaker()
        obj = self._obj(sp)
        hit = main.JarvisLive._voice_enroll_gate(obj, "Autoriza a Heloisa")
        self.assertTrue(hit)
        self.assertEqual(sp.authorized, ["Heloisa"])

    def test_forgets_a_voice(self):
        sp = FakeSpeaker()
        obj = self._obj(sp)
        hit = main.JarvisLive._voice_enroll_gate(obj, "Esquece a voz da Heloisa")
        self.assertTrue(hit)
        self.assertEqual(sp.removed, ["Heloisa"])

    def test_existing_name_is_authorized_not_re_enrolled(self):
        sp = FakeSpeaker([
            {"name": "Anthero", "owner": True, "authorized": True},
            {"name": "Heloisa", "owner": False, "authorized": False},
        ])
        obj = self._obj(sp)
        hit = main.JarvisLive._voice_enroll_gate(obj, "Pode responder a Heloisa")
        self.assertTrue(hit)
        self.assertEqual(sp.authorized, ["Heloisa"])
        self.assertEqual(sp.enrolled, [])

    def test_unknown_voice_is_enrolled_from_last_embedding(self):
        sp = FakeSpeaker()
        obj = self._obj(sp, unknown_emb=[0.1, 0.2])
        hit = main.JarvisLive._voice_enroll_gate(obj, "Essa é a Heloisa")
        self.assertTrue(hit)
        self.assertEqual(sp.enrolled[0][0], "Heloisa")
        self.assertTrue(sp.enrolled[0][2])
        self.assertIsNone(obj._last_unknown_emb)

    def test_no_sample_asks_for_a_voice(self):
        sp = FakeSpeaker()
        obj = self._obj(sp)
        hit = main.JarvisLive._voice_enroll_gate(obj, "Essa é a Heloisa")
        self.assertTrue(hit)
        self.assertEqual(sp.enrolled, [])
        self.assertIn("Não tenho a voz", obj.said[0])

    def test_owner_self_enrolls_with_recent_sample(self):
        sp = FakeSpeaker()
        obj = self._obj(sp, unknown_emb=[0.1, 0.2])
        hit = main.JarvisLive._voice_enroll_gate(
            obj, "A registro dessa voz também como minha",
            b"\x01\x00" * 8000)
        self.assertTrue(hit)
        self.assertEqual(sp.enrolled[0][0], "Anthero")
        self.assertTrue(sp.enrolled[0][2])

    def test_self_enroll_rejects_stale_sample(self):
        sp = FakeSpeaker()
        obj = self._obj(sp, unknown_emb=[0.1, 0.2])
        obj._last_unknown_at = 0.0
        hit = main.JarvisLive._voice_enroll_gate(
            obj, "Registra minha voz", b"\x01\x00" * 8000)
        self.assertTrue(hit)
        self.assertEqual(sp.enrolled, [])
        self.assertIn("Não tenho", obj.said[0])

    def test_self_enroll_regex_ignores_plain_comments(self):
        self.assertIsNone(main._VOICE_SELF_ENROLL_RE.search("A voz está baixa hoje"))


if __name__ == "__main__":
    unittest.main()
