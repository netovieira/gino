"""Ferramenta AGENDADOR — o porto único (voz e texto) do mecanismo A.

O dono pediu (áudio das 19:00 de 09/11/2026): "você vai criar um agendamento e
definir o gatilho que esse agendamento vai executar". Esta é a boca do
``core/scheduler``: **uma** action, chamada do turno de voz ou do texto
digitado/WhatsApp com o mesmo schema e o mesmo veredito (lei C1/C7). A trava
é o **enum**, não a descrição: ``acao``, ``gatilho.tipo`` e
``gatilho.destino`` são vocabulários fechados, do mesmo jeito que
``actions/home_control.py`` travou as ações de casa.

Nada aqui decide política: validação de hora, de gatilho, de origem, de
cancelamento e de vencido moram em :mod:`core.scheduler`. Esta camada só
traduz parâmetros e devolve JSON.
"""
from __future__ import annotations

import json

from core import scheduler


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(acao: str, **extra) -> str:
    payload = {"status": "ok", "acao": acao}
    payload.update(extra)
    return _json(payload)


def _recusado(acao: str, mensagem: str, **extra) -> str:
    payload = {"status": "recusado", "acao": acao, "mensagem": mensagem}
    payload.update(extra)
    return _json(payload)


def _origem(p: dict) -> tuple[str, str]:
    """(origem, persona). Turno do dono = fala do dono, por construction.

    O portão de origem vive em ``core/scheduler``; aqui só se declara quem
    pediu. Chamadas automáticas (eu, personas, fluxo de e-mail) precisam dizer
    ``origem`` explicitamente — sem origem o scheduler recusa, e a recusa
    nomeia o que faltou (lei C4).
    """
    return (str(p.get("origem") or "fala_do_dono"),
            str(p.get("persona") or "").strip())


def _gatilho(p: dict) -> dict:
    bruto = p.get("gatilho")
    if not isinstance(bruto, dict):
        bruto = {}
    argumentos = p.get("argumentos")
    if isinstance(argumentos, (list, dict)) and "argumentos" not in bruto:
        bruto = {**bruto, "argumentos": argumentos}
    for chave in ("tipo", "modulo", "funcao", "script", "destino",
                  "argumentos"):
        if chave not in bruto and chave in p:
            bruto[chave] = p[chave]
    return bruto


def _add(p: dict) -> str:
    origem, persona = _origem(p)
    resultado = scheduler.registrar(
        p.get("quando"), p.get("titulo") or p.get("texto"), _gatilho(p),
        origem=origem, persona=persona, texto=p.get("texto") or "",)
    if not resultado.get("ok"):
        return _recusado("add", resultado.get("recusa", "não agendei"),
                         pedido=resultado.get("pedido") or {})
    linha = resultado["linha"]
    return _ok("add", id=linha["id"], quando=linha["quando"],
               titulo=linha["titulo"], gatilho=linha["gatilho"],
               nota=resultado.get("nota", ""), recibo=resultado.get("recibo"),
               linha=scheduler.linha_texto(linha))


def _list(p: dict) -> str:
    abertos = str(p.get("status") or "").strip().lower() not in ("todas",
                                                                "all", "*")
    linhas = scheduler.listar(abertos_only=abertos)
    erros = scheduler.erro_de_leitura()
    return _ok("list", quantidade=len(linhas),
               agenda=[scheduler.linha_texto(l) for l in linhas],
               linhas=linhas, notas_em_erro=erros,
               fonte=f"{scheduler._pasta_notas()} (nota do cofre, cache em memoria)")


def _cancel(p: dict) -> str:
    origem, persona = _origem(p)
    resultado = scheduler.cancelar(
        origem=origem, persona=persona,
        id=str(p.get("id") or "").strip(),
        texto=str(p.get("titulo") or p.get("texto") or "").strip(),
        confirmacao=bool(p.get("confirmacao")))
    if resultado.get("pergunta"):
        return _json({"status": "pergunta", "acao": "cancel",
                      "mensagem": resultado["pergunta"],
                      "candidatos": resultado.get("candidatos")
                      or [resultado.get("candidato")]})
    if not resultado.get("ok"):
        return _recusado("cancel", resultado.get("recusa", "não cancelei"))
    return _ok("cancel", linha=resultado["linha"],
               cancelado=resultado["cancelado"]["id"],
               recibo=resultado.get("recibo"))


