"""Typed password authorization: hashed at rest, scoped to text channels."""
from __future__ import annotations

import json
import time

import pytest

import main
from core import owner_auth
from memory import config_manager


class _Timer:
    def __init__(self, *_a, **_k):
        self.daemon = False

    def start(self):
        pass


class _UI:
    muted = False

    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))


def _obj() -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj._owner_text_until = 0.0
    obj._owner_text_channel = ""
    obj._owner_text_fails = 0
    obj._owner_text_locked_until = 0.0
    obj._voice_bootstrap_until = 0.0
    obj._voice_bootstrap_by = ""
    obj.ui = _UI()
    obj.said = []
    obj._say_now = lambda text, **kw: obj.said.append(str(text))
    obj.sent = []
    obj._send_whatsapp_answer = lambda account, chat, text, media="text": (
        obj.sent.append((account, chat, text)) or True)
    return obj


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_manager, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(main.threading, "Timer", _Timer)
    monkeypatch.setattr(main._actiongate, "open_window", lambda *a, **k: 120.0)
    monkeypatch.setattr(main._actiongate, "arm_send_window",
                        lambda *a, **k: 900.0)
    yield


def _set_password(password: str) -> None:
    config_manager.save_plugin_config("owner_auth", {"password": password})


def test_password_is_hashed_at_rest():
    _set_password("segredo-forte")
    raw = json.loads((config_manager.CONFIG_FILE).read_text(encoding="utf-8"))
    stored = raw["plugin_config"]["owner_auth"]
    assert "password" not in stored
    assert stored["password_hash"].startswith("pbkdf2_sha256$")
    assert owner_auth.verify("segredo-forte") is True
    assert owner_auth.verify("errada") is False


def test_empty_save_keeps_the_existing_hash():
    _set_password("primeira")
    first = config_manager.get_plugin_config("owner_auth")["password_hash"]
    config_manager.save_plugin_config("owner_auth", {"password": ""})
    second = config_manager.get_plugin_config("owner_auth")["password_hash"]
    assert first == second


def test_correct_password_opens_typed_session_and_windows():
    _set_password("1234")
    obj = _obj()
    handled = main.JarvisLive._owner_text_gate(obj, "senha: 1234", "ui")
    assert handled is True
    assert main.JarvisLive._owner_text_active(obj) is True
    assert obj.said and "Autorizado por texto" in obj.said[-1]


def test_wrong_password_does_not_open_anything():
    _set_password("1234")
    obj = _obj()
    assert main.JarvisLive._owner_text_gate(obj, "senha: 9999", "ui") is True
    assert main.JarvisLive._owner_text_active(obj) is False
    assert "Senha incorreta" in obj.said[-1]


def test_whatsapp_answer_goes_back_to_the_chat():
    _set_password("1234")
    obj = _obj()
    main.JarvisLive._owner_text_gate(
        obj, "/autorizar 1234", "whatsapp", account="principal", chat="5511")
    assert obj.sent and obj.sent[-1][0:2] == ("principal", "5511")
    assert obj.said == []                       # não fala na sala


def test_five_failures_lock_the_password_for_a_minute():
    _set_password("1234")
    obj = _obj()
    for _ in range(5):
        main.JarvisLive._owner_text_gate(obj, "senha: errada", "ui")
    assert obj._owner_text_locked_until > time.monotonic()
    obj.said.clear()
    main.JarvisLive._owner_text_gate(obj, "senha: 1234", "ui")
    assert main.JarvisLive._owner_text_active(obj) is False
    assert "Muitas tentativas" in obj.said[-1]


def test_password_forms_are_recognized_and_redacted():
    for text in ("senha: 1234", "senha=1234", "autorizar: 1234",
                 "/autorizar 1234"):
        assert owner_auth.password_from_command(text) == "1234"
        assert owner_auth.redact_command(text) == owner_auth.REDACTED
    assert owner_auth.password_from_command("senha do wifi é outra") is None
    assert owner_auth.redact_command("qual a senha do wifi?") == \
        "qual a senha do wifi?"


def test_whatsapp_inbox_never_persists_the_password(tmp_path):
    from core import whatsapp as wa

    acct = type("Acct", (), {"inbox_file": tmp_path / "inbox.jsonl",
                             "appended": 0})()
    item = {"text": "senha: 1234", "chat": "dono"}
    wa._append_inbox(item, acct)

    raw = acct.inbox_file.read_text(encoding="utf-8")
    assert "1234" not in raw
    assert owner_auth.REDACTED in raw
    assert item["text"] == "senha: 1234"       # o gate ainda recebe a senha


def test_enrollment_requires_the_typed_password_when_configured():
    _set_password("1234")
    obj = _obj()
    assert main.JarvisLive._voice_bootstrap_gate(
        obj, "abrir cadastro de voz", None, "ui") is True
    assert main.JarvisLive._voice_bootstrap_active(obj) is False
    assert "Digite primeiro" in obj.said[-1]

    main.JarvisLive._owner_text_gate(obj, "senha: 1234", "ui")
    assert main.JarvisLive._voice_bootstrap_gate(
        obj, "abrir cadastro de voz", None, "ui") is True
    assert main.JarvisLive._voice_bootstrap_active(obj) is True
