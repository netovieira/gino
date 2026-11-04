"""Cohort grouping and per-device limits (core/voice_cohorts.py).

Synthetic vectors only: orthogonal dimensions for the owner and a guest, with
a controlled cosine between them, so the intra/inter distributions and the
derived limit are arithmetic facts instead of microphone luck. No model, no
audio, no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from core import voice_cohorts as vc

HW_C920 = "alsa-input-usb-046d-hd-pro-webcam-c920-test"
HW_EDIFIER = "alsa-input-usb-edifier-w820nb-test"
HW_C920_B = "alsa-input-usb-046d-hd-pro-webcam-c920-second"


def _unit(value) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    return arr / np.linalg.norm(arr)


def _near(base: np.ndarray, seed: int, mix: float = 0.02) -> list[float]:
    rng = np.random.default_rng(seed)
    return _unit(base + mix * rng.normal(size=base.shape)).tolist()


def _index(*, owner_second_device: bool = False,
           owner_first_samples: int = 5,
           guest_away: bool = False) -> dict:
    e0 = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    e1 = np.array([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    guest_base = 0.3 * e0 + np.sqrt(1.0 - 0.09) * e1
    owner_a = [_near(e0, i) for i in range(owner_first_samples)]
    guest = [_near(guest_base, 100 + i) for i in range(5)]
    owner_phone = [_near(e0, 200 + i, 0.03) for i in range(3)]
    profiles = {
        HW_C920: {"samples": owner_a},
        "celular": {"samples": owner_phone},
    }
    if owner_second_device:
        profiles[HW_C920_B] = {"samples": [_near(e0, 300 + i) for i in range(3)]}
    guest_device = HW_C920 if not guest_away else HW_EDIFIER
    return {
        "owner": "Anthero",
        "device_aliases": {"pc": HW_C920},
        "speakers": {
            "anthero": {"name": "Anthero", "authorized": True,
                        "profiles": profiles},
            "eloisa": {"name": "Eloisa", "authorized": True,
                       "profiles": {guest_device: {"samples": guest}}},
        },
    }


class CohortMatchingTest(unittest.TestCase):
    def test_matches_hardware_by_pattern_and_alias(self):
        registry = vc.CohortRegistry.from_index(_index())
        self.assertEqual(registry.cohort_for(HW_C920), "webcam-c920")
        # Historical key "pc" is an alias of the same physical webcam.
        self.assertEqual(registry.cohort_for("pc"), "webcam-c920")
        self.assertEqual(registry.cohort_for(HW_EDIFIER), "headset-edifier")
        self.assertEqual(registry.cohort_for("celular"), "celular")
        self.assertEqual(registry.cohort_for("usb-audio-desconhecido"), "default")
        self.assertEqual(registry.cohort_for(None), "default")

    def test_short_alias_does_not_match_inside_another_word(self):
        registry = vc.CohortRegistry.from_index(_index())
        # "pcm" contains "pc" but is not the desktop microphone cohort.
        self.assertEqual(registry.cohort_for("alsa-input-usb-pcm-test"),
                         "default")
        # "headphone" contains "phone" but is a headset, not a phone.
        self.assertEqual(registry.cohort_for("usb-headphone-test"),
                         "headset-edifier")

    def test_alias_resolution_is_case_and_accent_insensitive(self):
        registry = vc.CohortRegistry.from_index(_index())
        self.assertEqual(registry.resolved_device("PC"), vc._slugify(HW_C920))
        self.assertEqual(registry.cohort_for("PC"), "webcam-c920")


class CalibratedThresholdTest(unittest.TestCase):
    def setUp(self):
        self.registry = vc.CohortRegistry.from_index(_index())

    def test_threshold_is_derived_between_the_two_distributions(self):
        threshold = self.registry.threshold_for(HW_C920)
        self.assertTrue(threshold.calibrated)
        self.assertEqual(threshold.source, vc.SOURCE_DEVICE)
        self.assertEqual(threshold.samples, 5)
        self.assertTrue(threshold.has_profile)
        self.assertGreater(threshold.intra.mean, threshold.inter.mean)
        self.assertGreater(threshold.threshold, threshold.inter.mean)
        self.assertLess(threshold.threshold, threshold.intra.mean)
        self.assertAlmostEqual(threshold.normalize(threshold.threshold), 0.0,
                               places=6)
        self.assertEqual(threshold.normalized_threshold, 0.0)
        self.assertIn("calibrado", threshold.reason)

    def test_accept_z_moves_both_the_raw_and_normalized_limit(self):
        base = self.registry.threshold_for(HW_C920)
        strict = self.registry.threshold_for(HW_C920, accept_z=1.0)
        lenient = self.registry.threshold_for(HW_C920, accept_z=-1.0)
        self.assertGreater(strict.threshold, base.threshold)
        self.assertLess(lenient.threshold, base.threshold)
        self.assertEqual(strict.normalized_threshold, 1.0)
        self.assertEqual(lenient.normalized_threshold, -1.0)
        self.assertAlmostEqual(strict.normalize(strict.threshold), 1.0,
                               places=6)

    def test_pc_alias_uses_the_same_calibrated_limit(self):
        self.assertAlmostEqual(self.registry.threshold_for("pc").threshold,
                               self.registry.threshold_for(HW_C920).threshold,
                               places=9)
        self.assertTrue(self.registry.threshold_for("pc").calibrated)

    def test_duplicate_migrated_profile_does_not_inflate_the_sample_count(self):
        index = _index()
        # The migration copied the same vectors under the canonical key.
        index["speakers"]["anthero"]["profiles"]["pc"] = {
            "samples": index["speakers"]["anthero"]["profiles"][HW_C920]["samples"]}
        registry = vc.CohortRegistry.from_index(index)
        threshold = registry.threshold_for("pc")
        self.assertEqual(threshold.samples, 5)
        self.assertEqual(threshold.intra.n, 5)
        self.assertTrue(threshold.calibrated)


class FallbackThresholdTest(unittest.TestCase):
    def setUp(self):
        self.registry = vc.CohortRegistry.from_index(_index())

    def test_three_samples_use_the_explicit_default(self):
        threshold = self.registry.threshold_for("celular")
        self.assertFalse(threshold.calibrated)
        self.assertEqual(threshold.source, vc.SOURCE_DEFAULT)
        self.assertEqual(threshold.threshold, vc.DEFAULT_THRESHOLD)
        self.assertIsNone(threshold.normalized_threshold)
        self.assertTrue(threshold.has_profile)
        self.assertIn("default explícito", threshold.reason)
        self.assertIn("3 < 4", threshold.reason)
        # Uncalibrated normalization returns the raw score untouched.
        self.assertEqual(threshold.normalize(0.73), 0.73)

    def test_device_without_any_profile_is_not_a_rejection(self):
        threshold = self.registry.threshold_for(HW_EDIFIER)
        self.assertFalse(threshold.calibrated)
        self.assertEqual(threshold.source, vc.SOURCE_DEFAULT)
        self.assertFalse(threshold.has_profile)
        self.assertEqual(threshold.threshold, vc.DEFAULT_THRESHOLD)
        self.assertIn("sem amostras", threshold.reason)
        self.assertIn("default explícito", threshold.reason)

    def test_unknown_device_has_no_profile(self):
        threshold = self.registry.threshold_for("usb-audio-xyz")
        self.assertFalse(threshold.calibrated)
        self.assertFalse(threshold.has_profile)
        self.assertEqual(float(threshold), vc.DEFAULT_THRESHOLD)
        # With no device at all the registry cannot answer membership.
        self.assertIsNone(self.registry.threshold_for(None).has_profile)

    def test_cohort_fallback_pools_two_devices_of_the_same_type(self):
        registry = vc.CohortRegistry.from_index(_index(
            owner_second_device=True, owner_first_samples=2))
        threshold = registry.threshold_for(HW_C920)
        self.assertTrue(threshold.calibrated)
        self.assertEqual(threshold.source, vc.SOURCE_COHORT)
        self.assertEqual(threshold.samples, 5)
        self.assertIn(HW_C920_B, threshold.profile_devices)

    def test_overlapping_distributions_are_not_called_calibrated(self):
        e0 = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        # The owner's own clips wander; the impostor sits on their centroid, so
        # the two distributions are indistinguishable and any limit is noise.
        owner = [_near(e0, 500 + i, 1.0) for i in range(5)]
        centroid = _unit(np.mean(np.asarray([_unit(v) for v in owner]), axis=0))
        guest = [_near(centroid, 600 + i, 0.02) for i in range(5)]
        index = {
            "owner": "Anthero",
            "speakers": {
                "anthero": {"name": "Anthero", "profiles": {
                    HW_C920: {"samples": owner}}},
                "eloisa": {"name": "Eloisa", "profiles": {
                    HW_EDIFIER: {"samples": guest}}},
            },
        }
        threshold = vc.CohortRegistry.from_index(index).threshold_for(HW_C920)
        self.assertFalse(threshold.calibrated)
        self.assertEqual(threshold.source, vc.SOURCE_DEFAULT)
        self.assertIn("não separáveis", threshold.reason)


class RefreshSnapshotTest(unittest.TestCase):
    def test_refresh_writes_stats_and_snapshot_loads_without_the_index(self):
        index = _index()
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            index_path = store / "index.json"
            index_path.write_text(json.dumps(index), encoding="utf-8")
            before = index_path.read_bytes()
            data = vc.refresh(store_dir=store)
            written = store / "cohorts.json"
            self.assertTrue(written.is_file())
            self.assertEqual(index_path.read_bytes(), before)
            self.assertEqual(os.stat(written).st_mode & 0o777, 0o600)

            raw = json.loads(written.read_text(encoding="utf-8"))
            self.assertEqual(raw["owner"], "Anthero")
            self.assertIn("webcam-c920", raw["cohorts"])
            self.assertIn(HW_C920, raw["cohorts"]["webcam-c920"]["devices"])
            self.assertTrue(
                raw["cohorts"]["webcam-c920"]["stats"]["devices"][HW_C920]
                ["calibrated"])
            self.assertEqual(data["cohorts"]["webcam-c920"]["devices"],
                             raw["cohorts"]["webcam-c920"]["devices"])

            # No index at runtime: the persisted statistics still answer.
            snapshot = vc.CohortRegistry.from_file(
                written, index_path=store / "missing.json")
            threshold = snapshot.threshold_for(HW_C920)
            self.assertTrue(threshold.calibrated)
            self.assertTrue(threshold.has_profile)
            self.assertEqual(threshold.samples, 5)
            self.assertAlmostEqual(threshold.threshold,
                                   vc.CohortRegistry.from_index(index)
                                   .threshold_for(HW_C920).threshold,
                                   places=5)

    def test_refresh_persist_false_does_not_create_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / "index.json").write_text(json.dumps(_index()),
                                              encoding="utf-8")
            data = vc.refresh(store_dir=store, persist=False)
            self.assertFalse((store / "cohorts.json").exists())
            self.assertIn("webcam-c920", data["cohorts"])

    def test_loading_without_files_falls_back_to_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = vc.CohortRegistry.from_file(
                Path(tmp) / "cohorts.json", index_path=Path(tmp) / "index.json")
            threshold = registry.threshold_for(HW_C920)
            self.assertFalse(threshold.calibrated)
            self.assertEqual(threshold.source, vc.SOURCE_DEFAULT)
            self.assertIsNone(threshold.has_profile)
            self.assertEqual(threshold.threshold, vc.DEFAULT_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
