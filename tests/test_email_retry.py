"""Reconexão do e-mail: backoff real, diagnóstico sem segredo e aviso contido.

Nada aqui abre rede ou usa credencial real: o IMAP é um fake e o cofre é um
dicionário em memória. O que estes testes prendem:

  * a espera cresce de verdade (5, 15, 45, 135, 240 s) e para no teto;
  * falha transitória recupera na tentativa seguinte, sem desligar a conta;
  * falha persistente continua tentando e NUNCA vira `auto_start=False`;
  * o log/state.json guarda etapa, tipo, host/porta e duração, sem a senha;
  * o dono só é avisado na SEGUNDA falha consecutiva, e uma vez na volta.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import email as mail


class FakeIMAP:
    """O mínimo que `_attempt` usa: select e logout."""

    def __init__(self):
        self.selected = None
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return "OK", [b"1"]

    def logout(self):
        self.logged_out = True
        return "BYE", []


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Config/cofre em memória e diretórios temporários (nada real é tocado)."""
    data: dict[str, dict] = {}

    def get_cfg(namespace):
        return dict(data.get(namespace, {}))

    def save_cfg(namespace, values):
        data.setdefault(namespace, {}).update(values)

    import memory.config_manager as cm
    monkeypatch.setattr(cm, "get_plugin_config", get_cfg)
    monkeypatch.setattr(cm, "save_plugin_config", save_cfg)
    monkeypatch.setattr(cm, "get_plugin_setting",
                        lambda ns, key, default=None: get_cfg(ns).get(key, default))
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")

    monkeypatch.setattr(mail, "_STATE_ROOT", tmp_path / "config" / "mail")
    monkeypatch.setattr(mail, "_MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(mail, "PENDING_PATH",
                        tmp_path / "memory" / "mail_send_pending.json")
    mail.reset()
    mail._retry_stop.clear()          # reset() para as threads; o teste começa limpo
    mail._retry_threads.clear()
    yield data
    mail.reset()


def _account(name: str = "gmail", address: str = "eu@gmail.com",
             password: str = "segredo-de-app-123"):
    acct = mail.add_account(name, address, provider="gmail")
    mail.set_app_password(name, password)
    return acct


def _no_jitter(monkeypatch):
    monkeypatch.setattr(mail.random, "uniform", lambda _a, _b: 0.0)


def _instant_wait(monkeypatch, sink: list[float] | None = None):
    def fake_wait(seconds):
        if sink is not None:
            sink.append(float(seconds))
        return False
    monkeypatch.setattr(mail, "_retry_wait", fake_wait)


# ── a espera cresce e tem teto ──────────────────────────────────────────────

def test_backoff_ladder_has_real_exponential_growth_and_cap(monkeypatch):
    _no_jitter(monkeypatch)
    assert [mail._backoff_delay(i) for i in range(1, 8)] == [
        5.0, 15.0, 45.0, 135.0, 240.0, 240.0, 240.0,
    ]


def test_backoff_has_jitter_but_never_exceeds_the_cap(monkeypatch):
    monkeypatch.setattr(mail.random, "uniform", lambda _a, _b: 0.2)
    assert mail._backoff_delay(1) == pytest.approx(6.0)
    assert mail._backoff_delay(6) == pytest.approx(240.0)
    monkeypatch.setattr(mail.random, "uniform", lambda _a, _b: -0.2)
    assert mail._backoff_delay(1) == pytest.approx(4.0)
    assert mail._backoff_delay(6) == pytest.approx(192.0)


# ── retry de verdade, sem desligar a conta ──────────────────────────────────

def test_connect_all_retry_recovers_on_the_second_attempt(env, monkeypatch):
    """Primeira tentativa falha; o worker reconecta sozinho e não avisa nada."""
    _account()
    calls: list[int] = []

    def flaky(_acct):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("network is unreachable")
        return FakeIMAP()

    waits: list[float] = []
    monkeypatch.setattr(mail, "_imap_open", flaky)
    _no_jitter(monkeypatch)
    _instant_wait(monkeypatch, waits)
    notices: list[dict] = []
    mail.set_retry_notice(notices.append)

    states = mail.connect_all(settle_s=0.1)
    assert states[0]["state"] == "error"
    acct = mail._find("gmail")
    deadline = time.time() + 2.0
    while acct.state != "connected" and time.time() < deadline:
        time.sleep(0.01)

    assert acct.state == "connected"
    assert len(calls) == 2, "recuperou na tentativa seguinte, sem repetir à toa"
    assert waits and waits[0] == pytest.approx(5.0)
    assert notices == [], "primeira falha é silêncio"
    assert acct.auto_start is True


def test_persistent_failure_reaches_backoff_cap_without_disabling(env,
                                                                 monkeypatch):
    """Falha de rede persistente: tenta para sempre, avisa UMA vez, não desliga."""
    acct = _account()
    _no_jitter(monkeypatch)
    attempts: list[int] = []

    def unreachable(_acct):
        attempts.append(1)
        raise OSError("network is unreachable")

    monkeypatch.setattr(mail, "_imap_open", unreachable)
    waits: list[float] = []
    _instant_wait(monkeypatch, waits)
    notices: list[dict] = []
    mail.set_retry_notice(notices.append)

    mail._attempt(acct)                  # falha nº 1 (boot), silêncio
    assert notices == []
    mail._retry_worker(acct, max_attempts=5)

    assert len(attempts) == 6, "boot + 5 tentativas do worker"
    assert waits == [5.0, 15.0, 45.0, 135.0, 240.0]
    assert [n["kind"] for n in notices] == ["down"]
    assert "sigo tentando" in notices[0]["message"]
    assert acct.auto_start is True, "falha transitória não desabilita a conta"
    assert acct.state == "error"
    assert mail._load_entries()[0]["auto_start"] is True
    assert not any("desligad" in str(n).lower() for n in notices)


