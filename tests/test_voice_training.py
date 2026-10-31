"""Guided voice enrollment: the assistant asks, the owner repeats, we enroll."""
from __future__ import annotations

import unittest

import main


class FakeSpeaker:
    owner_name = "Anthero"
    sample_rate = 16000

    def __init__(self):
        self.enrolled = []
        self.records = [{"name": "Anthero", "owner": True, "authorized": True,
                         "samples": 3}]

    def list(self):
        return list(self.records)

    def embedding(self, _pcm):
        return [1.0, 0.0]

    def enroll(self, name, sample, authorized=False):
        self.enrolled.append((name, sample, authorized))
        self.records[0]["samples"] += 1
        return self.records[0]


class FakeUI:
    def __init__(self):
        self.lines = []

    def write_log(self, text):
        self.lines.append(text)


class VoiceTrainingTest(unittest.TestCase):
    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._voice_id_enabled = True
        obj._speaker = FakeSpeaker()
        obj._voice_train = None
        obj._voice_train_offered = False
        obj.ui = FakeUI()
        obj.said = []
        obj.heard = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._log_heard = lambda text: obj.heard.append(text)
        return obj

    def _sample(self, seconds=1.0):
        return b"\x01\x00" * int(16000 * seconds)

    def test_trigger_starts_the_guided_flow(self):
        obj = self._obj()
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", self._sample()))
        self.assertIsNotNone(obj._voice_train)
        self.assertTrue(any("Ok Google" in line for line in obj.said))
        self.assertTrue(any("Repita" in line for line in obj.said))

    def test_five_samples_finish_and_enroll_the_owner(self):
        obj = self._obj()
        main.JarvisLive._voice_train_gate(obj, "cadastra minha voz", self._sample())
        for _ in range(len(main._VOICE_TRAIN_PROMPTS)):
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, "Gino, sou eu.", self._sample()))
        self.assertIsNone(obj._voice_train)
        self.assertEqual(len(obj._speaker.enrolled),
                         len(main._VOICE_TRAIN_PROMPTS))
        self.assertTrue(all(name == "Anthero" and auth
                            for name, _sample, auth in obj._speaker.enrolled))
        self.assertIn("Pronto", obj.said[-1])

    def test_short_sample_is_asked_again(self):
        obj = self._obj()
        main.JarvisLive._voice_train_gate(obj, "cadastra minha voz", self._sample())
        main.JarvisLive._voice_train_gate(obj, "Gino", self._sample(0.3))
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertIn("curta", obj.said[-1])

    def test_cancel_stops_the_flow(self):
        obj = self._obj()
        main.JarvisLive._voice_train_gate(obj, "cadastra minha voz", self._sample())
        main.JarvisLive._voice_train_gate(obj, "cancela", self._sample())
        self.assertIsNone(obj._voice_train)
        self.assertIn("cancelado", obj.said[-1])

    def test_plain_speech_does_not_start_training(self):
        obj = self._obj()
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "que horas são?", self._sample()))
        self.assertIsNone(obj._voice_train)


if __name__ == "__main__":
    unittest.main()
