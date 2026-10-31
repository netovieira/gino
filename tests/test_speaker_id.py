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
            {"name": None, "score": 0.0, "known": False, "authorized": False},
        )

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