def test_auth_failure_keeps_account_enabled_and_does_not_loop(env, monkeypatch):
    """Senha recusada não é rede: pausa a insistência, mas mantém habilitada."""
    import imaplib
    _account(password="senha-errada")

    def refused(_acct):
        raise imaplib.IMAP4.error(
            "[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    monkeypatch.setattr(mail, "_imap_open", refused)
    notices: list[dict] = []
    mail.set_retry_notice(notices.append)

    states = mail.connect_all(settle_s=0.1)
    assert states[0]["state"] == "auth"
    assert mail._retry_active() is False, "auth não entra em retry infinito"
    assert states[0]["auto_start"] is True
    assert mail._load_entries()[0]["auto_start"] is True


# ── diagnóstico seguro ──────────────────────────────────────────────────────

def test_error_detail_is_logged_without_the_secret(env, monkeypatch):
    secret = "segredo-de-app-123"
    acct = _account(password=secret)

    def unreachable(_acct):
        raise OSError(f"connection refused while logging in with {secret}")

    monkeypatch.setattr(mail, "_imap_open", unreachable)
    messages: list[str] = []
    monkeypatch.setattr(mail, "_logger", messages.append)

    entry = mail._attempt(acct)

    assert entry["state"] == "error"
    log = "\n".join(messages)
    assert "tentativa 1" in log
    assert "OSError" in log
    assert "etapa tcp" in log
    assert "imap.gmail.com:993" in log
    assert "ms" in log
    assert secret not in log, "a senha nunca entra no log"

    assert acct.state_file.exists()
    raw = acct.state_file.read_text(encoding="utf-8")
    assert secret not in raw
    state = json.loads(raw)                      # escrita atômica: JSON válido
    assert state["state"] == "error"
    assert state["stage"] == "tcp"
    assert state["error_type"] == "OSError"
    assert state["host"] == "imap.gmail.com"
    assert state["port"] == 993
    assert state["duration_ms"] >= 0
    assert state["consecutive_failures"] == 1
    assert "detail" in state and "at" in state


def test_repeated_retries_never_corrupt_or_leak_into_state_json(env,
                                                                monkeypatch):
    secret = "segredo-de-app-123"
    acct = _account(password=secret)

    def unreachable(_acct):
        raise OSError("timed out")

    monkeypatch.setattr(mail, "_imap_open", unreachable)
    for _ in range(4):
        mail._attempt(acct)

    state = json.loads(acct.state_file.read_text(encoding="utf-8"))
    assert state["consecutive_failures"] == 4
    assert state["error_message"] == "timed out"
    assert secret not in acct.state_file.read_text(encoding="utf-8")
    assert secret not in json.dumps(state, ensure_ascii=False)


def test_diagnostic_stage_names_the_real_step(env):
    """DNS, TCP, TLS e login são distinguíveis no diagnóstico."""
    import imaplib
    import socket
    import ssl
    acct = _account()
    started = time.monotonic()
    cases = [
        (socket.gaierror("name resolution failed"), "dns"),
        (OSError("connection refused"), "tcp"),
        (ssl.SSLError("bad certificate"), "tls"),
        (imaplib.IMAP4.error("[AUTHENTICATIONFAILED] no"), "login"),
    ]
    for exc, expected in cases:
        diag = mail._diag(acct, exc, "connect", started, 1)
        assert diag["stage"] == expected, (type(exc).__name__, diag)
        assert diag["error_type"] == type(exc).__name__
        assert diag["host"] == "imap.gmail.com"
        assert diag["port"] == 993
        assert diag["duration_ms"] >= 0


# ── aviso: segunda falha e recuperação, nada de spam ────────────────────────

def test_notice_only_on_second_consecutive_failure_then_recovery(env,
                                                                 monkeypatch):
    secret = "segredo-de-app-123"
    acct = _account(password=secret)
    calls: list[int] = []

    def flaky(_acct):
        calls.append(1)
        if len(calls) <= 3:
            raise OSError("network is unreachable")
        return FakeIMAP()

    monkeypatch.setattr(mail, "_imap_open", flaky)
    notices: list[dict] = []
    mail.set_retry_notice(notices.append)

    mail._attempt(acct)
    assert notices == [], "a primeira falha não fala com o dono"
    mail._attempt(acct)
    assert [n["kind"] for n in notices] == ["down"]
    mail._attempt(acct)
    assert [n["kind"] for n in notices] == ["down"], "sem spam por tentativa"
    mail._attempt(acct)
    assert [n["kind"] for n in notices] == ["down", "up"]
    assert "voltou a conectar" in notices[-1]["message"]

    # Aviso carrega nome/estado; nunca senha nem conteúdo de mensagem.
    blob = json.dumps(notices, ensure_ascii=False)
    assert secret not in blob
    assert "eu@gmail.com" not in blob


def test_retry_stops_when_owner_disables_account(env, monkeypatch):
    """Se o DONO desligar a conta, o worker acorda e sai sem tocar em nada."""
    acct = _account()

    def unreachable(_acct):
        raise OSError("network is unreachable")

    monkeypatch.setattr(mail, "_imap_open", unreachable)
    waits: list[float] = []
    monkeypatch.setattr(mail, "_retry_wait",
                        lambda seconds: waits.append(float(seconds)) or False)

    acct.auto_start = False
    before = len(waits)
    mail._retry_worker(acct, max_attempts=5)

    assert waits[before:] == [], "conta desligada pelo dono não é reconectada"
    assert acct.auto_start is False
