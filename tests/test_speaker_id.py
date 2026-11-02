"""SpeakerId store/threshold tests and the GroqListener callback switch.

The enrollment tests use synthetic normalized vectors, so they never load an
ONNX model. The diarization smoke test skips when the git-ignored models are
absent. The listener tests replace the transcriber, so nothing touches Groq or
Whisper.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core import groq_stt, speaker_id


def _vec(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=dim)
    return vector / np.linalg.norm(vector)


def _near(base: np.ndarray, seed: int = 99, mix: float = 0.03) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = base + mix * rng.normal(size=base.shape)
    return vector / np.linalg.norm(vector)


class SpeakerIdStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_enroll_owner_is_known_and_always_authorized(self):
        self.assertEqual(self.sid.owner_name, "Anthero")
        owner = _vec(1)
        result = self.sid.enroll("Anthero", owner)
        self.assertTrue(result["authorized"])
        self.assertTrue(result["owner"])

        found = self.sid.identify(_near(owner))
        self.assertTrue(found["known"])
        self.assertEqual(found["name"], "Anthero")
        self.assertTrue(found["authorized"])
        self.assertGreaterEqual(found["score"], 0.45)

    def test_enroll_identify_authorize_list_remove(self):
        heloisa = _vec(2)
        enrolled = self.sid.enroll("Heloisa", heloisa)
        self.assertFalse(enrolled["authorized"])

        found = self.sid.identify(_near(heloisa))
        self.assertTrue(found["known"])
        self.assertEqual(found["name"], "Heloisa")
        self.assertFalse(found["authorized"])

        authorized = self.sid.authorize("Heloisa")
        self.assertTrue(authorized["authorized"])
        self.assertTrue(self.sid.identify(_near(heloisa))["authorized"])

        names = [person["name"] for person in self.sid.list()]
        self.assertEqual(names, ["Heloisa"])

        self.assertTrue(self.sid.remove("Heloisa"))
        self.assertFalse(self.sid.remove("Heloisa"))
        self.assertEqual(self.sid.list(), [])
        unknown = self.sid.identify(_near(heloisa))
        self.assertFalse(unknown["known"])
        self.assertIsNone(unknown["name"])
        self.assertFalse(unknown["authorized"])

    def test_cosine_threshold_decision(self):
        # A two-dimensional vector makes the cosine exactly the value we want.
        self.sid.enroll("Base", np.array([1.0, 0.0]))
        above = np.array([0.50, float(np.sqrt(1.0 - 0.50**2))])   # cos = 0.50
        below = np.array([0.40, float(np.sqrt(1.0 - 0.40**2))])   # cos = 0.40

        self.assertTrue(self.sid.identify(above)["known"])
        self.assertAlmostEqual(self.sid.identify(above)["score"], 0.50, places=6)
        self.assertFalse(self.sid.identify(below)["known"])
        self.assertAlmostEqual(self.sid.identify(below)["score"], 0.40, places=6)

        tuned = speaker_id.SpeakerId(store_dir=self.store, threshold=0.60)
        self.assertFalse(tuned.identify(above)["known"])

    def test_unknown_voice_on_empty_store(self):
        result = self.sid.identify(_vec(3))
        self.assertEqual(
            result,
            {"name": None, "score": 0.0, "sample_score": 0.0,
             "effective_score": 0.0, "known": False, "authorized": False,
             "verdict": "unknown"},
        )

    def test_a_single_sample_can_confirm_above_the_centroid(self):
        sid = speaker_id.SpeakerId(store_dir=self.store, threshold=0.8)
        sid.enroll("Base", [np.array([1.0, 0.0, 0.0]),
                            np.array([0.0, 1.0, 0.0])])
        found = sid.identify(np.array([1.0, 0.0, 0.0]))
        self.assertLess(found["score"], 0.8)
        self.assertGreaterEqual(found["sample_score"], 0.8)
        self.assertTrue(found["known"])

    def test_multiple_embeddings_become_one_centroid(self):
        first, second = _vec(4), _vec(5)
        result = self.sid.enroll("Heloisa", [first, second])
        self.assertEqual(result["samples"], 2)

        # The centroid is the normalized mean, so both samples are close.
        self.assertTrue(self.sid.identify(first)["known"])
        self.assertTrue(self.sid.identify(second)["known"])

    def test_reenroll_keeps_authorization(self):
        voice = _vec(6)
        self.sid.enroll("Heloisa", voice)
        self.sid.authorize("Heloisa")
        result = self.sid.enroll("Heloisa", _near(voice))
        self.assertTrue(result["authorized"])
        self.assertEqual(result["samples"], 2)

    def test_set_owner_marks_that_person_authorized(self):
        self.sid.enroll("Heloisa", _vec(7))
        self.sid.set_owner("Heloisa")
        self.assertEqual(self.sid.owner_name, "Heloisa")
        found = self.sid.identify(_vec(7))
        self.assertTrue(found["known"])
        self.assertTrue(found["authorized"])

    def test_index_is_json_with_vectors_and_no_audio(self):
        self.sid.enroll("Heloisa", _vec(8))
        index_file = self.store / "index.json"
        self.assertTrue(index_file.is_file())
        raw = index_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(data["owner"], "Anthero")
        self.assertEqual(data["threshold"], speaker_id.DEFAULT_THRESHOLD)
        person = data["speakers"]["heloisa"]
        self.assertIsInstance(person["centroid"], list)
        self.assertIsInstance(person["samples"][0], list)
        self.assertNotIn("pcm", raw.lower())
        self.assertNotIn("audio", raw.lower())
        self.assertEqual(list(self.store.glob(".index-*.tmp")), [])

        reopened = speaker_id.SpeakerId(store_dir=self.store)
        self.assertTrue(reopened.identify(_vec(8))["known"])

    def test_bad_embeddings_and_missing_names_raise(self):
        with self.assertRaises(ValueError):
            self.sid.enroll("", _vec(9))
        with self.assertRaises(ValueError):
            self.sid.enroll("Zero", np.zeros(8))
        self.sid.enroll("Base", _vec(10, dim=8))
        with self.assertRaises(ValueError):
            self.sid.enroll("Outra dimensão", _vec(11, dim=4))
        with self.assertRaises(KeyError):
            self.sid.authorize("Ninguém")


class AutoEnrollTest(unittest.TestCase):
    """Continuous (adaptive) enrollment: high-confidence, capped, isolated.

    No ONNX model: ``SpeakerId.embedding`` is replaced with a fixed vector and
    the audio is synthetic (loud enough for the quality gate).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _speaker(self, **kw) -> speaker_id.SpeakerId:
        kw.setdefault("store_dir", self.store)
        kw.setdefault("auto_enroll_threshold", 0.75)
        kw.setdefault("auto_enroll_max", 5)
        kw.setdefault("auto_enroll_min_seconds", 0.8)
        return speaker_id.SpeakerId(**kw)

    @staticmethod
    def _pcm(seconds: float = 1.0, seed: int = 0) -> bytes:
        rng = np.random.default_rng(seed)
        samples = rng.normal(0.0, 0.12, int(speaker_id.SAMPLE_RATE * seconds))
        return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    @staticmethod
    def _at_cosine(base: np.ndarray, cosine: float, seed: int = 7) -> np.ndarray:
        rng = np.random.default_rng(seed)
        orth = rng.normal(size=base.shape)
        orth -= base * float(np.dot(orth, base))
        orth /= np.linalg.norm(orth)
        return float(cosine) * base + float(np.sqrt(1.0 - cosine ** 2)) * orth

    def test_confident_match_is_appended_to_the_expanded_set(self):
        base = _vec(1)
        sid = self._speaker()
        sid.enroll("Anthero", base)
        query = self._at_cosine(base, 0.92)
        sid.embedding = lambda _pcm: query

        result = sid.auto_enroll(self._pcm())

        self.assertTrue(result["enrolled"])
        self.assertEqual(result["name"], "Anthero")
        self.assertEqual(result["samples"], 1)
        self.assertEqual(result["max"], 5)
        self.assertGreaterEqual(result["score"], 0.75)
        record = sid.list()[0]
        self.assertEqual(record["samples"], 1)        # trusted set untouched
        self.assertEqual(record["auto_samples"], 1)

    def test_below_high_threshold_is_not_enrolled(self):
        base = _vec(1)
        sid = self._speaker()
        sid.enroll("Anthero", base)
        query = self._at_cosine(base, 0.60)           # known (>0.45), not confident
        sid.embedding = lambda _pcm: query

        self.assertTrue(sid.identify(query)["known"])
        result = sid.auto_enroll(self._pcm())

        self.assertFalse(result["enrolled"])
        self.assertEqual(result["reason"], "confiança baixa")
        self.assertEqual(sid.list()[0]["auto_samples"], 0)

    def test_cap_keeps_the_most_recent_samples_and_persists(self):
        base = _vec(1)
        sid = self._speaker(auto_enroll_max=3)
        sid.enroll("Anthero", base)
        for seed in range(5):
            query = self._at_cosine(base, 0.95, seed=seed)
            sid.embedding = lambda _pcm, q=query: q
            result = sid.auto_enroll(self._pcm(seed=seed))
            self.assertTrue(result["enrolled"])

        self.assertEqual(sid.list()[0]["auto_samples"], 3)
        reopened = self._speaker(auto_enroll_max=3)
        self.assertEqual(reopened.list()[0]["auto_samples"], 3)
        self.assertEqual(reopened.list()[0]["samples"], 1)

    def test_duplicate_audio_does_not_fill_the_cap(self):
        base = _vec(1)
        sid = self._speaker()
        sid.enroll("Anthero", base)
        query = self._at_cosine(base, 0.92)
        sid.embedding = lambda _pcm: query

        self.assertTrue(sid.auto_enroll(self._pcm())["enrolled"])
        second = sid.auto_enroll(self._pcm())

        self.assertFalse(second["enrolled"])
        self.assertEqual(second["reason"], "amostra duplicada")
        self.assertEqual(sid.list()[0]["auto_samples"], 1)

    def test_security_gate_ignores_the_expanded_set(self):
        base = np.array([1.0, 0.0, 0.0, 0.0])
        sid = self._speaker(threshold=0.95, auto_enroll_threshold=0.60)
        sid.enroll("Anthero", base)
        query = self._at_cosine(base, 0.90)
        sid.embedding = lambda _pcm: query
        self.assertTrue(sid.auto_enroll(self._pcm())["enrolled"])

        # Confirmation-gated actions use identify()'s default: trusted only.
        trusted = sid.identify(query)
        self.assertFalse(trusted["known"])
        self.assertAlmostEqual(trusted["score"], 0.90, places=6)
        # The conversational gate may use the expanded set.
        expanded = sid.identify(query, include_auto=True)
        self.assertTrue(expanded["known"])
        self.assertEqual(expanded["name"], "Anthero")

        # A restart keeps both behaviours.
        reopened = self._speaker(threshold=0.95, auto_enroll_threshold=0.60)
        self.assertFalse(reopened.identify(query)["known"])
        self.assertTrue(reopened.identify(query, include_auto=True)["known"])

    def test_unauthorized_voice_is_never_auto_enrolled(self):
        base = _vec(2)
        sid = self._speaker()
        sid.enroll("Heloisa", base)
        query = self._at_cosine(base, 0.95)
        sid.embedding = lambda _pcm: query

        result = sid.auto_enroll(self._pcm())

        self.assertFalse(result["enrolled"])
        self.assertEqual(result["reason"], "voz não autorizada")
        self.assertEqual(sid.list()[0]["auto_samples"], 0)

    def test_bad_audio_is_rejected(self):
        base = _vec(1)
        sid = self._speaker()
        sid.enroll("Anthero", base)
        sid.embedding = lambda _pcm: self._at_cosine(base, 0.95)

        short = sid.auto_enroll(self._pcm(seconds=0.3))
        self.assertFalse(short["enrolled"])
        self.assertIn("curto", short["reason"])

        silent = sid.auto_enroll(b"\x00\x00" * speaker_id.SAMPLE_RATE)
        self.assertFalse(silent["enrolled"])
        self.assertIn("baixo", silent["reason"])

        clipped = sid.auto_enroll(
            np.full(speaker_id.SAMPLE_RATE, 32767, dtype="<i2").tobytes())
        self.assertFalse(clipped["enrolled"])
        self.assertIn("saturado", clipped["reason"])
        self.assertEqual(sid.list()[0]["auto_samples"], 0)

    def test_disabled_by_config(self):
        base = _vec(1)
        sid = self._speaker(auto_enroll_enabled=False)
        sid.enroll("Anthero", base)
        sid.embedding = lambda _pcm: self._at_cosine(base, 0.95)

        result = sid.auto_enroll(self._pcm())

        self.assertFalse(result["enrolled"])
        self.assertEqual(result["reason"], "desativado")
        self.assertFalse(sid.auto_enroll_enabled)


