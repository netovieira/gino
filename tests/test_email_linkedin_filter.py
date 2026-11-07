"""A REGRA do LinkedIn na camada de filtro (decisao do dono, 07/11/2026).

O dono decidiu que LinkedIn NAO tem plugin, rota propria nem OAuth: e so mais
uma regra dentro do fluxo de leitura de e-mails (UP-6, core/email_flows.py).
Este arquivo cuida do que o FILTRO enxerga: casar remetente, nao casar estranho
e decidir QUEM avisa. O conteudo do aviso (frase rica, piso de chegada, nota no
cofre, recibo) esta em test_email_flow_linkedin.py, e a proibicao da dupla frase
em test_email_linkedin_aviso_unico.py. Este recorte existe porque duas copias do
mesmo comportamento — a regra de filtro de um lado, o dispatcher do outro —
acabaram afirmando comportamentos contraditorios.

O que aqui se prova, e por que:
  * a regra ancora no DOMINIO do remetente (nunca no assunto) e aceita
    subdominio (messages-noreply@, news-noreply@, emissions@);
  * falso positivo obvio: a palavra "linkedin" no assunto vindo de outro
    remetente nao casa com a regra;
  * DELEGCANDO O AVISO: regra com ``dispatch`` + ``tipo`` de fluxo que avisa por
    si (NOTICE_TYPES) faz o gancho de chegada do poll_once ficar calado. A
    decisao e da REGRA, nunca do resultado do despacho — e o que impede a
    segunda frase do mesmo e-mail;
  * a rota antiga esta desligada: plugins_enabled.linkedin_control e False
    (o modulo continua no repo por historico — core/linkedin.py nao e chamado).

Nada aqui fala com a rede, com o cofre do dono ou com o config real de
segredos: o unico valor lido do cofre e um booleano de ligar/desligar.
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


@pytest.fixture(autouse=True)
def _ganchos_zerados():
    """Nenhum gancho global deste arquivo vaza para o teste vizinho."""
    flows.reset()
    yield
    flows.reset()


def _item(subject: str, sender: str, from_: str) -> dict:
    return {"uid": f"u{abs(hash((subject, from_))) % 9999}", "subject": subject,
            "sender": sender, "from": from_}


# ── 1. a regra casa o dominio, e so o dominio ────────────────────────────────

def test_a_regra_casa_linkedin_em_subdominio_e_recusa_o_resto():
    """A regra do LinkedIn ancora no remetente: subdominio vale, estranho nao."""
    hit = mail.match_filter(_item("Convite", "LinkedIn",
                                  "news-noreply@linkedin.com"), [LI_RULE])
    assert hit is not None and hit.get("dispatch") is True
    assert mail.match_filter(_item("Boas-vindas", "Amigo",
                                   "amigo@exemplo.com"), [LI_RULE]) is None
    assert mail.match_filter(_item("Extrato", "Banco", "no-reply@banco.com"),
                             [LI_RULE]) is None


def test_linkedin_no_assunto_de_desconhecido_nao_casa_na_regra():
    """"linkedin" escrito no assunto por quem nao e o LinkedIn nao vira nada."""
    item = _item("Como aparecer no linkedin: 10 dicas", "Curso de Marketing",
                 "news@cursomarketing.com.br")
    assert mail.match_filter(item, [LI_RULE]) is None


# ── 2. quem avisa: a delegacao decide no filtro, uma frase por e-mail ────────

def test_regra_com_tipo_que_avisa_por_si_delega_o_aviso_ao_fluxo():
    """O unico ponto que decide quem fala: a REGRA, nunca o resultado do despacho.

    Se a decisao dependesse do que ``dispatch_flow`` devolve, um recibo ja gasto
    (que devolve None) faria o filtro falar de novo — e o mesmo e-mail teria
    duas frases em duas varreduras.
    """
    assert mail._notice_is_delegated(LI_RULE) is True
    # Sem ``tipo``, ou com tipo que NAO produz aviso proprio, o filtro continua
    # avisando a chegada (e o fluxo roda depois, sem falar de aviso).
    assert mail._notice_is_delegated({"action": "avisa",
                                      "dispatch": True}) is False
    assert mail._notice_is_delegated({"action": "avisa", "dispatch": True,
                                      "tipo": "leitura_codigo"}) is False
    # ``tipo`` sem ``dispatch`` nao e despacho nenhum: quem avisa e o filtro.
    assert mail._notice_is_delegated({"action": "avisa",
                                      "tipo": "linkedin_notification"}) is False
    assert mail._notice_is_delegated(None) is False
    assert mail._notice_is_delegated({}) is False


def test_o_tipo_que_avisa_por_si_vem_da_lista_fechada():
    """Quem possui aviso proprio e lista fechada do modulo de fluxos — nada de
    adivinhar pelo nome do tipo ou pelo remetente."""
    assert flows.type_owns_notice("linkedin_notification") is True
    assert flows.type_owns_notice("LEITURA_CODIGO") is False
    assert flows.type_owns_notice("") is False
    assert flows.type_owns_notice(None) is False


# ── 3. a rota antiga esta desligada ─────────────────────────────────────────

def test_old_linkedin_route_is_disabled():
    """LinkedIn deixou de ser caminho proprio (plugin inerte)."""
    from memory.config_manager import get_plugin_enabled
    assert get_plugin_enabled("linkedin_control") is False
