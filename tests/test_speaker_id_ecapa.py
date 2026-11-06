"""Tests for FASE2-TASK2: ECAPA-TDNN migration with ERes2Net fallback.

Metrics of success:
  - ECAPA-TDNN is preferred when the model file exists
  - ERes2Net is used as fallback when ECAPA is absent
  - Threshold auto-adjusts based on active model
  - ``using_ecapa`` property reflects the active model
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from core import speaker_id


# -- constants -----------------------------------------------------------------

def test_ecapa_constant_defined():
    """ECAPA_EMBEDDING_MODEL points to the expected path."""
    assert speaker_id.ECAPA_EMBEDDING_MODEL.name == "ecapa_tdnn.onnx"
    assert speaker_id.ECAPA_EMBEDDING_MODEL.parent == speaker_id.DIARIZATION_DIR


def test_fallback_constant_defined():
    """FALLBACK_EMBEDDING_MODEL is the ERes2Net file."""
    assert speaker_id.FALLBACK_EMBEDDING_MODEL.name == "3dspeaker_eres2net_base.onnx"


def test_default_threshold_is_eres2net():
    """DEFAULT_THRESHOLD matches the ERes2Net threshold (backward compat)."""
    assert speaker_id.DEFAULT_THRESHOLD == speaker_id.ERES2NET_THRESHOLD


def test_ecapa_threshold_higher():
    """ECAPA threshold is higher than ERes2Net (more discriminative model)."""
    assert speaker_id.ECAPA_THRESHOLD > speaker_id.ERES2NET_THRESHOLD


# -- active_embedding_model ----------------------------------------------------

def test_active_embedding_model_returns_path():
    """``active_embedding_model()`` returns a Path."""
    result = speaker_id.active_embedding_model()
    assert isinstance(result, Path)


def test_active_embedding_model_prefers_ecapa_when_present():
    """When ECAPA file exists, it is the active model."""
    with patch.object(speaker_id.Path, "is_file", return_value=True):
        result = speaker_id.active_embedding_model()
        assert result == speaker_id.ECAPA_EMBEDDING_MODEL


def test_active_embedding_model_falls_back_to_eres2net():
    """When ECAPA file is absent, ERes2Net is the active model."""
    def fake_is_file(self):
        return str(self) == str(speaker_id.FALLBACK_EMBEDDING_MODEL)
    with patch.object(Path, "is_file", fake_is_file):
        result = speaker_id.active_embedding_model()
        assert result == speaker_id.FALLBACK_EMBEDDING_MODEL


# -- is_ecapa_active -----------------------------------------------------------

def test_is_ecapa_active_returns_bool():
    """``is_ecapa_active()`` returns True or False."""
    result = speaker_id.is_ecapa_active()
    assert isinstance(result, bool)


# -- available() ---------------------------------------------------------------

def test_available_true_when_both_models_present():
    """``available()`` returns True when segmentation + one embedding exist."""
    def fake_is_file(self):
        return True
    with patch.object(Path, "is_file", fake_is_file):
        assert speaker_id.available() is True


def test_available_false_when_segmentation_missing():
    """``available()`` returns False when the segmentation model is absent."""
    def fake_is_file(self):
        return str(self) != str(speaker_id.DEFAULT_SEGMENTATION_MODEL)
    with patch.object(Path, "is_file", fake_is_file):
        assert speaker_id.available() is False


def test_available_true_with_only_fallback_embedding():
    """``available()`` returns True when only ERes2Net exists (no ECAPA)."""
    def fake_is_file(self):
        return str(self) == str(speaker_id.FALLBACK_EMBEDDING_MODEL) or \
               str(self) == str(speaker_id.DEFAULT_SEGMENTATION_MODEL)
    with patch.object(Path, "is_file", fake_is_file):
        assert speaker_id.available() is True


# -- SpeakerId instance --------------------------------------------------------

def test_speaker_id_auto_threshold_ecapa(tmp_path):
    """When ECAPA is active, threshold defaults to ECAPA_THRESHOLD."""
    seg = tmp_path / "seg.onnx"
    seg.touch()
    ecapa = tmp_path / "ecapa_tdnn.onnx"
    ecapa.touch()
    with patch.object(speaker_id, "DEFAULT_SEGMENTATION_MODEL", seg), \
         patch.object(speaker_id, "ECAPA_EMBEDDING_MODEL", ecapa), \
         patch.object(speaker_id, "FALLBACK_EMBEDDING_MODEL", tmp_path / "no.onnx"), \
         patch.object(speaker_id, "DEFAULT_EMBEDDING_MODEL", tmp_path / "no.onnx"):
        sid = speaker_id.SpeakerId(
            segmentation_model=seg,
            store_dir=tmp_path / "store",
        )
        assert sid.threshold == speaker_id.ECAPA_THRESHOLD
        assert sid.using_ecapa is True


def test_speaker_id_auto_threshold_eres2net(tmp_path):
    """When only ERes2Net is available, threshold defaults to ERES2NET_THRESHOLD."""
    seg = tmp_path / "seg.onnx"
    seg.touch()
    eres = tmp_path / "3dspeaker_eres2net_base.onnx"
    eres.touch()
    with patch.object(speaker_id, "DEFAULT_SEGMENTATION_MODEL", seg), \
         patch.object(speaker_id, "ECAPA_EMBEDDING_MODEL", tmp_path / "no.onnx"), \
         patch.object(speaker_id, "FALLBACK_EMBEDDING_MODEL", eres), \
         patch.object(speaker_id, "DEFAULT_EMBEDDING_MODEL", eres):
        sid = speaker_id.SpeakerId(
            segmentation_model=seg,
            store_dir=tmp_path / "store",
        )
        assert sid.threshold == speaker_id.ERES2NET_THRESHOLD
        assert sid.using_ecapa is False


def test_speaker_id_explicit_threshold_overrides(tmp_path):
    """An explicit threshold value overrides the auto-selection."""
    seg = tmp_path / "seg.onnx"
    seg.touch()
    ecapa = tmp_path / "ecapa_tdnn.onnx"
    ecapa.touch()
    with patch.object(speaker_id, "DEFAULT_SEGMENTATION_MODEL", seg), \
         patch.object(speaker_id, "ECAPA_EMBEDDING_MODEL", ecapa), \
         patch.object(speaker_id, "FALLBACK_EMBEDDING_MODEL", tmp_path / "no.onnx"), \
         patch.object(speaker_id, "DEFAULT_EMBEDDING_MODEL", tmp_path / "no.onnx"):
        sid = speaker_id.SpeakerId(
            segmentation_model=seg,
            threshold=0.60,
            store_dir=tmp_path / "store",
        )
        assert sid.threshold == 0.60


def test_using_ecapa_property(tmp_path):
    """``using_ecapa`` reflects the active model."""
    seg = tmp_path / "seg.onnx"
    seg.touch()
    eres = tmp_path / "3dspeaker_eres2net_base.onnx"
    eres.touch()
    with patch.object(speaker_id, "DEFAULT_SEGMENTATION_MODEL", seg), \
         patch.object(speaker_id, "ECAPA_EMBEDDING_MODEL", tmp_path / "no.onnx"), \
         patch.object(speaker_id, "FALLBACK_EMBEDDING_MODEL", eres), \
         patch.object(speaker_id, "DEFAULT_EMBEDDING_MODEL", eres):
        sid = speaker_id.SpeakerId(
            segmentation_model=seg,
            store_dir=tmp_path / "store",
        )
        assert sid.using_ecapa is False


# -- embedding dimension mismatch (re-enrollment) ------------------------------

def test_identify_handles_dimension_mismatch(tmp_path):
    """When embedding dimensions differ (model change), identify returns unknown.

    This simulates the re-enrollment scenario: old embeddings were computed
    with ERes2Net (dim=192) but the model is now ECAPA-TDNN (dim=192 or 512).
    The ``identify`` method must not crash on a dimension mismatch.
    """
    seg = tmp_path / "seg.onnx"
    seg.touch()
    eres = tmp_path / "3dspeaker_eres2net_base.onnx"
    eres.touch()
    with patch.object(speaker_id, "DEFAULT_SEGMENTATION_MODEL", seg), \
         patch.object(speaker_id, "ECAPA_EMBEDDING_MODEL", tmp_path / "no.onnx"), \
         patch.object(speaker_id, "FALLBACK_EMBEDDING_MODEL", eres), \
         patch.object(speaker_id, "DEFAULT_EMBEDDING_MODEL", eres):
        sid = speaker_id.SpeakerId(
            segmentation_model=seg,
            store_dir=tmp_path / "store",
        )
        # Enroll with a 192-dim embedding (ERes2Net size).
        sid.enroll("test_user", [np.random.randn(192).tolist()])
        # Now try to identify with a 512-dim embedding (ECAPA size).
        result = sid.identify(np.random.randn(512))
        # Should not crash; should return unknown (dimension mismatch).
        assert result["known"] is False
        assert result["name"] is None
