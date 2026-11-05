"""Temporary enrollment window: typed text only, expires, never widens gates."""
from __future__ import annotations

import time

import pytest

import main


class _Timer:
    """No real 10-minute thread in tests."""

    def __init__(self, *_args, **_kwargs):
        self.daemon = False

    def start(self):
        pass


def _obj() -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj._voice_id_enabled = True
    obj._speaker = object()
    obj._voice_train = None
    obj._voice_train_offered = False
    obj._voice_bootstrap_until = 0.0
    obj._voice_bootstrap_by = ""
    obj._voice_train_owner_ok = lambda *_a, **_k: (
        False, 0.2, "score abaixo do limiar")
    obj._cinema_active_now = lambda: False
    obj._log_heard = lambda *_a, **_k: None
    obj.started = []
    obj._start_voice_training = lambda why="": obj.started.append(why)
    obj.said = []
    obj._say_now = lambda text, **kw: obj.said.append(text)
    obj.ui = type("UI", (), {
        "write_log": lambda self, text: None,
        "muted": False,
    })()
    return obj


@pytest.fixture(autouse=True)
def _no_timer(monkeypatch):
    monkeypatch.setattr(main.threading, "Timer", _Timer)


@pytest.fixture(autouse=True)
def _deterministic_password(monkeypatch):
    # Determinismo: o gate lê plugin_config real. Com senha salva na máquina,
    # "abrir cadastro de voz" digitado exige a sessão de texto antes — e o
    # teste mudava de comportamento conforme o config. A senha é sempre
    # stubada (ausente); nenhum segredo é lido ou gravado aqui.
    monkeypatch.setattr(main._owner_auth, "is_configured", lambda: False)


def test_typed_text_opens_the_window():
    obj = _obj()
    handled = main.JarvisLive._voice_bootstrap_gate(
        obj, "abrir cadastro de voz", None)
    assert handled is True
    assert main.JarvisLive._voice_bootstrap_active(obj) is True


def test_spoken_request_never_opens_the_window():
    obj = _obj()
    handled = main.JarvisLive._voice_bootstrap_gate(
        obj, "abrir cadastro de voz", b"\x00" * 3200)
    assert handled is True
    assert main.JarvisLive._voice_bootstrap_active(obj) is False


def test_typed_command_closes_the_window():
    obj = _obj()
    main.JarvisLive._voice_bootstrap_arm(obj, "teste")
    assert main.JarvisLive._voice_bootstrap_active(obj) is True
    main.JarvisLive._voice_bootstrap_gate(obj, "fechar cadastro de voz", None)
    assert main.JarvisLive._voice_bootstrap_active(obj) is False


def test_unrelated_text_is_not_consumed():
    obj = _obj()
    assert main.JarvisLive._voice_bootstrap_gate(
        obj, "qual é a previsão do tempo", None) is False
    assert main.JarvisLive._voice_bootstrap_active(obj) is False


def test_expired_window_is_closed():
    obj = _obj()
    obj._voice_bootstrap_until = time.monotonic() - 1.0
    assert main.JarvisLive._voice_bootstrap_active(obj) is False


def test_training_trigger_bypasses_voiceprint_only_inside_the_window():
    obj = _obj()
    pcm = b"\x00" * 3200
    assert main.JarvisLive._voice_train_gate(
        obj, "cadastra minha voz", pcm) is False
    assert obj.started == []

    main.JarvisLive._voice_bootstrap_arm(obj, "teste")
    assert main.JarvisLive._voice_train_gate(
        obj, "cadastra minha voz", pcm) is True
    assert obj.started == ["pedido do dono"]


def test_feature_request_is_still_not_enrollment_inside_the_window():
    obj = _obj()
    main.JarvisLive._voice_bootstrap_arm(obj, "teste")
    assert main.JarvisLive._voice_train_gate(
        obj, "implemente uma ferramenta para registrar minha voz",
        b"\x00" * 3200) is False
    assert obj.started == []
