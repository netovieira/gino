"""Boot do WhatsApp: tentar cada conta e falar só o que precisa de mão humana.

Pedido do dono (02/11/2026): "quando você inicia, você já conecta nelas; se não
conseguir, me avisa no boot que não conseguiu, que eu vou lá e conecto na mão".
Aqui o teste olha o estado REAL devolvido por `connect_all` (nunca o "pedido
aceito") e o aviso falado do main: uma frase por conta caída, silêncio quando
todas conectam.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import whatsapp


class FakeUI:
    def __init__(self):
        self.muted = False
        self.lines: list[str] = []
        self.opened = 0

    def write_log(self, text) -> None:
        self.lines.append(str(text))

    def open_whatsapp(self) -> None:
        self.opened += 1


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    original = dict(whatsapp._accounts)
    principal = whatsapp._Account("principal", "uuid-principal", default=True)
    trabalho = whatsapp._Account("trabalho", "uuid-trabalho")
    for i, acct in enumerate((principal, trabalho), start=1):
        acct.dir = tmp_path / f"conta{i}"
        acct.inbox_file = tmp_path / f"inbox{i}.jsonl"
        acct.media_dir = tmp_path / f"media{i}"
    whatsapp._accounts.clear()
    whatsapp._accounts.update({a.uuid: a for a in (principal, trabalho)})
    monkeypatch.setattr(whatsapp, "_ensure_accounts", lambda: None)
    monkeypatch.setattr(whatsapp, "available", lambda: True)
    yield {"principal": principal, "trabalho": trabalho}
    whatsapp._accounts.clear()
    whatsapp._accounts.update(original)


def _live() -> main.JarvisLive:
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    obj.said: list[tuple[str, dict]] = []
    obj._say_now = lambda text, **kwargs: obj.said.append((str(text), kwargs))
    obj._wait_boot_speech_ready = lambda *a, **k: True
    return obj


# ── core.whatsapp.connect_all: uma tentativa por conta, estado real ─────────

def test_boot_attempts_every_enabled_account_once(accounts, monkeypatch):
    calls: list[str] = []
    lock = threading.Lock()

    def fake_start(acct, on_message=None):
        with lock:
            calls.append(acct.name)
        acct.connected = True
        return True, "conectado"

    monkeypatch.setattr(whatsapp, "_start_account", fake_start)

    states = whatsapp.connect_all(settle_s=0.0)

    assert sorted(calls) == ["principal", "trabalho"]
    assert [s["state"] for s in states] == ["connected", "connected"]
    assert all(s["connected"] for s in states)


def test_failed_account_reports_real_state_not_a_receipt(accounts, monkeypatch):
    def fake_start(acct, on_message=None):
        if acct.name == "principal":
            acct.connected = True
        else:
            acct.error = "falha na conexão: sessão vencida"
        return True, "pedido aceito"       # o recibo NÃO vira sucesso

    monkeypatch.setattr(whatsapp, "_start_account", fake_start)

    states = whatsapp.connect_all(settle_s=0.0)
    by_name = {s["name"]: s for s in states}

    assert by_name["principal"]["connected"] is True
    assert by_name["trabalho"]["connected"] is False
    assert by_name["trabalho"]["state"] == "error"
    assert "sessão vencida" in by_name["trabalho"]["detail"]


def test_account_waiting_for_qr_is_not_authenticated(accounts, monkeypatch):
    def fake_start(acct, on_message=None):
        if acct.name == "trabalho":
            acct.qr_path = "/tmp/qr.png"
        else:
            acct.connected = True
        return True, "conectando"

    monkeypatch.setattr(whatsapp, "_start_account", fake_start)

    states = whatsapp.connect_all(settle_s=0.0)
    by_name = {s["name"]: s for s in states}

    assert by_name["trabalho"]["connected"] is False
    assert by_name["trabalho"]["state"] == "auth"
    assert by_name["trabalho"]["qr"] is True


def test_missing_neonize_is_an_error_and_no_attempt_is_made(accounts, monkeypatch):
    monkeypatch.setattr(whatsapp, "available", lambda: False)

    def boom(*_a, **_k):
        raise AssertionError("não deveria tentar sem neonize")

    monkeypatch.setattr(whatsapp, "_start_account", boom)

    states = whatsapp.connect_all(settle_s=0.0)

    assert len(states) == 2
    assert all(s["state"] == "error" and not s["connected"] for s in states)


# ── aviso falado do main ────────────────────────────────────────────────────

def test_one_spoken_notice_per_failed_account_names_it():
    obj = _live()
    states = [
        {"name": "principal", "connected": True, "state": "connected"},
        {"name": "trabalho", "connected": False, "state": "auth"},
    ]

    lines = main.JarvisLive._whatsapp_startup_notice(obj, states)

    assert len(lines) == 1
    assert "trabalho" in lines[0]
    assert "conecta na mão" in lines[0]
    # É fala: nada de caminho, módulo, ferramenta, JSON ou log.
    assert "/" not in lines[0]
    assert "whatsapp.py" not in lines[0]
    assert "{" not in lines[0]
    assert len(obj.said) == 1
    assert obj.said[0][0] == lines[0]
    assert obj.said[0][1].get("origin") == "whatsapp"
    # O painel abre para ele escanear o QR.
    assert obj.ui.opened == 1


def test_all_connected_stays_silent_at_boot():
    obj = _live()
    states = [
        {"name": "principal", "connected": True, "state": "connected"},
        {"name": "trabalho", "connected": True, "state": "connected"},
    ]

    lines = main.JarvisLive._whatsapp_startup_notice(obj, states)

    assert lines == []
    assert obj.said == []
    assert obj.ui.opened == 0


def test_two_failures_are_two_short_sentences():
    obj = _live()
    states = [
        {"name": "principal", "connected": False, "state": "error"},
        {"name": "trabalho", "connected": False, "state": "auth"},
    ]

    lines = main.JarvisLive._whatsapp_startup_notice(obj, states)

    assert len(lines) == 2
    assert "principal" in lines[0]
    assert "trabalho" in lines[1]
    assert len(obj.said) == 2


def test_notice_becomes_panel_text_when_audio_is_not_ready_yet():
    obj = _live()
    obj._wait_boot_speech_ready = lambda *a, **k: False
    states = [{"name": "trabalho", "connected": False, "state": "auth"}]

    lines = main.JarvisLive._whatsapp_startup_notice(obj, states)

    assert len(lines) == 1
    assert obj.said == []
    assert any("trabalho" in line for line in obj.ui.lines)


def test_start_whatsapp_uses_connect_all_and_speaks_the_failure(
        accounts, monkeypatch):
    obj = _live()
    seen: list[dict] = []

    monkeypatch.setattr(
        whatsapp, "connect_all",
        lambda on_message=None, logger=None: [
            {"name": "principal", "connected": True, "state": "connected"},
            {"name": "trabalho", "connected": False, "state": "auth"},
        ])
    monkeypatch.setattr(
        whatsapp, "status",
        lambda *a, **k: {"qr_path": "", "connected": True, "number": "5517"})
    monkeypatch.setattr(whatsapp, "ffmpeg_ready", lambda: True)
    obj._whatsapp_startup_notice = (
        lambda states: seen.append(list(states)) or [])

    main.JarvisLive._start_whatsapp(obj)

    assert len(seen) == 1
    assert [s["name"] for s in seen[0]] == ["principal", "trabalho"]
