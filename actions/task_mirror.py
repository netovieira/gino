"""Abre o espelho visível de uma execução pesada (terminal com o log ao vivo).

O dono diz "me mostra trabalhando": esta ação escolhe a tarefa da fila que está
rodando (ou o espelho agregado das execuções em segundo plano), cria o log se
precisar e abre o ptyxis com ``tail -F`` nele. É a versão explícita do que já
acontece sozinho quando um subagente começa; serve para reabrir a janela e para
apontar uma tarefa específica da fila.
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
        if action not in ("show", "mostrar", "abrir", "open", "ver"):
            return _json({"status": "error", "action": action,
                          "error": "Use action=show ou action=status."})
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
        "Abre na tela um terminal visível espelhando (tail -F) o log da "
        "execução pesada em andamento — o que o subagente está fazendo, comando "
        "por comando. Use quando o dono disser 'me mostra trabalhando', 'abre o "
        "espelho', 'quero ver o que está rodando'. Sem task_id, escolhe a tarefa "
        "da fila que está rodando ou a execução em segundo plano mais recente; "
        "com query, procura pelo título da tarefa. action=status lista os "
        "espelhos conhecidos."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["show", "status"],
                "description": "show (padrão) abre o espelho; status lista.",
            },
            "task_id": {
                "type": "STRING",
                "description": "Id da tarefa da fila (opcional).",
            },
            "query": {
                "type": "STRING",
                "description": "Trecho do título da tarefa, quando o dono a cita.",
            },
        },
        "required": [],
    },
    "handler": task_mirror_action,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