class LiveFingerprintTest(unittest.TestCase):
    """Fingerprint hygiene: trim, minimum voiced length and whole-clip retry.

    A deterministic stand-in embedder makes the experiment reproducible: its
    vector is ``base * voiced_fraction + junk * (1 - voiced_fraction)``, so
    padding the same speech with silence lowers the cosine against the
    enrolled ``base`` exactly like the real room tone does.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)
        self.sid._ensure_models = lambda: None
        self.base = _vec(21)
        self.junk = self._orthogonal(self.base, seed=22)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _orthogonal(base: np.ndarray, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        vector = rng.normal(size=base.shape)
        vector -= base * float(np.dot(vector, base))
        return vector / np.linalg.norm(vector)

    @staticmethod
    def _tone(seconds: float, freq: float = 220.0, amp: float = 0.2) -> np.ndarray:
        t = np.arange(int(speaker_id.SAMPLE_RATE * seconds)) / speaker_id.SAMPLE_RATE
        return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    @staticmethod
    def _silence(seconds: float) -> np.ndarray:
        return np.zeros(int(speaker_id.SAMPLE_RATE * seconds), dtype=np.float32)

    def _embedder(self, seen: list[int] | None = None):
        def embed(samples):
            arr = np.asarray(samples, dtype=np.float32).reshape(-1)
            if seen is not None:
                seen.append(int(arr.size))
            frame = int(0.02 * speaker_id.SAMPLE_RATE)
            hop = int(0.01 * speaker_id.SAMPLE_RATE)
            if arr.size >= frame:
                count = 1 + (arr.size - frame) // hop
                index = (np.arange(frame)[None, :]
                         + hop * np.arange(count)[:, None])
                rms = np.sqrt(np.mean(arr[index] ** 2, axis=1) + 1e-12)
                top = float(np.max(rms))
                voiced = (float(np.mean(rms >= max(0.004, 0.02 * top)))
                          if top > 1e-6 else 0.0)
            else:
                voiced = 1.0
            vector = (voiced * self.base
                      + float(np.sqrt(max(0.0, 1.0 - voiced ** 2))) * self.junk)
            return (vector / np.linalg.norm(vector)).tolist()
        return embed

    def _padded(self, seconds: float = 1.0, pad: float = 0.75) -> np.ndarray:
        return np.concatenate([self._silence(pad), self._tone(seconds),
                               self._silence(pad)])

    def test_trim_before_fingerprint_beats_the_padded_clip(self):
        self.sid.enroll("Anthero", self.base)
        padded = self._padded()
        contaminated = self._embedder()(padded)
        # The raw padded clip (pre-roll + room tone) is what used to be sent
        # to the extractor: it falls below the gate.
        self.assertFalse(self.sid.identify(contaminated)["known"])

        seen: list[int] = []
        self.sid._embed_samples = self._embedder(seen)
        emb = self.sid.embedding(padded)
        found = self.sid.identify(emb)

        self.assertTrue(found["known"])
        self.assertGreaterEqual(found["score"], 0.8)
        self.assertEqual(found["verdict"], speaker_id.VERDICT_KNOWN)
        # Only the voiced span (plus 50 ms of context) reached the extractor.
        self.assertTrue(seen)
        self.assertLess(seen[-1], padded.size)
        self.assertGreaterEqual(seen[-1], speaker_id.SAMPLE_RATE)

    def test_short_voiced_span_is_indecisive_not_unknown(self):
        self.sid.enroll("Anthero", self.base)
        self.sid._embed_samples = self._embedder()
        self.sid.diarize = lambda *a, **k: self.fail(
            "diarization must not run on an indecisive clip")
        clip = np.concatenate([self._silence(0.2), self._tone(0.5),
                               self._silence(0.2)])

        result = self.sid.analyze_utterance(clip, full_text="oi")

        self.assertEqual(result["verdict"], speaker_id.VERDICT_INDECISIVE)
        self.assertEqual(result["primary"]["verdict"],
                         speaker_id.VERDICT_INDECISIVE)
        self.assertFalse(result["primary"]["known"])
        self.assertIsNone(result["primary"]["name"])
        self.assertIsNone(result["unknown"])          # not a rejection
        self.assertIsNone(result["unauthorized"])
        self.assertEqual(result["labeled_text"], "oi")
        self.assertTrue(result["primary_pcm"])
        self.assertLess(result["voiced_seconds"], 0.8)

        short = self.sid.identify(self.base, voiced_seconds=0.5)
        self.assertFalse(short["known"])
        self.assertIsNone(short["name"])
        self.assertEqual(short["verdict"], speaker_id.VERDICT_INDECISIVE)
        long = self.sid.identify(self.base, voiced_seconds=1.2)
        self.assertTrue(long["known"])
        self.assertEqual(long["verdict"], speaker_id.VERDICT_KNOWN)

    def test_whole_clip_retry_wins_over_failed_tiny_segments(self):
        self.sid.enroll("Anthero", self.base)
        self.sid._embed_samples = lambda _samples: self.base.tolist()
        self.sid.diarize = lambda *a, **k: [
            {"start": 0.0, "end": 0.2, "speaker": 0,
             "embedding": self.junk.tolist()},
            {"start": 0.2, "end": 0.4, "speaker": 0,
             "embedding": self.junk.tolist()},
        ]
        clip = np.concatenate([self._silence(0.1), self._tone(1.0),
                               self._silence(0.1)])
        result = self.sid.analyze_utterance(clip, full_text="bom dia")

        self.assertEqual(result["verdict"], speaker_id.VERDICT_KNOWN)
        self.assertEqual(result["primary_name"], "Anthero")
        self.assertTrue(result["primary"]["authorized"])
        self.assertIsNone(result["unknown"],
                          "a failed tiny segment must not stay as the unknown")
        self.assertGreaterEqual(
            float(np.dot(np.asarray(result["primary_embedding"]), self.base)),
            0.9,
        )
        self.assertIn("[FALANTE: Anthero]", result["labeled_text"])


class RobustCentroidTest(unittest.TestCase):
    """The centroid follows the coherent cluster; no sample is ever deleted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.store.mkdir(parents=True)
        self.base = _vec(31)
        rng = np.random.default_rng(32)
        # Six good samples and five singletons, interleaved like the real
        # index (0,4,5,6,7,8 good / 1,2,3,9,10 junk).
        self.samples: list[np.ndarray] = []
        for index in range(11):
            if index in (0, 4, 5, 6, 7, 8):
                vector = self.base + 0.06 * rng.normal(size=self.base.shape)
            else:
                vector = rng.normal(size=self.base.shape)
                vector -= self.base * float(np.dot(vector, self.base))
            self.samples.append(vector / np.linalg.norm(vector))
        self.cluster = [0, 4, 5, 6, 7, 8]
        poisoned = np.mean(self.samples, axis=0)
        self.poisoned = poisoned / np.linalg.norm(poisoned)
        payload = {
            "version": 1,
            "threshold": speaker_id.DEFAULT_THRESHOLD,
            "owner": "Anthero",
            "speakers": {
                "anthero": {
                    "name": "Anthero",
                    "authorized": True,
                    "centroid": self.poisoned.tolist(),
                    "samples": [s.tolist() for s in self.samples],
                    "created": 1.0,
                    "updated": 1.0,
                }
            },
        }
        (self.store / "index.json").write_text(json.dumps(payload),
                                              encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_and_recompute_keep_all_samples_and_tighten_the_centroid(self):
        sid = speaker_id.SpeakerId(store_dir=self.store)
        trust = sid.sample_trust("Anthero")
        self.assertEqual(len(trust), 11)

        old = [float(np.dot(self.samples[i], self.poisoned))
               for i in self.cluster]
        new = [trust[i] for i in self.cluster]
        self.assertGreaterEqual(min(new), 0.5)
        self.assertGreater(float(np.mean(new)), float(np.mean(old)) + 0.02)
        self.assertLess(max(trust[i] for i in range(11)
                            if i not in self.cluster), 0.5)

        # Loading recomputes in memory only: the file still has every sample
        # and the old centroid.
        on_disk = json.loads((self.store / "index.json").read_text("utf-8"))
        person = on_disk["speakers"]["anthero"]
        self.assertEqual(len(person["samples"]), 11)
        self.assertEqual(person["centroid"], self.poisoned.tolist())

        report = sid.recompute_centroids(persist=True)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["samples"], 11)
        self.assertEqual(len(report[0]["trust"]), 11)
        self.assertTrue(set(report[0]["cluster"]) <= set(self.cluster))
        self.assertGreaterEqual(len(report[0]["cluster"]), 2)
        stored = json.loads((self.store / "index.json").read_text("utf-8"))
        self.assertEqual(len(stored["speakers"]["anthero"]["samples"]), 11)
        self.assertNotEqual(stored["speakers"]["anthero"]["centroid"],
                            self.poisoned.tolist())

        reopened = speaker_id.SpeakerId(store_dir=self.store)
        self.assertEqual(len(reopened.sample_trust("Anthero")), 11)
        self.assertTrue(reopened.identify(self.base)["known"])

    def test_sample_trust_on_unknown_person_raises(self):
        sid = speaker_id.SpeakerId(store_dir=self.store)
        with self.assertRaises(KeyError):
            sid.sample_trust("Ninguém")


