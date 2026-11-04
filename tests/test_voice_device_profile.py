"""Voice profile selected by the PHYSICAL microphone, and its migration.

The bug these cover: the app read the webcam through an AEC node while the
voice index held a headset centroid; matching pooled every profile, so the
owner's own voice scored 0.2–0.3 and looked like an intruder.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core import speaker_id

HW_A = "alsa-input-usb-046d-hd-pro-webcam-c920-fed801ef-02-analog-stereo"
HW_B = "alsa-input-usb-edfier-edifier-w820nb-plus-usbcompositedevic-00-mono-fallback"


def _vec(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=dim)
    return vector / np.linalg.norm(vector)


def _near(base: np.ndarray, seed: int = 99, mix: float = 0.03) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = base + mix * rng.normal(size=base.shape)
    return vector / np.linalg.norm(vector)


class PerDeviceProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)
        # Orthogonal basis vectors: the similarity between the two voices is
        # zero, so "wrong profile" cannot pass by chance (8-D random vectors
        # often correlate above 0.45).
        self.a = np.zeros(16)
        self.a[0] = 1.0
        self.b = np.zeros(16)
        self.b[1] = 1.0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_switching_microphone_consults_the_other_centroid(self):
        self.sid.enroll("Anthero", self.a, authorized=True, device=HW_A)
        self.sid.enroll("Anthero", self.b, authorized=True, device=HW_B)

        self.sid.set_active_device(HW_A)
        on_a = self.sid.identify(_near(self.a))
        self.assertTrue(on_a["known"])
        self.assertTrue(on_a["authorized"])
        self.assertEqual(on_a["device"], HW_A)
        self.assertEqual(on_a["profile"], HW_A)

        self.sid.set_active_device(HW_B)
        on_b = self.sid.identify(_near(self.b))
        self.assertTrue(on_b["known"])
        self.assertTrue(on_b["authorized"])
        self.assertEqual(on_b["device"], HW_B)

        # The webcam voice on the headset profile (and vice versa) must NOT be
        # "known": the whole point is that profiles are not interchangeable.
        wrong = self.sid.identify(_near(self.a))
        self.assertFalse(wrong["known"])
        low = self.sid.identify(_near(self.b))
        self.assertGreater(low["effective_score"], 0.45)

    def test_microphone_without_enrolment_is_not_compared_with_another(self):
        self.sid.enroll("Anthero", self.a, authorized=True, device=HW_A)
        self.sid.set_active_device("microfone-nunca-cadastrado")

        # This embedding IS the enrolled centroid: pooling all profiles would
        # return a perfect match. Filtered, it must say there is no profile.
        match = self.sid.identify(self.a, include_auto=True)
        self.assertEqual(match["verdict"], speaker_id.VERDICT_NO_PROFILE)
        self.assertFalse(match["known"])
        self.assertFalse(match["authorized"])
        self.assertIsNone(match["name"])
        self.assertEqual(match["score"], 0.0)
        self.assertEqual(match["device"], "microfone-nunca-cadastrado")

    def test_explicit_empty_device_keeps_the_legacy_pooling(self):
        # Compatibility door: tools/tests that never set an active device (or
        # pass "") still see the historical any-profile match.
        self.sid.enroll("Anthero", self.a, authorized=True, device=HW_A)
        self.sid.set_active_device(HW_B)
        match = self.sid.identify(self.a, device="")
        self.assertTrue(match["known"])
        self.assertEqual(match["device"], HW_A)

    def test_auto_set_does_not_vouch_across_microphones(self):
        self.sid.enroll("Anthero", self.a, authorized=True, device=HW_A)
        self.sid.auto_enroll(embedding=self.a)
        self.sid.set_active_device(HW_B)
        match = self.sid.identify(self.a, include_auto=True)
        self.assertEqual(match["verdict"], speaker_id.VERDICT_NO_PROFILE)

    def test_has_profile_for(self):
        self.sid.enroll("Anthero", self.a, authorized=True, device=HW_A)
        self.assertTrue(self.sid.has_profile_for(HW_A))
        self.assertTrue(self.sid.has_profile_for(HW_A, "Anthero"))
        self.assertFalse(self.sid.has_profile_for(HW_B))
        self.assertFalse(self.sid.has_profile_for(""))

    def test_active_device_can_be_cleared(self):
        self.sid.set_active_device(HW_A)
        self.assertEqual(self.sid.active_device, HW_A)
        self.sid.set_active_device("")
        self.assertIsNone(self.sid.active_device)


class DeviceProfileMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.vec = _vec(5)
        self.sid = speaker_id.SpeakerId(store_dir=self.store)
        self.sid.enroll("Anthero", self.vec, authorized=True)
        self.assertTrue(
            any(rec["devices"] == [speaker_id.DEFAULT_DEVICE]
                for rec in self.sid.list()))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_explicit_mapping_copies_without_destroying_the_legacy_profile(self):
        report = self.sid.migrate_device_profiles({speaker_id.DEFAULT_DEVICE: HW_A})
        self.assertEqual(report["copied"], 1)
        self.assertEqual(report["aliases"], {speaker_id.DEFAULT_DEVICE: HW_A})
        self.assertTrue(report["backup"])
        rec = next(r for r in self.sid.list() if r["owner"])
        self.assertEqual(set(rec["devices"]),
                         {speaker_id.DEFAULT_DEVICE, HW_A})

        # The copy is usable on that hardware and scores like the original.
        self.sid.set_active_device(HW_A)
        match = self.sid.identify(_near(self.vec))
        self.assertTrue(match["known"])
        self.assertEqual(match["device"], HW_A)

    def test_mapping_is_recorded_and_never_reapplied(self):
        self.sid.migrate_device_profiles({speaker_id.DEFAULT_DEVICE: HW_A})
        reloaded = speaker_id.SpeakerId(store_dir=self.store)
        self.assertEqual(reloaded.device_aliases,
                         {speaker_id.DEFAULT_DEVICE: HW_A})
        before = reloaded.list()
        again = reloaded.migrate_device_profiles({speaker_id.DEFAULT_DEVICE: HW_A})
        self.assertEqual(again["copied"], 0)
        self.assertEqual(reloaded.list(), before)

    def test_migration_survives_a_legacy_index_without_profiles(self):
        # Simulate an index written before per-device profiles existed.
        index = self.store / "index.json"
        data = json.loads(index.read_text(encoding="utf-8"))
        rec = data["speakers"]["anthero"]
        rec.pop("profiles", None)
        rec.pop("primary", None)
        index.write_text(json.dumps(data), encoding="utf-8")

        sid = speaker_id.SpeakerId(store_dir=self.store)
        report = sid.migrate_device_profiles({"pc": HW_A})
        self.assertEqual(report["copied"], 1)
        self.assertTrue(sid.has_profile_for(HW_A))
        # Legacy 'pc' still answers when no active device is set.
        self.assertTrue(sid.identify(_near(self.vec))["known"])

    def test_bad_mapping_is_ignored(self):
        report = self.sid.migrate_device_profiles({"pc": "pc"})
        self.assertEqual(report["copied"], 0)
        report = self.sid.migrate_device_profiles({})
        self.assertEqual(report["copied"], 0)


if __name__ == "__main__":
    unittest.main()
