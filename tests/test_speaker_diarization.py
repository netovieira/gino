"""Tests for full-utterance diarization labeling (no ONNX model needed)."""
from __future__ import annotations

import tempfile
import unittest

from core.speaker_id import SpeakerId


class AnalyzeUtteranceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="speaker-analyze-")
        self.speaker = SpeakerId(store_dir=self.tmp, owner_name="Anthero")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_segments_are_labeled_and_primary_is_last(self):
        pcm = b"\x01\x00" * 32_000  # 2 s at 16 kHz
        self.speaker.diarize = lambda _pcm, num_speakers=-1: [
            {"start": 0.0, "end": 1.0, "speaker": 0, "embedding": [1.0, 0.0]},
            {"start": 1.0, "end": 2.0, "speaker": 1, "embedding": [0.0, 1.0]},
        ]

        def identify(emb):
            if emb and emb[0] > emb[1]:
                return {"name": "Anthero", "score": 1.0, "known": True,
                        "authorized": True}
            return {"name": None, "score": 0.0, "known": False,
                    "authorized": False}

        self.speaker.identify = identify
        self.speaker.embedding = lambda _pcm: [0.0, 1.0]  # whole clip stays unknown
        texts = iter(["primeira parte", "segunda parte"])
        result = self.speaker.analyze_utterance(
            pcm, transcribe=lambda _pcm: next(texts, ""), full_text="completo")
        self.assertIn("[FALANTE: Anthero] primeira parte", result["labeled_text"])
        self.assertIn("[FALANTE: desconhecido] segunda parte", result["labeled_text"])
        self.assertEqual(result["primary_name"], "desconhecido")
        self.assertFalse(result["primary"]["known"])
        self.assertIsNotNone(result["unknown"])
        self.assertEqual(len(result["unknown"]["embedding"]), 2)
        self.assertGreater(len(result["unknown"]["pcm"]), 0)

    def test_known_but_unauthorized_is_reported(self):
        pcm = b"\x01\x00" * 16_000
        self.speaker.diarize = lambda _pcm, num_speakers=-1: [
            {"start": 0.0, "end": 1.0, "speaker": 0, "embedding": [1.0, 0.0]},
        ]
        self.speaker.identify = lambda _emb: {
            "name": "Heloisa", "score": 0.9, "known": True, "authorized": False}
        result = self.speaker.analyze_utterance(
            pcm, transcribe=lambda _pcm: "oi", full_text="oi")
        self.assertEqual(result["primary_name"], "Heloisa")
        self.assertEqual(result["unauthorized"]["name"], "Heloisa")
        self.assertIsNone(result["unknown"])

    def test_no_segments_falls_back_to_whole_utterance(self):
        pcm = b"\x01\x00" * 16_000
        self.speaker.diarize = lambda _pcm, num_speakers=-1: []
        self.speaker.embedding = lambda _pcm: [1.0, 0.0]
        self.speaker.identify = lambda _emb: {
            "name": "Anthero", "score": 1.0, "known": True, "authorized": True}
        result = self.speaker.analyze_utterance(pcm, full_text="texto")
        self.assertEqual(result["labeled_text"], "texto")
        self.assertEqual(result["primary_name"], "Anthero")
        self.assertIsNone(result["unknown"])


if __name__ == "__main__":
    unittest.main()