class VoicedTrimFixtureTest(unittest.TestCase):
    def test_real_fixture_is_trimmed_to_its_voiced_span(self):
        import wave

        path = Path(__file__).resolve().parents[1] / "logs" / "ultima-fala-dormindo.wav"
        if not path.is_file():
            self.skipTest("fixture logs/ultima-fala-dormindo.wav ausente")
        with wave.open(str(path)) as handle:
            frames = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0

        trimmed, voiced_seconds = speaker_id._trim_to_voiced(samples, rate)

        self.assertLess(trimmed.size, samples.size)
        self.assertGreaterEqual(voiced_seconds, 0.8)
        self.assertLessEqual(voiced_seconds, trimmed.size / rate)


class GroqListenerCallbackTest(unittest.TestCase):
    def _wait(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_on_utterance_gets_text_and_pcm(self):
        got: list[tuple[str, bytes]] = []
        text_calls: list[str] = []
        listener = groq_stt.GroqListener(
            "key", "model", "pt",
            on_text=text_calls.append,
            on_utterance=lambda text, pcm: got.append((text, pcm)),
        )
        pcm = b"\x01\x00" * 1600
        try:
            with mock.patch.object(groq_stt, "transcribe", lambda *a, **k: "olá mundo"), \
                    mock.patch("core.usage.note", lambda *a, **k: None):
                listener._q.put(pcm)
                self.assertTrue(self._wait(lambda: bool(got)))
            self.assertEqual(got, [("olá mundo", pcm)])
            self.assertEqual(text_calls, [])
        finally:
            listener.stop()

    def test_on_text_is_used_without_on_utterance(self):
        text_calls: list[str] = []
        listener = groq_stt.GroqListener(
            "key", "model", "pt", on_text=text_calls.append
        )
        pcm = b"\x02\x00" * 1600
        try:
            with mock.patch.object(groq_stt, "transcribe", lambda *a, **k: "bom dia"), \
                    mock.patch("core.usage.note", lambda *a, **k: None):
                listener._q.put(pcm)
                self.assertTrue(self._wait(lambda: bool(text_calls)))
            self.assertEqual(text_calls, ["bom dia"])
        finally:
            listener.stop()

    def test_backward_compatible_signature_without_callback(self):
        listener = groq_stt.GroqListener(
            "key", "model", "pt", on_text=lambda text: None
        )
        self.assertIsNone(listener._on_utterance)
        listener.stop()


@unittest.skipUnless(speaker_id.available(), "diarization models not downloaded")
class DiarizationSmokeTest(unittest.TestCase):
    def test_silence_returns_a_list_of_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            sid = speaker_id.SpeakerId(store_dir=tmp)
            result = sid.diarize(np.zeros(16000, dtype=np.int16))
        self.assertIsInstance(result, list)
        for segment in result:
            self.assertEqual(
                set(segment), {"start", "end", "speaker", "embedding"}
            )
            self.assertIsInstance(segment["embedding"], list)


if __name__ == "__main__":
    unittest.main()
