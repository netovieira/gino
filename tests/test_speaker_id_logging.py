"""UC-7: validacao dos eventos voice_identified e voice_enrolled no activity.log.

Garante que:
  - enroll() grava evento voice_enrolled com person, device, samples
  - enroll_device() grava evento voice_enrolled com device label
  - identify() com match positivo grava evento voice_identified com score
  - identify() sem match NAO grava voice_identified
  - _log_activity() e silencioso em falha de disco (nao quebra o fluxo)

Usa embeddings sinteticos; nenhum modelo ONNX e carregado.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core import speaker_id


def _vec(seed: int, dim: int = 8) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)


def _near(base: np.ndarray, seed: int = 99, mix: float = 0.03) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = base + mix * rng.normal(size=base.shape)
    return v / np.linalg.norm(v)


class VoiceEnrolledLoggingTest(unittest.TestCase):
    """Evento voice_enrolled gravado ao cadastrar."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.log_path = Path(self._tmp.name) / "activity.log"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)
        self._patcher = mock.patch.object(
            speaker_id, "_ACTIVITY_LOG_PATH", self.log_path)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def test_enroll_logs_voice_enrolled_event(self):
        self.sid.enroll("Anthero", _vec(1))
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("event=voice_enrolled", content)
        self.assertIn("person='Anthero'", content)
        self.assertIn("device=pc", content)
        self.assertIn("samples=1", content)
        self.assertIn("is_owner=True", content)

    def test_enroll_visitor_logs_is_owner_false(self):
        self.sid.enroll("Valmira", _vec(10), authorized=False)
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("event=voice_enrolled", content)
        self.assertIn("person='Valmira'", content)
        self.assertIn("is_owner=False", content)

    def test_enroll_device_logs_device_label(self):
        self.sid.enroll("Anthero", _vec(1), device="pc")
        self.log_path.write_text("", encoding="utf-8")  # limpa
        self.sid.enroll_device("Anthero", _vec(2), device="celular")
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("event=voice_enrolled", content)
        self.assertIn("device=celular", content)
        self.assertIn("append=False", content)


class VoiceIdentifiedLoggingTest(unittest.TestCase):
    """Evento voice_identified gravado na identificacao positiva."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.log_path = Path(self._tmp.name) / "activity.log"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)
        self._patcher = mock.patch.object(
            speaker_id, "_ACTIVITY_LOG_PATH", self.log_path)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def test_identify_known_logs_voice_identified(self):
        owner = _vec(1)
        self.sid.enroll("Anthero", owner)
        self.log_path.write_text("", encoding="utf-8")  # limpa
        found = self.sid.identify(_near(owner))
        self.assertTrue(found["known"])
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("event=voice_identified", content)
        self.assertIn("person='Anthero'", content)
        self.assertIn("score=", content)
        self.assertIn("verdict=known", content)

    def test_identify_unknown_does_not_log_voice_identified(self):
        self.sid.enroll("Anthero", _vec(1))
        self.log_path.write_text("", encoding="utf-8")  # limpa
        found = self.sid.identify(_vec(99))
        self.assertFalse(found["known"])
        content = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("event=voice_identified", content)

    def test_identify_unknown_no_enrollment_does_not_log(self):
        found = self.sid.identify(_vec(1))
        self.assertFalse(found["known"])
        if self.log_path.exists():
            content = self.log_path.read_text(encoding="utf-8")
            self.assertNotIn("event=voice_identified", content)


class LogActivityRobustnessTest(unittest.TestCase):
    """_log_activity e silencioso em falha."""

    def test_log_activity_swallows_exception(self):
        """Diretorio inacessivel nao levanta excecao."""
        bad_path = Path("/dev/null/impossible/activity.log")
        with mock.patch.object(speaker_id, "_ACTIVITY_LOG_PATH", bad_path):
            speaker_id._log_activity("event=test")  # nao deve levantar


if __name__ == "__main__":
    unittest.main()