def _snooze(p: dict) -> str:
    origem, persona = _origem(p)
    resultado = scheduler.pospor(p.get("id") or p.get("titulo"),
                                 p.get("quando"), origem=origem,
                                 persona=persona)
    if resultado.get("pergunta"):
        return _json({"status": "pergunta", "acao": "snooze",
                      "mensagem": resultado["pergunta"]})
    if not resultado.get("ok"):
        return _recusado("snooze", resultado.get("recusa", "não pospus"))
    return _ok("snooze", quando=resultado["linha"]["quando"],
               id=resultado["linha"]["id"], recibo=resultado.get("recibo"))


def _resumo(p: dict, speak=None) -> str:
    """A regra ditada dele (decisão 6): contagem + resumo + a pergunta."""
    if str(p.get("um_a_um") or "").strip().lower() in ("sim", "true", "1",
                                                       "quero", "ouvir"):
        feito = scheduler.um_a_um(boca=speak)
        return _ok("um_a_um", falados=feito["falados"])
    atrasados = scheduler.vencidos()
    hoje = scheduler.agenda_de_hoje()
    return _ok("resumo", vencidos=len(atrasados),
               resumo=scheduler.resumo_dos_vencidos(atrasados) if atrasados
               else "nada vencido",
               pergunta=("Quer ouvir um a um?" if atrasados else ""),
               hoje=[scheduler.linha_texto(l) for l in hoje],
               pedidos_aprovacao=scheduler.pedidos_aprovacao())


def _gatilhos(p: dict) -> str:
    """O que existe para ser agendado: enums e scripts cadastrados (C4)."""
    return _ok("gatilhos", tipos=list(scheduler.GATILHO_TIPOS),
               destinos=list(scheduler.GATILHO_DESTINOS),
               destino_padrao=list(scheduler.DESTINO_PADRAO),
               tipos_de_argumento=list(scheduler.ARGUMENTO_TIPOS),
               modulos_permitidos=list(scheduler.MODULOS_PERMITIDOS),
               scripts=scheduler.scripts_cadastrados())


def _executar(p: dict, speak=None) -> str:
    """Roda o gatilho AGORA, pela mesma função única do disparo marcado.

    Existe para ele poder conferir ("roda esse gatilho aí") e para o turno
    poder exercitar o mesmo caminho — não é um segundo disparo: é
    :func:`core.scheduler.executar_gatilho`, a mesma função que o laço chama.
    """
    linha = scheduler.linha_de(p.get("id") or p.get("titulo"))
    gatilho = _gatilho(p) or dict((linha or {}).get("gatilho") or {})
    resultado = scheduler.executar_gatilho(gatilho, linha=linha, boca=speak)
    if resultado.get("recusa"):
        return _recusado("executar", resultado["recusa"])
    if resultado.get("pendente") and not resultado.get("feito"):
        return _json({"status": "pendente", "acao": "executar",
                      "mensagem": resultado["pendente"]})
    return _ok("executar", feito=resultado["feito"],
               destinos=resultado["destinos"], valor=resultado["valor"])


def _cadastrar(p: dict) -> str:
    origem, persona = _origem(p)
    resultado = scheduler.cadastrar_script(p.get("caminho"),
                                           p.get("descricao") or "",
                                           origem=origem, persona=persona)
    if not resultado.get("ok"):
        return _recusado("cadastrar", resultado["recusa"])
    return _ok("cadastrar", **resultado)


