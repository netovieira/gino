"""LinkedIn como REGRA do fluxo de corpo de e-mail (decisao do dono, 07/11/2026).

O dono decidiu que LinkedIn NAO tem plugin, rota propria nem OAuth: e so mais
uma regra dentro do fluxo de leitura de e-mails (UP-6, core/email_flows.py).
O que este teste vigia, e por que:

  * e-mail do LinkedIn (dominio linkedin.com) -> fluxo ``linkedin_notification``
    -> SOMENTE um aviso ao dono: nenhum executor de acao e chamado;
  * e-mail normal (banco, sem codigo) -> NAO vira aviso do LinkedIn;
  * falso positivo obvio: a palavra "linkedin" no ASSUNTO vindo de outro
    remetente nao casa com a regra, que e ancorada no dominio do remetente;
  * a rota antiga esta desligada: ``plugins_enabled.linkedin_control`` e False
    (o modulo continua no repo por historico — core/linkedin.py nao e chamado).

Nada aqui fala com a rede, com o vault do dono ou com o config real de
segredos: historico vai para tmp_path, aviso e executor sao coletores em
memoria, e o unico valor lido do cofre e um booleano de ligar/desligar.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import email as mail
from core import email_flows as flows

# A regra escolhida: dominio do REMETENTE (nunca o assunto), ainda que
# mascarado por subdominio (messages-noreply@, news-noreply@, emissions@).
LI_RULE = {
    "field": "from",
    "pattern": r"@([\w.-]+\.)?linkedin\.com$",
    "match": "regex",
    "action": "avisa",
    "dispatch": True,
    "tipo": "linkedin_notification",
    "label": "linkedin",
}
ACCT = {"name": "principal", "address": "eu@gmail.com"}


@pytest.fixture()
def collectors(monkeypatch, tmp_path):
    """Recibo em disco temporario + ganchos coletores (nada de fala real)."""
    monkeypatch.setattr(flows, "_seen_path", tmp_path / "email_flow_seen.json")
    notices: list[dict] = []
    events: list[dict] = []
    execed: list[tuple] = []
    flows.reset()
    flows.set_notify_hook(lambda p: (notices.append(p), True)[1])
    flows.set_event_hook(events.append)
    flows.set_executor(lambda a, p, i, c: (execed.append((a, p)), True)[1])
    # O texto da mensagem privada nunca cai no vault do dono em teste.
    monkeypatch.setattr(flows, "_write_note",
                        lambda *a, **k: notices.append({"note": a}))
    yield {"notices": notices, "events": events, "execed": execed}
    flows.reset()


def _item(subject: str, sender: str, from_: str) -> dict:
    return {"uid": f"u{abs(hash((subject, from_))) % 9999}", "subject": subject,
            "sender": sender, "from": from_}


# ── 1. bate: notificacao do LinkedIn vira SO aviso ───────────────────────────

def test_linkedin_invitation_is_a_simple_notice(collectors):
    item = _item("Maria Souza quer conectar-se com você",
                 "LinkedIn", "messages-noreply@linkedin.com")
    ev = flows.dispatch_flow(ACCT, item, LI_RULE,
                             "Maria Souza quer conectar-se com você. "
                             "Aceitar o convite: https://www.linkedin.com/feed")
    assert ev is not None
    assert ev["type"] == "linkedin_notification"
    assert ev["action"] == "aviso"
    assert ev["ok"] is True
    # so um aviso, nunca despacho de acao automatica
    assert collectors["execed"] == [], "LinkedIn nao pode executar acao"
    assert len(collectors["notices"]) == 1
    text = str(collectors["notices"][0].get("text") or "")
    assert text.startswith("LinkedIn:")
    # privacidade: corpo/link nao vao para o evento
    assert "linkedin.com/feed" not in str(ev)


def test_linkedin_message_is_noticed_once_per_receipt(collectors):
    item = _item("João Lima enviou uma nova mensagem", "LinkedIn",
                 "messages-noreply@linkedin.com")
    body = ("João Lima enviou uma nova mensagem. "
            "Veja: https://www.linkedin.com/messaging")
    ev = flows.dispatch_flow(ACCT, item, LI_RULE, body)
    assert ev is not None and ev["action"] == "aviso"
    assert collectors["execed"] == []
    # um recibo, uma execucao
    assert flows.dispatch_flow(ACCT, item, LI_RULE, body) is None


# ── 2. nao bate: e-mail comum nao vira aviso do LinkedIn ─────────────────────

def test_plain_bank_email_is_not_a_linkedin_notice(collectors):
    item = _item("Extrato de outubro", "Banco Bom", "no-reply@bancobom.com")
    ev = flows.dispatch_flow(ACCT, item, {"action": "avisa",
                                         "dispatch": True},
                             "Olá, seu extrato de outubro está disponível.")
    assert ev is not None
    assert ev["type"] != "linkedin_notification"
    assert collectors["notices"] == []


def test_filter_rule_never_matches_other_senders():
    assert mail.match_filter(_item("Boas-vindas", "Amigo",
                                   "amigo@exemplo.com"),
                             [LI_RULE]) is None
    assert mail.match_filter(_item("Extrato", "Banco", "no-reply@banco.com"),
                             [LI_RULE]) is None
    hit = mail.match_filter(_item("Convite", "LinkedIn",
                                  "news-noreply@linkedin.com"), [LI_RULE])
    assert hit is not None and hit.get("dispatch") is True


# ── 3. falso positivo obvio: "linkedin" no assunto, remetente outro ───────────

def test_linkedin_word_in_subject_from_stranger_is_not_notified(collectors):
    item = _item("Como aparecer no linkedin: 10 dicas", "Curso de Marketing",
                 "news@cursomarketing.com.br")
    assert mail.match_filter(item, [LI_RULE]) is None
    ev = flows.dispatch_flow(ACCT, item, {"action": "avisa",
                                         "dispatch": True},
                             "10 dicas para seu perfil no linkedin crescer.")
    assert ev is None or ev["type"] != "linkedin_notification"
    assert collectors["notices"] == []


# ── 4. DSL nao inventa tipo, e a rota antiga esta desligada ──────────────────

def test_unknown_tipo_forces_nothing():
    assert flows.flow_from_rule({"tipo": "linkedin"}) is None
    assert flows.flow_from_rule({}) is None
    got = flows.flow_from_rule(dict(LI_RULE))
    assert got is not None and got["type"] == "linkedin_notification"


def test_old_linkedin_route_is_disabled():
    """LinkedIn deixou de ser caminho proprio (plugin inerte)."""
    from memory.config_manager import get_plugin_enabled
    assert get_plugin_enabled("linkedin_control") is False
