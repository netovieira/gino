"""Conta de envio: default é a do dono; conta errada é erro; sem reenvio.

Regra do dono (07/11/2026, depois do áudio que saiu pelo número errado):
"envio por WhatsApp sai SEMPRE na conta que o dono usa para falar comigo,
salvo pedido explícito em contrário". O lado perigoso disso é o reenvio
automático: se uma conta falha, "tentar na outra" é mandar assunto pessoal
para o trabalho. Nada aqui tenta duas vezes.

A camada de envio é falsa (``FakeWA``): nenhum destes testes alcança o
WhatsApp real, e nenhum áudio de teste toca em conta pareada.
"""
from __future__ import annotations

import pytest

from plugins import whatsapp_control as wa

from tests.test_whatsapp_parity_dispatch import OWNER_NUMBER, FakeWA

PRINCIPAL = {"name": "principal", "uuid": "u1", "default": True,
             "number": OWNER_NUMBER, "paired": True}
TRABALHO = {"name": "trabalho", "uuid": "u2", "default": False,
            "number": "5511999997777", "paired": True}


@pytest.fixture
def two(monkeypatch):
    """Duas contas pareadas (o caso em que escolher sozinho é o defeito)."""
    w = FakeWA(accounts=[dict(PRINCIPAL), dict(TRABALHO)])
    monkeypatch.setattr(wa, "whatsapp", w)
    monkeypatch.setattr(wa, "get_plugin_setting",
                        lambda ns, key, default=None: (
                            OWNER_NUMBER if key == "owner_chat" else default))
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda text, **kw: ("/tmp/nota.ogg", text))
    return w


@pytest.fixture
def one(monkeypatch):
    """Uma conta só — o caso do dono hoje (principal)."""
    w = FakeWA(accounts=[dict(PRINCIPAL)])
    monkeypatch.setattr(wa, "whatsapp", w)
    monkeypatch.setattr(wa, "get_plugin_setting",
                        lambda ns, key, default=None: (
                            OWNER_NUMBER if key == "owner_chat" else default))
    monkeypatch.setattr(wa.voice_note, "synthesize",
                        lambda text, **kw: ("/tmp/nota.ogg", text))
    return w


# ── default ─────────────────────────────────────────────────────────────────

def test_sem_conta_usa_a_conta_de_saida_do_dono(one):
    """Uma conta pareada, nenhum `account`: ela, com `account=None` no core."""
    wa.run({"action": "send_voice", "to": "Joao", "text": "oi"})
    assert one.medias == [("Joao", "/tmp/nota.ogg", "", None)]
    assert one.default_account_name() == "principal"


def test_com_duas_contas_e_padrao_definido_age_no_padrao(two):
    """>  default marcado = não perguntar: a de saída é a do dono."""
    wa.run({"action": "send_voice", "to": "Joao", "text": "oi"})
    assert two.medias and two.medias[0][3] is None


def test_com_duas_contas_e_sem_padrao_pergunta_em_vez_de_escolher(monkeypatch, two):
    for a in two.accounts:
        a["default"] = False

    def _no_default():
        return ""
    monkeypatch.setattr(two, "default_account_name", _no_default)
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi"})
    assert "Qual eu uso?" in out
    assert two.medias == [], "perguntar é o certo; escolher sozinho e o defeito"


def test_conta_nomeada_existe_e_usada(two):
    wa.run({"action": "send_voice", "to": "Joao", "text": "oi",
            "account": "trabalho"})
    assert [m[3] for m in two.medias] == ["trabalho"]


# ── conta inexistente ───────────────────────────────────────────────────────

@pytest.mark.parametrize("errada", ["trbalho", "pessoal", "55 11 0000",
                                    "conta trabalho 3"])
def test_conta_que_nao_existe_erro_claro_e_nenhum_envio(two, errada):
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi",
                  "account": errada})
    assert "nada foi enviado" in out
    assert "principal" in out and "trabalho" in out, "lista as contas pareadas"
    assert two.medias == [] and two.sent == []


def test_rota_de_texto_tambem_recusa_conta_inexistente(two):
    out = wa.run({"action": "send", "to": "Joao", "text": "oi",
                  "account": "trbalho"})
    assert "nada foi enviado" in out
    assert two.sent == []


def test_conta_de_leitura_inexistente_tambem_erro(two):
    """`recent` não é exceção: nome errado de conta também é erro, não inbox."""
    out = wa.run({"action": "recent", "account": "trbalho"})
    assert "nada foi enviado" in out or "nao conheço" in out or "conta" in out
    assert two.medias == [] and two.sent == []


# ── nenhum reenvio para outra conta ─────────────────────────────────────────

def test_falha_de_envio_nao_tenta_a_outra_conta(monkeypatch, two):
    monkeypatch.setattr(two, "send_media",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sem sessão")))
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi",
                  "account": "trabalho"})
    assert "falhou" in out or "sem sessão" in out
    # O ponto: exatamente UMA tentativa, na conta pedida. Reenviar na outra é
    # o que mandaria assunto pessoal para o trabalho (ou o contrário).
    assert len(two.medias) == 0 and len(two.sent) == 0


def test_um_envio_por_pedido_nunca_dois(two):
    out = wa.run({"action": "send_voice", "to": "Joao", "text": "oi"})
    assert "midia enviada" in out
    assert len(two.medias) == 1, "nota de voz não se manda duas vezes"
    assert two.sent == [], "nota de voz não se duplica em texto"


# ── destino "mim" é o número do dono, lido do config ────────────────────────

def test_para_mim_usa_owner_chat(one, monkeypatch):
    wa.run({"action": "send_voice", "to": "pra mim", "text": "lembrete"})
    assert one.medias[0][0] == OWNER_NUMBER


def test_sem_owner_chat_pedir_registro_nao_adivinhar(one, monkeypatch):
    monkeypatch.setattr(wa, "get_plugin_setting",
                        lambda ns, key, default=None: default)
    out = wa.run({"action": "send_voice", "to": "mim", "text": "oi"})
    assert "owner_chat" in out
    assert one.medias == []
