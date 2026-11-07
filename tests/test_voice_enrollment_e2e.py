"""UC-7: fluxo completo de cadastro de voz (end-to-end, sem microfone).

Cobre os tres cenarios do FLUXO IDENTIFICACAO (FLUXOS.md):
  1. voz desconhecida -> cadastro -> evento voice_enrolled
  2. voz do dono em mic novo -> enroll_device -> evento voice_enrolled
  3. cadastro -> identificacao posterior -> evento voice_identified

Usa embeddings sinteticos (numpy); nenhum modelo ONNX e carregado.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


class EnrollmentE2ETest(unittest.TestCase):
    """Fluxo completo: enroll -> identify -> enroll_device -> identify."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unknown_voice_enroll_then_identify(self):
        """Cenario 1: voz desconhecida e cadastrada, depois identificada."""
        owner = _vec(1)
        result = self.sid.enroll("Anthero", owner)
        self.assertTrue(result["authorized"])
        self.assertTrue(result["owner"])
        self.assertEqual(result["samples"], 1)

        found = self.sid.identify(_near(owner))
        self.assertTrue(found["known"])
        self.assertEqual(found["name"], "Anthero")
        self.assertGreaterEqual(found["effective_score"], 0.45)

    def test_visitor_enroll_not_authorized(self):
        """Visitante cadastrado nao e autorizado por padrao."""
        visitor = _vec(10)
        result = self.sid.enroll("Valmira", visitor, authorized=False)
        self.assertFalse(result["authorized"])
        self.assertFalse(result["owner"])

        found = self.sid.identify(_near(visitor))
        self.assertTrue(found["known"])
        self.assertEqual(found["name"], "Valmira")
        self.assertFalse(found["authorized"])

    def test_device_enroll_owner_new_mic(self):
        """Cenario 3: dono em mic novo -> enroll_device -> identify."""
        owner = _vec(1)
        self.sid.enroll("Anthero", owner, device="pc")

        celular_vec = _vec(2)
        result = self.sid.enroll_device("Anthero", celular_vec, device="celular")
        self.assertEqual(result["device"], "celular")
        self.assertTrue(result["authorized"])

        found = self.sid.identify(_near(celular_vec), device="celular")
        self.assertTrue(found["known"])
        self.assertEqual(found["name"], "Anthero")

    def test_device_enroll_unknown_speaker_raises(self):
        """enroll_device em voz nao cadastrada levanta KeyError."""
        with self.assertRaises(KeyError):
            self.sid.enroll_device("Desconhecido", _vec(5), device="headset")

    def test_device_enroll_non_owner_celular_raises(self):
        """Perfil 'celular' e exclusivo do dono."""
        visitor = _vec(20)
        self.sid.enroll("Valmira", visitor, authorized=False)
        with self.assertRaises(PermissionError):
            self.sid.enroll_device("Valmira", _vec(21), device="celular")

    def test_reenroll_merges_samples(self):
        """Re-cadastrar junta amostras, nao remove autorizacao."""
        owner = _vec(1)
        self.sid.enroll("Anthero", owner)
        result = self.sid.enroll("Anthero", _near(owner, seed=50))
        self.assertEqual(result["samples"], 2)
        self.assertTrue(result["authorized"])

    def test_identify_unknown_returns_unknown_verdict(self):
        """Voz nao cadastrada retorna verdict='unknown'."""
        self.sid.enroll("Anthero", _vec(1))
        stranger = _vec(99)
        found = self.sid.identify(stranger)
        self.assertFalse(found["known"])
        self.assertIsNone(found["name"])
        self.assertEqual(found["verdict"], "unknown")

    def test_identify_no_profile_when_device_empty(self):
        """Dispositivo sem cadastro retorna verdict='no_profile'."""
        self.sid.enroll("Anthero", _vec(1), device="pc")
        found = self.sid.identify(_near(_vec(1)), device="celular")
        self.assertEqual(found["verdict"], "no_profile")
        self.assertFalse(found["known"])


class MultipleSpeakersTest(unittest.TestCase):
    """Dois falantes cadastrados: identificacao distingue."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name) / "voices"
        self.sid = speaker_id.SpeakerId(store_dir=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_two_speakers_identified_correctly(self):
        owner = _vec(1)
        visitor = _vec(50)
        self.sid.enroll("Anthero", owner)
        self.sid.enroll("Valmira", visitor, authorized=False)

        found_owner = self.sid.identify(_near(owner))
        self.assertEqual(found_owner["name"], "Anthero")
        self.assertTrue(found_owner["authorized"])

        found_visitor = self.sid.identify(_near(visitor))
        self.assertEqual(found_visitor["name"], "Valmira")
        self.assertFalse(found_visitor["authorized"])


if __name__ == "__main__":
    unittest.main()
