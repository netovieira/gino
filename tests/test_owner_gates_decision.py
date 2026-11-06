"""The main.py owner gates wired to core.owner_decision.

No microphone and no PortAudio: a fake speaker answers like SpeakerId.identify
(and, importantly, ACCEPTS ``device=``), while an injected cohort registry pins
the calibrated limit. What is fixed here:

  * a privileged gate refuses ``no_profile`` even with a perfect score;
  * a privileged gate accepts the owner above the cohort limit;
  * another speaker is refused by the same gate;
  * no audio (typed text) never confirms;
  * the SpeakerState is one object per process and its hysteresis survives a
    single contradicting phrase;
  * no_profile is announced once, not once per phrase, and normal speech is
    still allowed by the decision (only the owner-only confirmation refuses);
  * enrolling a voice regenerates cohorts.json from the live index.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import main
from core import owner_decision as od
from core import voice_cohorts as vc

OWNER = "Anthero"
HW_C920 = "alsa-input-usb-046d-hd-pro-webcam-c920-test"
HW_EDIFIER = "alsa-input-usb-edifier-w820nb-test"


def _unit(value) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    return arr / np.linalg.norm(arr)


def _near(base: np.ndarray, seed: int, mix: float = 0.02) -> list[float]:
    rng = np.random.default_rng(seed)
    return _unit(base + mix * rng.normal(size=base.shape)).tolist()


def _index() -> dict:
    e0 = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    e1 = np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    guest_base = 0.3 * e0 + np.sqrt(1.0 - 0.09) * e1
    return {
        "owner": OWNER,
        "device_aliases": {"pc": HW_C920},
        "speakers": {
            "anthero": {
                "name": OWNER, "authorized": True,
                "profiles": {
                    HW_C920: {"samples": [_near(e0, i) for i in range(5)]},
                    "celular": {"samples": [_near(e0, 200 + i, 0.03)
                                            for i in range(3)]},
                },
            },
            "eloisa": {
                "name": "Eloisa", "authorized": True,
                "profiles": {
                    HW_EDIFIER: {"samples": [_near(guest_base, 100 + i)
                                             for i in range(5)]},
                },
            },
        },
    }


def _registry() -> vc.CohortRegistry:
    return vc.CohortRegistry.from_index(_index())


class FakeUI:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def write_log(self, text) -> None:
        self.logs.append(str(text))


class FakeSpeaker:
    """SpeakerId stand-in; accepts every keyword the main.py gates use."""

    def __init__(self, *, name=OWNER, score=0.72, known=True, authorized=True,
                 verdict="known", device=HW_C920) -> None:
        self.owner_name = OWNER
        self.name = name
        self.score = score
        self.known = known
        self.authorized = authorized
        self.verdict = verdict
        self.device = device
        self.embeddings = 0
        self.identify_calls: list[dict] = []
        self.enroll_calls: list[tuple] = []

    def embedding(self, _pcm):
        self.embeddings += 1
        return [1.0, 0.0]

    def identify(self, _emb, include_auto=False, voiced_seconds=None,
                 device=None):
        self.identify_calls.append({"device": device,
                                    "include_auto": include_auto,
                                    "voiced_seconds": voiced_seconds})
        return {
            "name": self.name if self.known else None,
            "score": self.score,
            "sample_score": self.score,
            "effective_score": self.score,
            "known": self.known,
            "authorized": self.authorized,
            "verdict": self.verdict,
            "device": self.device,
        }

    def list(self):
        return [{"name": OWNER, "owner": True, "authorized": True}]

    def enroll(self, name, sample, authorized=False, device=None):
        self.enroll_calls.append((name, sample, authorized, device))
        return {"name": name, "samples": 1}


def _obj(speaker=None, registry=None) -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj._speaker = speaker if speaker is not None else FakeSpeaker()
    obj._voice_registry = registry if registry is not None else _registry()
    obj._owner_speaker_state = od.SpeakerState()
    obj._series_gate = None
    obj._series_decision_for = lambda pcm: None
    obj._multi_mic = None
    obj.ui = FakeUI()
    obj.said: list[str] = []
    obj.audit: list[tuple[str, str]] = []
    obj._say_now = lambda text, **kw: obj.said.append(str(text))
    obj._log_heard = lambda text: None
    obj._voice_audit = lambda message, detail="", **kw: obj.audit.append(
        (str(message), str(detail)))
    return obj


def _no_profile_speaker() -> FakeSpeaker:
    return FakeSpeaker(name=None, score=0.99, known=False, authorized=False,
                       verdict="no_profile", device=HW_EDIFIER)


class PrivilegedGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def test_no_profile_refuses_the_privilege_even_with_a_perfect_score(self):
        obj = _obj(_no_profile_speaker(), self.registry)
        self.assertFalse(main.JarvisLive._owner_voice_ok(
            obj, b"owner-voice", device=HW_EDIFIER))
        self.assertFalse(any("aberto" in message for message, _ in obj.audit))
        self.assertTrue(any("sem perfil" in detail for _, detail in obj.audit))

    def test_owner_above_the_cohort_limit_opens_the_gate(self):
        limit = od.threshold_for(HW_C920, registry=self.registry)
        self.assertTrue(limit.calibrated)
        self.assertLess(limit.threshold, 0.72)
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        self.assertTrue(main.JarvisLive._owner_voice_ok(
            obj, b"owner-voice", device=HW_C920))
        self.assertTrue(any("aberto" in message for message, _ in obj.audit))

    def test_owner_below_the_cohort_limit_does_not_open_the_gate(self):
        obj = _obj(FakeSpeaker(score=0.50, device=HW_C920), self.registry)
        self.assertFalse(main.JarvisLive._owner_voice_ok(
            obj, b"owner-voice", device=HW_C920))

    def test_another_speaker_is_refused(self):
        obj = _obj(FakeSpeaker(name="Eloisa", score=0.95, device=HW_C920),
                   self.registry)
        self.assertFalse(main.JarvisLive._owner_voice_ok(
            obj, b"guest-voice", device=HW_C920))

    def test_text_without_audio_never_confirms(self):
        speaker = FakeSpeaker()
        obj = _obj(speaker, self.registry)
        self.assertFalse(main.JarvisLive._owner_voice_ok(obj, None))
        self.assertEqual(speaker.embeddings, 0)

    def test_positive_owner_match_leaves_identification_trace(self):
        # Passo 5.1.1 com rastro: voiceprint POSITIVO do dono imprime o
        # marcador com o papel e o perfil do microfone — nunca amostra,
        # telefone ou id de contato. O caminho é o real: _owner_voice_ok →
        # _owner_verdict → _owner_decision_for_match.
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(main.JarvisLive._owner_voice_ok(
                obj, b"owner-voice", device=HW_C920))
        out = buf.getvalue()
        self.assertIn("[Voz] dono confirmado — identidade registrada em "
                      f"memória perfil={HW_C920}", out)
        self.assertNotIn(OWNER, out)     # nem o nome do dono sai no rastro

    def test_refusal_never_leaves_the_positive_trace(self):
        # Só o match positivo imprime: voz conhecida não-dono e recusa
        # continuam sem o marcador (contraprova do rastro).
        obj = _obj(FakeSpeaker(name="Eloisa", score=0.95, device=HW_C920),
                   self.registry)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(main.JarvisLive._owner_voice_ok(
                obj, b"guest-voice", device=HW_C920))
        self.assertNotIn("dono confirmado", buf.getvalue())

    def test_no_profile_never_leaves_the_positive_trace(self):
        speaker = _no_profile_speaker()
        obj = _obj(speaker, self.registry)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(main.JarvisLive._owner_voice_ok(
                obj, b"ghost-voice", device=HW_EDIFIER))
        self.assertNotIn("dono confirmado", buf.getvalue())


class SharedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def test_one_state_object_is_reused_across_phrases(self):
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        state = obj._owner_speaker_state
        for i in range(2):
            self.assertTrue(main.JarvisLive._owner_voice_ok(
                obj, b"owner-" + bytes([i]), device=HW_C920))
        self.assertIs(obj._owner_speaker_state, state)
        self.assertIs(main.JarvisLive._owner_state(obj), state)
        self.assertEqual(state.state, od.SpeakerState.PRESENT)

    def test_one_contradiction_does_not_invert_the_state(self):
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        for i in range(2):
            main.JarvisLive._owner_voice_ok(
                obj, b"owner-" + bytes([i]), device=HW_C920)
        self.assertEqual(obj._owner_speaker_state.state,
                         od.SpeakerState.PRESENT)

        obj._speaker = FakeSpeaker(name="Eloisa", score=0.95,
                                   device=HW_C920)
        main.JarvisLive._owner_voice_ok(obj, b"guest-1", device=HW_C920)
        self.assertEqual(obj._owner_speaker_state.state,
                         od.SpeakerState.PRESENT)
        main.JarvisLive._owner_voice_ok(obj, b"guest-2", device=HW_C920)
        self.assertEqual(obj._owner_speaker_state.state,
                         od.SpeakerState.ABSENT)


class NoProfileConversationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _registry()

    def test_conversational_decision_allows_normal_speech_but_not_privilege(
            self):
        speaker = _no_profile_speaker()
        obj = _obj(speaker, self.registry)
        match = speaker.identify([1.0, 0.0], device=HW_EDIFIER)
        verdict = obj._owner_decision_for_match(
            match, device=HW_EDIFIER, privileged=False)
        self.assertEqual(verdict.profile_state, "no_profile")
        self.assertIs(verdict.action, od.Action.NORMAL)
        self.assertFalse(verdict.privileged)

    def test_conversational_gate_refuses_the_owner_claim_and_warns_once(
            self):
        obj = _obj(_no_profile_speaker(), self.registry)
        for i in range(2):
            confirmed, score, detail = (
                main.JarvisLive._owner_speaker_confirmed(
                    obj, "Gino, tudo bem?", b"clip-" + bytes([i]),
                    device=HW_EDIFIER))
            self.assertFalse(confirmed)
            self.assertEqual(score, 0.0)
            self.assertIn("sem cadastro", detail)
        notices = [line for line in obj.said
                   if "Não tenho cadastro" in line]
        self.assertEqual(len(notices), 1, obj.said)


class SpeakerOrderIdentityTest(unittest.TestCase):
    """The direct-order voiceprint now answers through owner_decision.decide."""

    def setUp(self) -> None:
        self.registry = _registry()

    def test_no_profile_never_opens_a_direct_order(self):
        obj = _obj(_no_profile_speaker(), self.registry)
        ok, known, detail = main.JarvisLive._speaker_order_identity(
            obj, b"owner-voice", device=HW_EDIFIER)
        self.assertFalse(ok)
        self.assertFalse(known)
        self.assertIn("sem perfil", detail)

    def test_another_speaker_never_opens_a_direct_order(self):
        obj = _obj(FakeSpeaker(name="Eloisa", score=0.95, device=HW_C920),
                   self.registry)
        ok, known, detail = main.JarvisLive._speaker_order_identity(
            obj, b"guest-voice", device=HW_C920)
        self.assertFalse(ok)
        self.assertTrue(known, "convidado cadastrado deve poder escalar")
        self.assertIn("não o dono", detail)

    def test_owner_above_the_cohort_limit_opens_a_direct_order(self):
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        ok, known, detail = main.JarvisLive._speaker_order_identity(
            obj, b"owner-voice", device=HW_C920)
        self.assertTrue(ok, detail)
        self.assertTrue(known)
        self.assertIn("dono confirmado", detail)


class CachedSeriesGateTest(unittest.TestCase):
    """A cached SeriesGate "accept" must not skip the single owner decision."""

    def setUp(self) -> None:
        self.registry = _registry()

    def test_cached_accept_of_another_speaker_never_confirms(self):
        obj = _obj(FakeSpeaker(), self.registry)
        obj._series_decision_for = lambda pcm: {
            "verdict": "accept", "speaker": "Eloisa", "score": 0.99,
            "device": HW_C920}
        confirmed, _score, detail = main.JarvisLive._owner_speaker_confirmed(
            obj, "pode", b"clip")
        self.assertFalse(confirmed)
        self.assertIn("não o dono", detail)

    def test_cached_accept_does_not_skip_no_profile(self):
        obj = _obj(_no_profile_speaker(), self.registry)
        obj._series_decision_for = lambda pcm: {
            "verdict": "accept", "speaker": OWNER, "score": 0.99,
            "device": HW_EDIFIER}
        confirmed, score, detail = main.JarvisLive._owner_speaker_confirmed(
            obj, "Gino, tudo bem?", b"clip")
        self.assertFalse(confirmed)
        self.assertEqual(score, 0.0)
        self.assertIn("sem cadastro", detail)

    def test_cached_accept_still_confirms_the_real_owner_above_limit(self):
        obj = _obj(FakeSpeaker(score=0.72, device=HW_C920), self.registry)
        obj._series_decision_for = lambda pcm: {
            "verdict": "accept", "speaker": OWNER, "score": 0.72,
            "device": HW_C920}
        confirmed, _score, detail = main.JarvisLive._owner_speaker_confirmed(
            obj, "pode", b"clip")
        self.assertTrue(confirmed, detail)

    def test_cached_accept_does_not_wake_on_no_profile(self):
        obj = _obj(_no_profile_speaker(), self.registry)
        obj._series_decision_for = lambda pcm: {
            "verdict": "accept", "speaker": OWNER, "score": 0.99,
            "device": HW_EDIFIER}
        ok, _score, detail = main.JarvisLive._bare_address_evidence(
            obj, b"owner-voice")
        self.assertFalse(ok)
        self.assertIn("sem cadastro", detail)


class CohortRefreshTest(unittest.TestCase):
    def test_refresh_regenerates_cohorts_json_from_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / "index.json").write_text(
                json.dumps(_index()), encoding="utf-8")
            speaker = FakeSpeaker()
            speaker._store_dir = store
            obj = _obj(speaker, _registry())
            self.assertTrue(main.JarvisLive._refresh_voice_cohorts(
                obj, "teste"))
            data = json.loads(
                (store / "cohorts.json").read_text(encoding="utf-8"))
            self.assertEqual(data["owner"], OWNER)
            self.assertIn("cohorts", data)

    def test_enrolling_a_voice_triggers_the_cohort_refresh(self):
        speaker = FakeSpeaker()
        obj = _obj(speaker, _registry())
        obj._voice_id_enabled = True
        obj._last_unknown_pcm = None
        obj._last_unknown_emb = [1.0, 0.0]
        calls: list[str] = []
        obj._refresh_voice_cohorts = lambda reason="": calls.append(reason) or True

        self.assertTrue(main.JarvisLive._voice_enroll_gate(obj, "ela é a Ana"))
        self.assertEqual([call[0] for call in speaker.enroll_calls], ["Ana"])
        self.assertEqual(calls, ["cadastro de convidada"])


if __name__ == "__main__":
    unittest.main()
