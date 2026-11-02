"""Guided voice enrollment: the assistant asks, the owner repeats, we enroll."""
from __future__ import annotations

import time
import unittest

import main
from core import groq_stt


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
        if not self.records:
            self.records.append({"name": name, "owner": True,
                                 "authorized": True, "samples": 1})
        else:
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
        obj._cinema_mode_enabled = False   # never read the real state file
        obj._cinema_active = False
        obj._cinema_loaded = True
        obj.ui = FakeUI()
        obj.said = []
        obj.heard = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._log_heard = lambda text: obj.heard.append(text)
        # The speaker index is stubbed out in this suite; the voiceprint rule
        # itself is covered by the confirmation tests. Default: owner.
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            True, 0.88, "voz=Anthero")
        return obj

    def _sample(self, seconds=1.0):
        return b"\x01\x00" * int(16000 * seconds)

    def _start(self, obj, text="cadastra minha voz"):
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, text, self._sample()))
        self.assertIsNotNone(obj._voice_train)

    def test_trigger_starts_the_guided_flow(self):
        obj = self._obj()
        self._start(obj)
        self.assertTrue(any("Ok Google" in line for line in obj.said))
        self.assertTrue(any("Repita" in line for line in obj.said))

    def test_feature_request_does_not_start_enrollment(self):
        obj = self._obj()
        for line in (
                "implemente uma ferramenta para registrar minha voz",
                "cria uma feature de cadastro automático da minha voz",
                "quero uma ferramenta para cadastrar minha voz",
                "adiciona essa também"):
            self.assertFalse(main.JarvisLive._voice_train_gate(
                obj, line, self._sample()), line)
            self.assertIsNone(obj._voice_train, line)
        self.assertTrue(any("pedido de ferramenta" in line
                            for line in obj.ui.lines))

    def test_explicit_owner_request_starts_enrollment(self):
        for line in ("quero cadastrar minha voz",
                     "quero gravar minha voz",
                     "cadastrar minha voz de novo",
                     "cadastra minha voz"):
            obj = self._obj()
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, line, self._sample()), line)
            self.assertIsNotNone(obj._voice_train, line)

    def test_request_is_refused_without_owner_voice(self):
        obj = self._obj()
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            False, 0.19, "voz=TV")
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", self._sample()))
        self.assertIsNone(obj._voice_train)
        self.assertTrue(any("não confirmada como dono" in line
                            for line in obj.ui.lines))

    def test_five_samples_finish_and_enroll_the_owner(self):
        obj = self._obj()
        self._start(obj)
        for prompt in main._VOICE_TRAIN_PROMPTS:
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, prompt, self._sample()))
        self.assertIsNone(obj._voice_train)
        self.assertEqual(len(obj._speaker.enrolled),
                         len(main._VOICE_TRAIN_PROMPTS))
        self.assertTrue(all(name == "Anthero" and auth
                            for name, _sample, auth in obj._speaker.enrolled))
        self.assertIn("Pronto", obj.said[-1])

    def test_first_enrollment_needs_no_owner_profile(self):
        obj = self._obj()
        obj._speaker.records = []
        self._start(obj)
        for prompt in main._VOICE_TRAIN_PROMPTS:
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, prompt, self._sample()))
        self.assertIsNone(obj._voice_train)
        self.assertEqual(len(obj._speaker.enrolled),
                         len(main._VOICE_TRAIN_PROMPTS))

    def test_mismatched_phrase_is_rejected_without_progress(self):
        obj = self._obj()
        self._start(obj)
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, "Vai tomar no cú.", self._sample()))
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertEqual(obj._voice_train["done"], 0)
        self.assertEqual(obj._voice_train["fails"], 1)
        self.assertIn(main._VOICE_TRAIN_PROMPTS[0], obj.said[-1])
        self.assertLess(main._voice_phrase_score(
            "Vai tomar no cú.", main._VOICE_TRAIN_PROMPTS[0]),
            main._VOICE_TRAIN_MATCH_MIN)
        # The SAME phrase is accepted on the next try and only then progresses.
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, main._VOICE_TRAIN_PROMPTS[0], self._sample()))
        self.assertEqual(len(obj._speaker.enrolled), 1)
        self.assertEqual(obj._voice_train["done"], 1)

    def test_non_owner_voice_is_rejected_without_progress(self):
        obj = self._obj()
        self._start(obj)
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            False, 0.21, "voz=TV")
        self.assertTrue(main.JarvisLive._voice_train_gate(
            obj, main._VOICE_TRAIN_PROMPTS[0], self._sample()))
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertEqual(obj._voice_train["done"], 0)
        self.assertEqual(obj._voice_train["fails"], 1)

    def test_three_failures_cancel_the_flow(self):
        obj = self._obj()
        self._start(obj)
        for _ in range(3):
            self.assertTrue(main.JarvisLive._voice_train_gate(
                obj, "frase completamente diferente", self._sample()))
        self.assertIsNone(obj._voice_train)
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertIn("Cadastro de voz cancelado", obj.said[-1])

    def test_short_sample_is_asked_again(self):
        obj = self._obj()
        self._start(obj)
        main.JarvisLive._voice_train_gate(obj, "Gino", self._sample(0.3))
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertIn("curta", obj.said[-1])

    def test_cancel_stops_the_flow(self):
        obj = self._obj()
        self._start(obj)
        main.JarvisLive._voice_train_gate(obj, "cancela", self._sample())
        self.assertIsNone(obj._voice_train)
        self.assertIn("cancelado", obj.said[-1])

    def test_refusal_stops_the_flow(self):
        obj = self._obj()
        self._start(obj)
        main.JarvisLive._voice_train_gate(
            obj, "eu não pedi isso", self._sample())
        self.assertIsNone(obj._voice_train)
        self.assertIn("cancelado", obj.said[-1])

    def test_plain_speech_does_not_start_training(self):
        obj = self._obj()
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "que horas são?", self._sample()))
        self.assertIsNone(obj._voice_train)

    def test_cinema_mode_blocks_enrollment(self):
        obj = self._obj()
        obj._cinema_mode_enabled = True
        obj._cinema_active = True
        self.assertFalse(main.JarvisLive._voice_train_gate(
            obj, "cadastra minha voz", self._sample()))
        self.assertIsNone(obj._voice_train)


