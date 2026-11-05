"""A credit error must switch to a free model, never retry the same one."""
from __future__ import annotations

import json
import threading

import pytest

import main
from core import free_models


def _write_catalog(tmp_path, models):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_models(monkeypatch, tmp_path):
    free_models.reset()
    monkeypatch.setattr(free_models, "CATALOG_PATH", tmp_path / "models.json")
    yield
    free_models.reset()


def _free(value, display):
    return {"value": value, "displayName": display,
            "isFree": True, "priceFactor": 0}


def _paid(value, display):
    return {"value": value, "displayName": display,
            "isFree": False, "priceFactor": 0.1}


def _live_obj():
    obj = object.__new__(main.JarvisLive)
    obj._qoder_model = "Qwen3.8-Flash"
    obj._qoder_heavy_model = "Qwen3.8-Flash"
    obj._qoder_credit_switch_at = 0.0
    obj._qoder_credit_blocked = False
    obj._qoder_live = None
    obj._qoder_lock = threading.RLock()
    obj._qoder_shutting_down = False
    obj.ui = type("UI", (), {"write_log": lambda self, text: None})()
    obj._say_now = lambda *a, **k: None
    obj._schedule_qoder_restart = lambda: None
    return obj


def test_credit_error_switches_to_the_other_free_model(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _write_catalog(tmp_path, [
        _free("qfmodel", "Qwen3.8-Flash"),
        _free("efficient", "Efficient"),
        _paid("dfmodel", "DeepSeek-Flash"),
    ]))
    saved = []
    monkeypatch.setattr(main, "save_qoder_live_model",
                        lambda name: saved.append(name))
    obj = _live_obj()

    handled = main.JarvisLive._switch_qoder_after_credit(
        obj, "You've reached your credit usage limit.", "conversa")

    assert handled is True
    assert obj._qoder_model == "Efficient"
    assert saved == ["Efficient"]
    assert obj._qoder_credit_blocked is False


def test_subagent_credit_error_persists_the_heavy_model(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _write_catalog(tmp_path, [
        _free("qfmodel", "Qwen3.8-Flash"),
        _free("efficient", "Efficient"),
    ]))
    saved = []
    monkeypatch.setattr(main, "save_qoder_heavy_model",
                        lambda name: saved.append(name))
    obj = _live_obj()

    main.JarvisLive._switch_qoder_after_credit(
        obj, "credits are depleted", "subagente")

    assert obj._qoder_heavy_model == "Efficient"
    assert saved == ["Efficient"]


def test_no_free_alternative_blocks_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(free_models, "CATALOG_PATH", _write_catalog(tmp_path, [
        _free("qfmodel", "Qwen3.8-Flash"),
    ]))
    obj = _live_obj()
    retries = []
    obj._schedule_credit_retry = lambda: retries.append(1)

    main.JarvisLive._switch_qoder_after_credit(
        obj, "credit usage limit", "conversa")

    assert obj._qoder_model == "Qwen3.8-Flash"
    assert obj._qoder_credit_blocked is True
    assert retries == [1]


def test_credit_error_pattern_matches_the_sdk_message():
    for message in (
        "You've reached your credit usage limit. Please upgrade your subscription plan.",
        "credits are depleted",
        "insufficient credits",
        "402 payment required",
    ):
        assert main._QODER_CREDIT_RE.search(message)
    assert not main._QODER_CREDIT_RE.search("Operation aborted")
