"""The free-only model switch: paid catalog entries never reach the CLI."""
from __future__ import annotations

import json

import pytest

from core import free_models
from core import qoder


def _catalog(tmp_path, models):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    return path


def _model(value, display, *, free, factor):
    return {
        "value": value,
        "displayName": display,
        "isFree": free,
        "priceFactor": factor,
    }


@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    free_models.reset()
    monkeypatch.setattr(free_models, "CATALOG_PATH", tmp_path / "models.json")
    yield
    free_models.reset()


def test_paid_model_is_replaced_by_the_free_default(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("qfmodel", "Qwen3.8-Flash", free=True, factor=0),
        _model("dfmodel", "DeepSeek-Flash", free=False, factor=0.1),
    ]))
    chosen, previous = free_models.resolve("DeepSeek-Flash", "teste")
    assert (chosen, previous) == ("Qwen3.8-Flash", "DeepSeek-Flash")


def test_free_model_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("qfmodel", "Qwen3.8-Flash", free=True, factor=0),
        _model("dfmodel", "DeepSeek-Flash", free=False, factor=0.1),
    ]))
    assert free_models.resolve("Qwen3.8-Flash", "teste") == ("Qwen3.8-Flash", None)


def test_zero_multiplier_counts_as_free_even_without_the_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("efficient", "Efficient", free=False, factor=0),
    ]))
    assert free_models.resolve("Efficient", "teste") == ("Efficient", None)


def test_switch_is_announced_once(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("qfmodel", "Qwen3.8-Flash", free=True, factor=0),
        _model("dfmodel", "DeepSeek-Flash", free=False, factor=0.1),
    ]))
    seen = []
    free_models.register_listener(lambda *args: seen.append(args))
    free_models.resolve("DeepSeek-Flash", "teste")
    free_models.resolve("DeepSeek-Flash", "teste")
    assert seen == [("DeepSeek-Flash", "Qwen3.8-Flash",
                     "modelo pago no catálogo Qoder (teste)")]


def test_no_catalog_never_returns_a_paid_name():
    assert free_models.resolve("DeepSeek-Flash", "teste") == (
        "Qwen3.8-Flash", "DeepSeek-Flash")
    assert free_models.resolve("Auto", "teste") == (
        "Qwen3.8-Flash", "Auto")


def test_alternative_skips_the_current_model(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("qfmodel", "Qwen3.8-Flash", free=True, factor=0),
        _model("efficient", "Efficient", free=True, factor=0),
    ]))
    assert free_models.alternative("Qwen3.8-Flash") == "Efficient"
    assert free_models.alternative("Efficient") == "Qwen3.8-Flash"


def test_qoder_free_model_uses_the_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _catalog(tmp_path, [
        _model("qfmodel", "Qwen3.8-Flash", free=True, factor=0),
        _model("dfmodel", "DeepSeek-Flash", free=False, factor=0.1),
    ]))
    assert qoder.free_model("DeepSeek-Flash") == "Qwen3.8-Flash"
    assert qoder.HEAVY_MODEL == "Qwen3.8-Flash"