class _FakeGate:
    """Only the cache surface the audio-thread hook is allowed to touch."""

    def __init__(self, decision=None, raises=False):
        self._decision = decision
        self._raises = raises

    def last_decision(self):
        if self._raises:
            raise RuntimeError("gate quebrado")
        return dict(self._decision) if self._decision else None


class LegacyVoiceEnrollTest(unittest.TestCase):
    """Legacy auto-enroll ("última amostra") uses the guided gate's table:
    explicit first-person intent, NO feature vocabulary, owner-confirmed voice.
    """

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._voice_id_enabled = True
        obj._speaker = FakeSpeaker()
        obj._last_unknown_emb = [0.1, 0.2]
        obj._last_unknown_pcm = None
        obj._last_unknown_at = time.monotonic()
        obj._last_known_emb = None
        obj._last_known_at = 0.0
        obj.ui = FakeUI()
        obj.said = []
        obj._say_now = lambda text, log=True: obj.said.append(text)
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            True, 0.88, "voz=Anthero")
        return obj

    def _sample(self):
        return b"\x01\x00" * 16000

    def test_feature_request_never_fires_the_legacy_enroll(self):
        for line in (
                "implemente uma ferramenta para registrar minha voz",
                "cria um plugin para cadastrar minha voz",
                "cria uma funcionalidade de registro automático da minha voz"):
            obj = self._obj()
            self.assertFalse(main.JarvisLive._voice_enroll_gate(
                obj, line, self._sample()), line)
            self.assertEqual(obj._speaker.enrolled, [], line)
            self.assertTrue(any("pedido de ferramenta" in entry
                                for entry in obj.ui.lines), line)

    def test_explicit_first_person_request_still_enrolls_the_owner(self):
        for line in ("registra minha voz",
                     "salva essa variação como minha voz",
                     "essa voz é minha"):
            obj = self._obj()
            self.assertTrue(main.JarvisLive._voice_enroll_gate(
                obj, line, self._sample()), line)
            self.assertEqual(len(obj._speaker.enrolled), 1, line)
            self.assertEqual(obj._speaker.enrolled[0][0], "Anthero", line)
            self.assertTrue(obj._speaker.enrolled[0][2], line)

    def test_legacy_enroll_is_refused_for_a_non_owner_voice(self):
        obj = self._obj()
        obj._owner_speaker_confirmed = lambda _text, _pcm: (
            False, 0.19, "voz=TV")
        self.assertFalse(main.JarvisLive._voice_enroll_gate(
            obj, "registra minha voz", self._sample()))
        self.assertEqual(obj._speaker.enrolled, [])
        self.assertTrue(any("não confirmada como dono" in entry
                            for entry in obj.ui.lines))


