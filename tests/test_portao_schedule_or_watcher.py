"""O PORTÃO DE FLUXO ``require_schedule_or_watcher`` — item 1 do adendo aprovado.

Texto do dono (decisão 10 do plano
``PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento``, 09/11/2026,
fonte whatsapp, registrado em ``skills/fluxos/FLUXOS.md``):

    "UM PORTAO DECLARATIVO POR TIPO DE FRASE: ORDEM COM HORA OU COM ESTADO
     APONTADO NAO EXECUTA NA HORA — VIRA REGISTRO COM RECIBO."

Item 4 do mesmo adendo: antes de abrir execução, o cérebro vê se a frase tem
**horário fixo** (→ agendador, mecanismo A) ou **estado apontado** (→ ouvinte,
mecanismo B); só o que não tem nenhum dos dois executa na hora. Os dois
motores são separados — este portão não mistura os dois, ele exige que EXISTIA
um registro de um deles.

Um caso por teste, nenhum comportamento inventado:

* (c) recusa sem agendamento **e** sem escuta; aceita com qualquer um dos dois;
* frase que não é pedido de aviso continua livre (sem falso positivo);
* registro inválido não engana o portão (sem gatilho, já disparado, cancelado,
  evento fora do vocabulário do ouvinte);
* (d) paridade com o scheduler: a linha que ``core/scheduler.py`` aceita é a
  linha que o portão aceita, campo a campo.

Hermetismo: o cofre do agendador vive em ``tmp_path`` (fixture do conftest +
o ``cofre`` daqui); nenhum teste fala, envia ou escreve no caminho real.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import obsidian, scheduler
from core.flow_ops.rules import (EVENTOS_DO_OUVINTE, SCHEDULE_GATE,
                                 GateVerdict, pede_aviso_quando,
                                 require_schedule_or_watcher as gate,
                                 tem_estado_apontado, tem_horario_fixo)

AGENDAMENTO_BOM = {"id": "abc123", "titulo": "ligar para o eletricista",
                   "quando": "2026-11-11T14:30:00",
                   "gatilho": {"tipo": "funcao", "modulo": "core.scheduler",
                               "funcao": "agora", "destino": ["voz"]},
                   "disparou_em": "", "cancelado_em": ""}
ESCUTA_BOM = {"alvo": "job-77", "evento": "encerrar",
              "gatilho": {"tipo": "funcao", "modulo": "core.scheduler",
                          "funcao": "agora", "destino": ["voz"]}}


@pytest.fixture
def cofre(tmp_path, monkeypatch):
    root = tmp_path / "Jarvis"
    monkeypatch.setattr(obsidian, "VAULT", root)
    for nome, valor in (("_cache", {"assinatura": None, "linhas": {}}),
                        ("_scripts_cache", {"assinatura": None, "lista": []}),
                        ("_PORTAS", {}), ("_pendente_aviso", set()),
                        ("_pedidos_aprovacao", []), ("_ja_acordei", False)):
        monkeypatch.setattr(scheduler, nome, valor)
    return root


# ── classificação da frase (item 4 do adendo) ──────────────────────────────

def test_frases_do_dono_com_hora_fixa_sao_do_agendador():
    for frase in ("às 14:30 avisa o eletricista",
                  "amanhã me lembra de pagar a luz",
                  "me fala daqui 2 horas",
                  "me avisa em 20 minutos"):
        assert pede_aviso_quando(frase), frase
        assert tem_horario_fixo(frase), frase
        saida = gate(frase)
        assert saida.classe == "hora" and saida.rota == "agendador", saida


def test_frases_com_estado_apontado_sao_do_ouvinte():
    for frase in ("avisa quando o job terminar",
                  "me fala quando o download acabar",
                  "avisa quando mudar o status da tarefa",
                  "me lembra quando a porta abrir"):
        assert pede_aviso_quando(frase), frase
        assert tem_estado_apontado(frase), frase
        saida = gate(frase)
        assert saida.classe == "estado" and saida.rota == "ouvinte", saida


def test_pedido_de_aviso_sem_hora_nem_estado_executa_na_hora():
    saida = gate("avisa o porteiro que eu cheguei")
    assert saida.ok is True and saida.rota == "executar-agora", saida


def test_questao_com_palavra_hora_nao_e_pre_texto_para_travar():
    """Falso positivo seria a pane: 'que horas são?' tem hora e não é ordem."""
    for frase in ("que horas são?", "me diz a data de hoje",
                  "o que tem na agenda hoje?"):
        saida = gate(frase)
        assert saida.ok is True, (frase, saida.motivo)
        assert saida.rota == "executar-agora", frase


# ── (c) recusa sem agendamento nem escuta; aceita com qualquer um ──────────

def test_ordem_com_hora_sem_agendamento_e_recusada_nomeando_o_que_falta():
    saida = gate("às 14:30 me avisa de ligar para o eletricista")
    assert saida.ok is False
    assert saida.gate == SCHEDULE_GATE
    assert "agendamento" in saida.motivo and "gatilho" in saida.motivo
    assert saida.faltou, "a recusa tem de nomear o que faltou (lei C4)"
    assert bool(saida) is False
    assert saida.as_dict()["rota"] == "agendador"


def test_ordem_com_estado_sem_escuta_e_recusada_e_diz_o_motivo_real():
    saida = gate("me avisa quando o job do agente terminar")
    assert saida.ok is False
    assert "escuta" in saida.motivo
    assert "event_watcher" in saida.motivo, "tem de dizer que o B não existe"


def test_agendamento_valido_destrava_a_ordem_com_hora():
    saida = gate("às 14:30 me avisa de ligar para o eletricista",
                 agendamento=AGENDAMENTO_BOM)
    assert saida.ok is True and saida.rota == "agendador", saida


def test_escuta_valida_destrava_a_ordem_com_estado():
    saida = gate("avisa quando o job terminar", escuta=ESCUTA_BOM)
    assert saida.ok is True and saida.rota == "ouvinte", saida


def test_escuta_destrava_tambem_a_frase_com_hora_e_estado():
    saida = gate("quando o build terminar, me avisa às 14:30",
                 escuta=ESCUTA_BOM)
    assert saida.ok is True, saida


def test_lista_de_registros_vale_tanto_quanto_um_solo():
    assert gate("me avisa quando o job encerrar",
                escuta=[{"alvo": "", "evento": "encerrar"},
                        ESCUTA_BOM]).ok is True


# ── registro podre não engana o portão ─────────────────────────────────────

def test_agendamento_sem_gatilho_nao_destrava_nada():
    podre = dict(AGENDAMENTO_BOM, gatilho={})
    saida = gate("às 14:30 me avisa da consulta", agendamento=podre)
    assert saida.ok is False
    assert "gatilho" in saida.motivo, saida


def test_agendamento_ja_disparado_ou_cancelado_nao_vale():
    for estado in ({"disparou_em": "2026-11-10T09:00:00"},
                   {"cancelado_em": "2026-11-10T09:00:00"}):
        saida = gate("amanhã me lembra de pagar a luz",
                     agendamento=dict(AGENDAMENTO_BOM, **estado))
        assert saida.ok is False, (estado, saida)


def test_escuta_sem_evento_do_vocabulario_nao_vale():
    for escuta in ({"alvo": "job-77"},
                   {"alvo": "job-77", "evento": "quando-bem-entender"},
                   {"alvo": "", "evento": "iniciar"}):
        saida = gate("me avisa quando o processo iniciar", escuta=escuta)
        assert saida.ok is False, escuta
    assert EVENTOS_DO_OUVINTE == ("iniciar", "encerrar", "mudou status")


def test_escuta_cancelada_nao_vale():
    saida = gate("avisa quando o job terminar",
                 escuta=dict(ESCUTA_BOM, cancelado_em="2026-11-10T09:00:00"))
    assert saida.ok is False


def test_veredito_e_dado_legivel_para_o_recibo():
    saida = gate("às 14:30 me avisa de ligar para o eletricista")
    d = saida.as_dict()
    assert set(d) == {"ok", "gate", "rota", "classe", "motivo", "faltou",
                      "texto"}
    assert isinstance(saida, GateVerdict)


# ── (d) paridade com o scheduler e com a barreira da persona ───────────────

def test_linha_real_do_agendador_destrava_o_portao(cofre):
    """O que o portão aceita é exatamente o que o mecanismo A grava."""
    quando = (datetime.now() + timedelta(days=1)).replace(
        hour=14, minute=30, second=0, microsecond=0).isoformat()
    saida = scheduler.registrar(
        quando, "ligar para o eletricista",
        {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora",
         "destino": ["voz"]}, origem="fala_do_dono")
    assert saida["ok"] is True, saida
    linha = saida["linha"]
    assert gate("às 14:30 me avisa de ligar para o eletricista",
                agendamento=linha).ok is True
    # a mesma linha, depois de cancelada, deixa de destravar (e cancelar pede
    # confirmação: decisão 8 dele, nada é cancelado sem ela)
    sem_confirmacao = scheduler.cancelar(id=linha["id"],
                                         origem="fala_do_dono")
    assert sem_confirmacao["ok"] is False and sem_confirmacao.get("pergunta")
    cancelado = scheduler.cancelar(id=linha["id"], origem="fala_do_dono",
                                   confirmacao=True)
    assert cancelado["ok"] is True, cancelado
    assert gate("às 14:30 me avisa de ligar para o eletricista",
                agendamento=scheduler.linha_de(linha["id"])).ok is False


def test_pedido_de_persona_nao_vira_registro_e_o_portao_continua_cego(cofre):
    """Barreira da persona pelo caminho novo: nada gravado, portão recusa."""
    pedido = scheduler.registrar(
        (datetime.now() + timedelta(days=1)).isoformat(),
        "aviso da persona",
        {"tipo": "funcao", "modulo": "core.scheduler", "funcao": "agora",
         "destino": ["voz"]}, origem="automatica", persona="beatriz")
    assert pedido["ok"] is False and pedido.get("pedido"), pedido
    assert scheduler.listar() == [], "persona escreveu no cofre"
    saida = gate("amanhã me avisa do aviso da persona",
                 agendamento=scheduler.listar())
    assert saida.ok is False, "portão aceitou registro que não existe"
