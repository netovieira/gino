"""The single owner decision (core/owner_decision.py).

Synthetic vectors, injected cohort registry: the score boundaries are
arithmetic, not microphone luck. These tests pin the policy that matters:

  * owner above the calibrated limit may open a privileged gate;
  * another speaker, an unauthorized voice and an unknown voice may not;
  * ``no_profile`` is a state, never an authorization (allow_single is gone);
  * diffuse acoustics (room/TV) block privilege even with the owner's score;
  * AS-norm reports ``uncalibrated=True`` with few samples and the raw score;
  * SpeakerState only changes after N concordant units and decays.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import numpy as np

from core import owner_decision as od
from core import voice_cohorts as vc

HW_C920 = "alsa-input-usb-046d-hd-pro-webcam-c920-test"
HW_EDIFIER = "alsa-input-usb-edifier-w820nb-test"
OWNER = "Anthero"


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


def _match(name, score, *, authorized=True, verdict="known", known=True):
    return {"name": name, "score": score, "sample_score": score,
            "effective_score": score, "known": known,
            "authorized": authorized, "verdict": verdict}


def _owner(score=0.72, **kwargs):
    return _match(OWNER, score, **kwargs)


class OwnerDetectedTest(unittest.TestCase):
    def setUp(self):
        self.registry = _registry()

    def test_owner_above_calibrated_limit_is_owner(self):
        verdict = od.decide(match=_owner(0.72), device=HW_C920,
                            registry=self.registry, privileged=False)
        self.assertIs(verdict.who, od.Who.OWNER)
        self.assertIs(verdict.action, od.Action.NORMAL)
        self.assertFalse(verdict.privileged)
        self.assertTrue(verdict.calibrated)
        self.assertFalse(verdict.uncalibrated)
        self.assertEqual(verdict.threshold_source, "device")
        self.assertGreater(verdict.confidence, 0.5)
        self.assertGreater(verdict.normalized_score, 0.0)
        self.assertIn("dono confirmado", verdict.reason)

    def test_owner_opens_the_privileged_action(self):
        verdict = od.decide(match=_owner(0.72), device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OWNER)
        self.assertIs(verdict.action, od.Action.PRIVILEGED)
        self.assertTrue(verdict.privileged)
        self.assertTrue(verdict.can_answer)

    def test_pc_alias_is_the_same_capture_chain(self):
        verdict = od.decide(match=_owner(0.72), device="pc",
                            registry=self.registry, privileged=True)
        self.assertTrue(verdict.privileged)
        self.assertEqual(verdict.cohort, "webcam-c920")

    def test_match_carries_the_device_when_the_caller_omits_it(self):
        payload = _owner(0.72)
        payload["device"] = HW_C920
        verdict = od.decide(match=payload, registry=self.registry,
                            privileged=True)
        self.assertTrue(verdict.calibrated)
        self.assertTrue(verdict.privileged)
        self.assertEqual(verdict.device, HW_C920)

    def test_explicit_embedding_and_reference_also_work(self):
        profile = _index()["speakers"]["anthero"]["profiles"][HW_C920]
        verdict = od.decide(embedding=profile["samples"][0],
                            reference=profile, device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OWNER)
        self.assertTrue(verdict.privileged)
        self.assertGreater(verdict.score, 0.99)

    def test_explicit_score_overrides_the_match_field(self):
        verdict = od.decide(match=_owner(0.10), score=0.90, device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OWNER)
        self.assertAlmostEqual(verdict.score, 0.90, places=6)


class OtherSpeakerTest(unittest.TestCase):
    def setUp(self):
        self.registry = _registry()

    def test_known_guest_never_opens_a_privileged_gate(self):
        verdict = od.decide(match=_match("Eloisa", 0.95), device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertFalse(verdict.privileged)
        self.assertEqual(verdict.speaker, "Eloisa")
        self.assertIn("não o dono", verdict.reason)

    def test_guest_may_still_be_answered_normally(self):
        verdict = od.decide(match=_match("Eloisa", 0.95), device=HW_C920,
                            registry=self.registry, privileged=False)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertIs(verdict.action, od.Action.NORMAL)
        self.assertFalse(verdict.privileged)

    def test_unknown_voice_is_rejected(self):
        verdict = od.decide(
            match=_match(None, 0.20, authorized=False, verdict="unknown",
                         known=False),
            device=HW_C920, registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertFalse(verdict.privileged)

    def test_owner_name_without_authorization_is_not_owner(self):
        verdict = od.decide(match=_owner(0.95, authorized=False),
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertFalse(verdict.privileged)

    def test_explicit_is_owner_false_wins_even_with_the_owner_name(self):
        verdict = od.decide(match=_owner(0.95), is_owner=False,
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertFalse(verdict.privileged)

    def test_score_in_the_gray_band_is_uncertain(self):
        # 0.60 is below the 0.613 limit but inside the normalized gray band.
        verdict = od.decide(match=_owner(0.60), device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertFalse(verdict.privileged)
        self.assertIn("faixa cinzenta", verdict.reason)

    def test_clearly_below_the_limit_is_other(self):
        verdict = od.decide(match=_owner(0.45), device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.OTHER)
        self.assertFalse(verdict.privileged)


class NoProfileTest(unittest.TestCase):
    def setUp(self):
        self.registry = _registry()

    def test_no_profile_is_a_state_never_an_authorization(self):
        verdict = od.decide(
            match=_match(None, 0.99, authorized=False, verdict="no_profile",
                         known=False),
            device=HW_EDIFIER, registry=self.registry, privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertFalse(verdict.privileged)
        self.assertEqual(verdict.profile_state, "no_profile")
        self.assertIn("allow_single não autoriza", verdict.reason)
        self.assertIn("privilégio negado", verdict.reason)

    def test_no_profile_still_allows_normal_speech(self):
        verdict = od.decide(
            match=_match(None, 0.99, authorized=False, verdict="no_profile",
                         known=False),
            device=HW_EDIFIER, registry=self.registry, privileged=False)
        self.assertIs(verdict.action, od.Action.NORMAL)
        self.assertFalse(verdict.privileged)

    def test_raw_score_on_a_device_without_profile_is_also_no_profile(self):
        threshold = od.threshold_for(HW_EDIFIER, registry=self.registry)
        # A guest profile exists there, but not the owner's: the owner decision
        # must say no_profile rather than compare against the guest.
        self.assertTrue(threshold.has_profile)
        self.assertFalse(threshold.owner_profile)
        verdict = od.decide(score=0.99, device=HW_EDIFIER,
                            registry=self.registry, privileged=True)
        self.assertEqual(verdict.profile_state, "no_profile")
        self.assertFalse(verdict.privileged)
        self.assertIs(verdict.action, od.Action.NONE)

    def test_malformed_voiced_seconds_does_not_crash_the_gate(self):
        verdict = od.decide(match=_owner(0.72), voiced_seconds="não é número",
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertTrue(verdict.privileged)

    def test_silence_blocks_even_normal_speech_without_profile(self):
        verdict = od.decide(
            match=_match(None, 0.99, authorized=False, verdict="no_profile",
                         known=False),
            device=HW_EDIFIER, acoustic={"silent": True},
            registry=self.registry, privileged=False)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertEqual(verdict.profile_state, "no_profile")
        self.assertFalse(verdict.privileged)

    def test_short_voiced_audio_is_neutral_and_never_privileged(self):
        verdict = od.decide(match=_owner(0.99), voiced_seconds=0.3,
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertEqual(verdict.profile_state, "indecisive_short")
        self.assertFalse(verdict.privileged)
        self.assertIn("curta demais", verdict.reason)

    def test_no_score_is_uncertain(self):
        verdict = od.decide(device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertFalse(verdict.privileged)
        self.assertIn("sem embedding/score", verdict.reason)


class AcousticTest(unittest.TestCase):
    def setUp(self):
        self.registry = _registry()

    def test_diffuse_room_blocks_privilege_even_for_the_owner_score(self):
        verdict = od.decide(match=_owner(0.95), acoustic="diffuse",
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertFalse(verdict.privileged)
        self.assertLessEqual(verdict.confidence, 0.40)
        self.assertIn("difusa", verdict.reason)

    def test_pair_reading_object_is_accepted(self):
        reading = SimpleNamespace(verdict="near", silent=False)
        verdict = od.decide(match=_owner(0.72), acoustic=reading,
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertTrue(verdict.privileged)
        self.assertEqual(verdict.acoustic, "near")
        self.assertIn("microfone próximo", verdict.reason)

    def test_mapping_with_silent_true_blocks_everything(self):
        verdict = od.decide(match=_owner(0.95), acoustic={"silent": True},
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertIs(verdict.action, od.Action.NONE)
        self.assertFalse(verdict.privileged)
        self.assertEqual(verdict.acoustic, "silent")

    def test_portuguese_labels_are_understood(self):
        verdict = od.decide(match=_owner(0.95), acoustic="difuso",
                            device=HW_C920, registry=self.registry,
                            privileged=True)
        self.assertEqual(verdict.acoustic, "diffuse")
        self.assertFalse(verdict.privileged)


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.registry = _registry()

    def test_threshold_for_device_is_calibrated_and_above_the_default(self):
        threshold = od.threshold_for(HW_C920, registry=self.registry)
        self.assertTrue(threshold.calibrated)
        self.assertGreater(threshold.threshold, vc.DEFAULT_THRESHOLD)
        self.assertEqual(threshold.normalized_threshold, 0.0)

    def test_threshold_for_falls_back_with_few_samples(self):
        threshold = od.threshold_for("celular", registry=self.registry)
        self.assertFalse(threshold.calibrated)
        self.assertEqual(threshold.threshold, vc.DEFAULT_THRESHOLD)
        self.assertIn("default explícito", threshold.reason)
        verdict = od.decide(match=_owner(0.95), device="celular",
                            registry=self.registry, privileged=True)
        self.assertTrue(verdict.uncalibrated)
        self.assertAlmostEqual(verdict.normalized_score, 0.95, places=6)
        self.assertEqual(verdict.threshold, vc.DEFAULT_THRESHOLD)
        self.assertTrue(verdict.privileged)  # raw score still decides

    def test_as_norm_changes_the_comparison_when_calibrated(self):
        verdict = od.decide(match=_owner(0.72), device=HW_C920,
                            registry=self.registry, privileged=True)
        self.assertTrue(verdict.calibrated)
        self.assertNotAlmostEqual(verdict.normalized_score, verdict.score,
                                  places=3)
        self.assertEqual(verdict.normalized_threshold, 0.0)

    def test_explicit_threshold_disables_as_norm(self):
        verdict = od.decide(match=_owner(0.50), device=HW_C920,
                            threshold=0.40, registry=self.registry,
                            privileged=True)
        self.assertTrue(verdict.uncalibrated)
        self.assertEqual(verdict.threshold, 0.40)
        self.assertEqual(verdict.threshold_source, "explicit")
        self.assertTrue(verdict.privileged)

    def test_explicit_threshold_must_be_finite(self):
        with self.assertRaises(ValueError):
            od.decide(match=_owner(0.72), threshold=float("nan"),
                      registry=self.registry)


class SpeakerStateTest(unittest.TestCase):
    def test_needs_n_concordant_units(self):
        state = od.SpeakerState(confirmations=2, decay_seconds=10.0, now=0)
        self.assertEqual(state.state, od.SpeakerState.TRANSITION)
        self.assertEqual(state.observe(od.Who.OWNER, now=1),
                         od.SpeakerState.TRANSITION)
        self.assertFalse(state.changed)
        self.assertEqual(state.observe(od.Who.OWNER, now=2),
                         od.SpeakerState.PRESENT)
        self.assertTrue(state.changed)

    def test_one_contradiction_does_not_flip_a_stable_state(self):
        state = od.SpeakerState(confirmations=2, decay_seconds=10.0, now=0)
        state.observe(od.Who.OWNER, now=1)
        state.observe(od.Who.OWNER, now=2)
        self.assertEqual(state.observe(od.Who.OTHER, now=3),
                         od.SpeakerState.PRESENT)
        self.assertEqual(state.observe(od.Who.OTHER, now=4),
                         od.SpeakerState.ABSENT)
        # And back: one owner sentence is not enough.
        self.assertEqual(state.observe(od.Who.OWNER, now=5),
                         od.SpeakerState.ABSENT)
        self.assertEqual(state.observe(od.Who.OWNER, now=6),
                         od.SpeakerState.PRESENT)

    def test_uncertain_units_do_not_vote(self):
        state = od.SpeakerState(confirmations=2, decay_seconds=10.0, now=0)
        for moment in range(1, 8):
            self.assertEqual(state.observe(od.Who.UNCERTAIN, now=moment),
                             od.SpeakerState.TRANSITION)

    def test_decay_returns_to_transition_and_resets_the_streak(self):
        state = od.SpeakerState(confirmations=2, decay_seconds=5.0, now=0)
        state.observe(od.Who.OWNER, now=1)
        state.observe(od.Who.OWNER, now=2)
        self.assertEqual(state.state_at(now=8), od.SpeakerState.TRANSITION)
        self.assertEqual(state.observe(od.Who.OWNER, now=9),
                         od.SpeakerState.TRANSITION)
        self.assertEqual(state.observe(od.Who.OWNER, now=10),
                         od.SpeakerState.PRESENT)

    def test_invalid_configuration_is_clamped(self):
        state = od.SpeakerState(confirmations=0, decay_seconds=-1)
        self.assertEqual(state.confirmations, 1)
        self.assertGreater(state.decay_seconds, 0)

    def test_decide_feeds_and_reports_the_state(self):
        state = od.SpeakerState(confirmations=2, decay_seconds=10.0, now=0)
        first = od.decide(match=_owner(0.72), device=HW_C920,
                          registry=_registry(), state=state, now=1)
        self.assertEqual(first.state, od.SpeakerState.TRANSITION)
        second = od.decide(match=_owner(0.72), device=HW_C920,
                           registry=_registry(), state=state, now=2)
        self.assertEqual(second.state, od.SpeakerState.PRESENT)
        self.assertTrue(second.state_changed)


class PrivilegeDoorTest(unittest.TestCase):
    def test_only_owner_with_privileged_action_carries_privilege(self):
        registry = _registry()
        cases = [
            dict(match=_owner(0.95), privileged=True, acoustic="near"),
            dict(match=_match("Eloisa", 0.99), privileged=True),
            dict(match=_match(None, 0.1, authorized=False,
                              verdict="unknown", known=False), privileged=True),
            dict(match=_match(None, 0.99, authorized=False,
                              verdict="no_profile", known=False),
                 device=HW_EDIFIER, privileged=True),
            dict(match=_owner(0.95), acoustic="diffuse", privileged=True),
            dict(match=_owner(0.99), voiced_seconds=0.2, privileged=True),
            dict(score=0.99, device=HW_EDIFIER, privileged=True),
            dict(device=HW_C920, privileged=True),
        ]
        for kwargs in cases:
            kwargs.setdefault("device", HW_C920)
            verdict = od.decide(registry=registry, **kwargs)
            self.assertEqual(verdict.privileged,
                             verdict.action is od.Action.PRIVILEGED)
            if verdict.privileged:
                self.assertIs(verdict.who, od.Who.OWNER)
                self.assertNotEqual(verdict.profile_state, "no_profile")
                self.assertNotEqual(verdict.acoustic, "diffuse")

    def test_no_profile_never_privileges_even_with_a_perfect_score(self):
        for privileged in (True, False):
            verdict = od.decide(
                match=_match(OWNER, 1.0, verdict="no_profile", known=False),
                device=HW_EDIFIER, registry=_registry(),
                privileged=privileged)
            self.assertFalse(verdict.privileged)
            self.assertEqual(verdict.profile_state, "no_profile")

    def test_verdict_dict_is_json_ready(self):
        verdict = od.decide(match=_owner(0.72), device=HW_C920,
                            registry=_registry(), privileged=True,
                            transcript="Gino, reinicia o assistente")
        payload = verdict.as_dict()
        self.assertEqual(payload["who"], "owner")
        self.assertEqual(payload["action"], "privilegiado")
        self.assertTrue(payload["speech"])
        self.assertIn("reinicia", payload["reason"])
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_empty_transcript_is_reported_as_no_speech(self):
        verdict = od.decide(match=_owner(0.72), device=HW_C920,
                            registry=_registry(), transcript="   ")
        self.assertFalse(verdict.speech)

    def test_invalid_embedding_does_not_open_the_gate(self):
        verdict = od.decide(embedding=[0.0, 0.0], reference={"centroid": [1.0, 0.0]},
                            device=HW_C920, registry=_registry(),
                            privileged=True)
        self.assertIs(verdict.who, od.Who.UNCERTAIN)
        self.assertFalse(verdict.privileged)


if __name__ == "__main__":
    unittest.main()