def schedule(parameters: dict, speak=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    acao = str(p.get("acao") or "list").strip().lower()
    try:
        if acao in ("add", "agendar", "criar", "registrar"):
            return _add(p)
        if acao in ("list", "listar", "ver"):
            return _list(p)
        if acao in ("cancel", "cancelar", "esquecer", "esquece"):
            return _cancel(p)
        if acao in ("snooze", "pospor", "adiar"):
            return _snooze(p)
        if acao in ("resumo", "vencidos", "acordar"):
            return _resumo(p, speak=speak)
        if acao in ("gatilhos", "opcoes"):
            return _gatilhos(p)
        if acao in ("executar", "rodar", "testar"):
            return _executar(p, speak=speak)
        if acao in ("cadastrar", "cadastro"):
            return _cadastrar(p)
    except Exception as exc:  # noqa: BLE001 - o turno nunca cai por aqui
        return _recusado(acao, f"não consegui completar: {exc}")
    return _recusado(acao,
                     f"ação {acao!r} fora da lista add/list/cancel/snooze/"
                     "resumo/gatilhos/executar/cadastrar")


TOOL = {
    "name": "schedule",
    "description": (
        "Agenda uma coisa com HORARIO FIXO para rodar sozinha na hora marcada. "
        "Todo agendamento carrega um gatilho: uma funcao Python da casa "
        "(modulo + funcao) ou um script cadastrado, com argumentos tipados e "
        "um destino (voz, whatsapp, upload). Use para 'as 7 manda WhatsApp', "
        "'as 4 avisa na voz', 'amanha 14:30 roda tal script'. Nao use para "
        "'quando aquilo terminar' — isso e o ouvinte de execucao, outro "
        "mecanismo. Sem gatilho resolvivel, sem hora entendivel, nada e "
        "agendado e a recusa diz o que faltou."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "acao": {
                "type": "STRING",
                "description": "O que fazer.",
                "enum": ["add", "list", "cancel", "snooze", "resumo",
                         "gatilhos", "executar", "cadastrar"],
            },
            "quando": {
                "type": "STRING",
                "description": ("Instante fixo, como falado: 'hoje 14:30', "
                                "'amanha as 7 da manha', 'daqui a duas horas', "
                                "ou ISO '2026-11-12 08:00'."),
            },
            "titulo": {
                "type": "STRING",
                "description": "Nome curto do agendamento (o que ele ouve).",
            },
            "texto": {
                "type": "STRING",
                "description": ("Frase a ser dita no disparo; vazio = o "
                                "titulo (ou o que a funcao devolver)."),
            },
            "id": {
                "type": "STRING",
                "description": "id do agendamento (cancelar/snooze com id).",
            },
            "confirmacao": {
                "type": "BOOLEAN",
                "description": ("True somente depois que ele confirmou. Nada "
                                "e cancelado sem confirmacao (decisao 8 dele)."),
            },
            "gatilho": {
                "type": "OBJECT",
                "description": ("O que roda na hora marcada. Obrigatorio: sem "
                                "gatilho o registro e recusado."),
                "properties": {
                    "tipo": {"type": "STRING",
                             "enum": list(scheduler.GATILHO_TIPOS)},
                    "modulo": {"type": "STRING",
                               "description": "funcao: modulo real (core.*, "
                                              "actions.*, plugins.*, memory.*)"},
                    "funcao": {"type": "STRING",
                               "description": "funcao: nome do simbolo"},
                    "script": {"type": "STRING",
                               "description": "script: caminho absoluto ja "
                                              "cadastrado"},
                    "argumentos": {
                        "type": "ARRAY",
                        "description": ("Lista tipada, validada na criacao."),
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "nome": {"type": "STRING"},
                                "tipo": {"type": "STRING",
                                         "enum": list(
                                             scheduler.ARGUMENTO_TIPOS)},
                                "valor": {"type": "STRING"},
                            },
                        },
                    },
                    "destino": {
                        "type": "ARRAY",
                        "description": ("Para onde vai o disparo. Vazio = os "
                                        "dois (voz + whatsapp), decisao 7."),
                        "items": {"type": "STRING",
                                  "enum": list(scheduler.GATILHO_DESTINOS)},
                    },
                },
            },
            "origem": {
                "type": "STRING",
                "description": ("Quem pediu. O turno do dono e fala_do_dono; "
                                "chamada automatica precisa declarar."),
                "enum": ["fala_do_dono", "automatica", "fala_visitante",
                         "filtro_email"],
            },
            "persona": {
                "type": "STRING",
                "description": ("Nome da persona quando ela pede. Sem "
                                "autorizacao dele, vira pedido de aprovacao e "
                                "nada e gravado."),
            },
            "status": {
                "type": "STRING",
                "description": "list: 'abertos' (default) ou 'todas'.",
            },
            "um_a_um": {
                "type": "STRING",
                "description": ("resumo: 'sim' fala cada vencido um a um "
                                "(a resposta dele a pergunta da decisao 6)."),
            },
            "caminho": {
                "type": "STRING",
                "description": "cadastrar: caminho absoluto do script.",
            },
            "descricao": {
                "type": "STRING",
                "description": "cadastrar: para que serve o script.",
            },
        },
        "required": ["acao"],
    },
    "handler": schedule,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
