"""UC-6: validacao da configuracao de filtros e do evento email_filter_decision.

O que o teste vigia, e por que:
  * (a) a config real (config/api_keys.json) tem o campo ``filters`` preenchido
    na conta principal, com regras de remetente/assunto/acao;
  * (b) o hook ``set_filter_decision_hook`` e chamado por ``_filter_trace``
    para cada decisao de filtro (avisa/ignora/classificar/fora-do-filtro);
  * (c) o handler do main.py grava a linha ``email_filter_decision`` no
    activity.log com conta, uid, acao e motivo — sem dados pessoais.

Nada aqui fala com a rede: IMAP e fake, cofre em memoria.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from core import email as mail
from tests.test_email import FakeIMAP, FakeUI, _header  # noqa: F401


# ── (a) config real tem campo filters ────────────────────────────────────────

def test_config_has_filters_field():
    """A config real deve ter ``filters`` na conta principal."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = data.get("plugin_config", {}).get("mail", {}).get("accounts", [])
    assert accounts, "deve haver pelo menos uma conta configurada"
    principal = accounts[0]
    assert "filters" in principal, "conta principal deve ter campo filters"
    filters = principal["filters"]
    assert isinstance(filters, list), "filters deve ser uma lista"
    assert len(filters) >= 1, "deve haver pelo menos uma regra de filtro"
    # Cada regra tem os campos obrigatorios
    for rule in filters:
        assert "field" in rule, "regra deve ter campo field"
        assert "pattern" in rule, "regra deve ter campo pattern"
        assert "action" in rule, "regra deve ter campo action"
        assert rule["action"] in ("avisa", "ignora", "classificar"), \
            f"acao invalida: {rule['action']}"


def test_config_filters_have_real_rules():
    """As regras devem cobrir remetente, assunto ou palavras-chave."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    accounts = data.get("plugin_config", {}).get("mail", {}).get("accounts", [])
    filters = accounts[0].get("filters", [])
    # Pelo menos uma regra de remetente (from) e uma de assunto (subject)
    fields = {r.get("field") for r in filters}
    assert "from" in fields or "subject" in fields, \
        "deve haver regra por remetente ou assunto"


# ── (b) hook de decisao de filtro ────────────────────────────────────────────

def test_filter_decision_hook_is_called(vault, monkeypatch):
    """O hook registrado e chamado para cada decisao de filtro."""
    mail.add_account("gmail", "eu@gmail.com", notify=True, filters=[
        {"from": "banco", "action": "avisa"},
        {"subject": "FLASH SALE", "action": "ignora"},
    ])
    fake = FakeIMAP({
        b"1": _header("Extrato mensal", sender="Banco <b@banco.example.com>",
                      message_id="<f1>"),
        b"2": _header("FLASH SALE imperdivel", sender="promo@x.com",
                      message_id="<f2>"),
        b"3": _header("Assunto qualquer", sender="ninguem@x.com",
                      message_id="<f3>"),
    })
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)
    decisions: list[tuple] = []
    mail.set_filter_decision_hook(
        lambda acct, uid, action, motivo: decisions.append(
            (acct, uid, action, motivo)))
    try:
        mail.poll_once(hook=lambda item: None)
    finally:
        mail.set_filter_decision_hook(None)
    # 3 emails: 1 avisa (regra1-from), 1 ignora (regra2-subject),
    # 1 fora-do-filtro (ignora)
    assert len(decisions) == 3, f"uma decisao por email: {decisions}"
    assert decisions[0][2] == "avisa" and "regra1" in decisions[0][3]
    assert decisions[1][2] == "ignora" and "regra2" in decisions[1][3]
    assert decisions[2][2] == "ignora" and "fora-do-filtro" in decisions[2][3]


def test_filter_decision_hook_not_called_without_filters(vault, monkeypatch):
    """Sem filtros, nenhuma decisao e registrada (silencio total)."""
    mail.add_account("gmail", "eu@gmail.com", notify=True)
    fake = FakeIMAP({
        b"1": _header("Qualquer assunto", sender="alguem@x.com",
                      message_id="<f1>"),
    })
    monkeypatch.setattr(mail, "_imap_open", lambda _a: fake)
    decisions: list[tuple] = []
    mail.set_filter_decision_hook(
        lambda acct, uid, action, motivo: decisions.append(
            (acct, uid, action, motivo)))
    try:
        mail.poll_once(hook=lambda item: None)
    finally:
        mail.set_filter_decision_hook(None)
    assert len(decisions) == 0, "sem filtros, sem decisao"


# ── (c) handler do main.py grava no activity.log ─────────────────────────────

def test_mail_filter_decision_writes_to_activity_log():
    """O handler do main.py grava linha email_filter_decision no log."""
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    main.JarvisLive._mail_filter_decision(
        obj, "gmail", "12345", "avisa", "regra1-from")
    assert len(obj.ui.lines) == 1
    line = obj.ui.lines[0]
    assert "email_filter_decision" in line
    assert "conta=gmail" in line
    assert "uid=12345" in line
    assert "acao=avisa" in line
    assert "motivo=regra1-from" in line
    # Sem dados pessoais na linha
    assert "banco" not in line
    assert "example.com" not in line


def test_mail_filter_decision_handles_empty_values():
    """Valores vazios viram '?' — nunca quebra o log."""
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    main.JarvisLive._mail_filter_decision(obj, "", "", "", "")
    assert len(obj.ui.lines) == 1
    line = obj.ui.lines[0]
    assert "conta=?" in line
    assert "uid=?" in line
    assert "acao=?" in line
    assert "motivo=?" in line


# ── cofre falso (emprestado do test_email.py) ────────────────────────────────

@pytest.fixture()
def vault(monkeypatch, tmp_path):
    """plugin_config em memoria; nenhum arquivo real e lido ou escrito."""
    data: dict[str, dict] = {}

    def get_cfg(namespace):
        return dict(data.get(namespace, {}))

    def save_cfg(namespace, values):
        cur = data.setdefault(namespace, {})
        cur.update(values)

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
    yield data
    mail.reset()