class SttSpeakerHookTest(unittest.TestCase):
    """`current_speaker_is_owner` must be cheap, wired, and fail open."""

    def _obj(self, gate=None):
        obj = object.__new__(main.JarvisLive)
        obj._speaker = FakeSpeaker()
        obj._series_gate = gate
        return obj

    def _decision(self, verdict, score=0.0, seconds=10.0, speaker=None,
                  age=0.0):
        return {"verdict": verdict, "score": score, "seconds": seconds,
                "speaker": speaker, "at": time.monotonic() - age}

    def test_wiring_sets_the_callable_and_fails_open(self):
        obj = self._obj()
        listener = groq_stt.GroqListener(
            "", "test", "pt", on_text=lambda _t: None,
            preroll_s=1.5, max_utterance_s=15)
        try:
            main.JarvisLive._wire_stt_hooks(obj, listener)
            self.assertTrue(callable(listener.current_speaker_is_owner))
            self.assertTrue(listener.current_speaker_is_owner())
            listener._voiced = 10_000
            self.assertEqual(listener._soft_cap_blocks(),
                             listener._owner_cap_blocks)
        finally:
            listener.stop()

    def test_negative_evidence_tightens_the_segment_cap(self):
        gate = _FakeGate(self._decision("playback", score=0.1))
        obj = self._obj(gate)
        listener = groq_stt.GroqListener(
            "", "test", "pt", on_text=lambda _t: None,
            preroll_s=1.5, max_utterance_s=15)
        try:
            main.JarvisLive._wire_stt_hooks(obj, listener)
            self.assertFalse(listener.current_speaker_is_owner())
            listener._voiced = 10_000
            self.assertEqual(listener._soft_cap_blocks(),
                             listener._non_owner_cap_blocks)
        finally:
            listener.stop()

    def test_no_gate_or_no_decision_fails_open(self):
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(self._obj()))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(
            self._obj(_FakeGate(None))))

    def test_owner_and_gray_band_are_owner(self):
        obj = self._obj(_FakeGate(
            self._decision("accept", score=0.81, speaker="Anthero")))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(obj))
        obj = self._obj(_FakeGate(self._decision("unknown", score=0.40)))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(obj))
        obj = self._obj(_FakeGate(self._decision("unknown", score=0.05,
                                                 seconds=1.5)))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(obj))

    def test_known_non_owner_playback_and_unknown_are_not_owner(self):
        for decision in (
                self._decision("accept", score=0.90, speaker="Alice"),
                self._decision("unauthorized", score=0.90, speaker="Alice"),
                self._decision("playback", score=0.20),
                self._decision("unknown", score=0.10, seconds=12.0),
                self._decision("indecisive")):
            obj = self._obj(_FakeGate(decision))
            self.assertFalse(main.JarvisLive._stt_speaker_is_owner(obj),
                             decision)

    def test_stale_evidence_and_broken_gate_fail_open(self):
        obj = self._obj(_FakeGate(self._decision("playback", age=3600.0)))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(obj))
        obj = self._obj(_FakeGate(raises=True))
        self.assertTrue(main.JarvisLive._stt_speaker_is_owner(obj))


class FakeAutoSpeaker(FakeSpeaker):
    auto_enroll_enabled = True

    def __init__(self, result=None):
        super().__init__()
        self.calls = []
        self.result = result or {"enrolled": True, "reason": "",
                                 "name": "Anthero", "score": 0.87,
                                 "samples": 12, "max": 50}

    def auto_enroll(self, sample=None, *, embedding=None):
        self.calls.append((sample, embedding))
        return dict(self.result)


class AutoEnrollHookTest(unittest.TestCase):
    """The live turn feeds the expanded set after the gate accepts it."""

    def _obj(self, speaker):
        obj = object.__new__(main.JarvisLive)
        obj._voice_id_enabled = True
        obj._speaker = speaker
        obj.ui = FakeUI()
        return obj

    def test_accepted_utterance_is_logged_in_the_activity_style(self):
        speaker = FakeAutoSpeaker()
        obj = self._obj(speaker)
        analysis = {"primary_embedding": [1.0, 0.0], "primary_pcm": b"\x01\x00",
                    "unknown": None, "unauthorized": None}

        main.JarvisLive._auto_enroll_voice(obj, analysis, b"\x02\x00")

        self.assertEqual(len(speaker.calls), 1)
        self.assertEqual(obj.ui.lines,
                         ["SYS: auto-enroll Anthero +1 (12/50, sim=0.87)"])

    def test_mixed_window_without_an_isolated_segment_is_skipped(self):
        speaker = FakeAutoSpeaker()
        obj = self._obj(speaker)
        analysis = {"primary_embedding": [], "primary_pcm": b"",
                    "unknown": {"pcm": b"\x01\x00"}, "unauthorized": None}

        main.JarvisLive._auto_enroll_voice(obj, analysis, b"\x02\x00")

        self.assertEqual(speaker.calls, [])
        self.assertEqual(obj.ui.lines, [])

    def test_a_speaker_without_auto_enroll_is_harmless(self):
        obj = self._obj(FakeSpeaker())
        main.JarvisLive._auto_enroll_voice(
            obj, {"primary_embedding": [1.0, 0.0]}, b"\x02\x00")
        self.assertEqual(obj.ui.lines, [])


if __name__ == "__main__":
    unittest.main()
