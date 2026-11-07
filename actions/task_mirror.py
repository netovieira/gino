"""Abre o espelho visível de uma execução pesada (terminal ou nota no Obsidian).

O dono diz "me mostra trabalhando": esta ação escolhe a tarefa da fila que está
rodando (ou o espelho agregado das execuções em segundo plano), cria o log se
precisar e abre o ptyxis com ``tail -F`` nele. É a versão explícita do que já
acontece sozinho quando um subagente começa; serve para reabrir a janela e para
apontar uma tarefa específica da fila.

``action=plan`` é o passo 4 do FLUXO TRABALHO: o dono pede o plano na tela. A
execução vira nota Markdown no cofre (plano de ação + andamento), aberta no
Obsidian e copiada para a pasta Nextcloud do dono, com o evento
``obsidian_opened`` no activity.log.
"""
from __future__ import annotations

import json

from core import task_mirror


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def task_mirror_action(parameters: dict, player=None, speak=None,
                       session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "show").strip().lower()
    try:
        if action in ("status", "estado", "lista", "list"):
            return _json({"status": "ok", "action": "status",
                          **task_mirror.status()})
        if action in ("plan", "plano", "obsidian", "nota"):
            result = task_mirror.open_plan(
                ident=p.get("task_id") or p.get("id"),
                query=str(p.get("query") or p.get("title") or ""),
                plan=str(p.get("plan") or ""))
            return _json({"status": "ok" if result.get("ok") else "error",
                          "action": "plan", **result})
        if action not in ("show", "mostrar", "abrir", "open", "ver"):
            return _json({"status": "error", "action": action,
                          "error": "Use action=show, action=plan "
                                   "ou action=status."})
        result = task_mirror.show(
            ident=p.get("task_id") or p.get("id"),
            query=str(p.get("query") or p.get("title") or ""))
        payload = {"status": "ok" if result.get("ok") else "error",
                   "action": "show", **result}
        if not result.get("ok") and "message" not in payload:
            payload["error"] = "Não consegui abrir o espelho."
        return _json(payload)
    except Exception as exc:  # noqa: BLE001 - ação sempre devolve JSON
        return _json({"status": "error", "action": action,
                      "error": str(exc)[:300]})


TOOL = {
    "name": "task_mirror",
    "description": (
        "Abre na tela o trabalho pesado em andamento. action=show abre o "
        "terminal visível com o log (tail -F) — o que o subagente está fazendo, "
        "comando por comando; use com 'me mostra trabalhando', 'abre o "
        "espelho', 'quero ver o que está rodando'. action=plan abre a NOTA do "
        "plano de ação no Obsidian (passo 4 do fluxo de trabalho: o dono pede "
        "para ver o plano na tela) e copia a nota para o Nextcloud do dono. "
        "action=status lista os espelhos conhecidos. Sem task_id, escolhe a "
        "tarefa da fila que está rodando ou a execução em segundo plano mais "
        "recente; com query, procura pelo título da tarefa."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["show", "plan", "status"],
                "description": ("show (padrão) abre o espelho na tela; "
                                "plan abre a nota do plano de ação no "
                                "Obsidian; status lista."),
            },
            "task_id": {
                "type": "STRING",
                "description": "Id da tarefa da fila (opcional).",
            },
            "query": {
                "type": "STRING",
                "description": "Trecho do título da tarefa, quando o dono a cita.",
            },
            "plan": {
                "type": "STRING",
                "description": ("Passos do plano de ação para a nota, quando o "
                                "cérebro já os tem; vazio usa o plano do job."),
            },
        },
        "required": [],
    },
    "handler": task_mirror_action,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
