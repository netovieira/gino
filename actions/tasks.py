"""Voice/tool access to the owner's persistent task and reminder queue.

The owner can dictate several things at once ("põe na fila: comprar pão, pagar
a conta e me lembrar da consulta amanhã"). This action splits that list, stores
each item in ``core.task_queue`` and answers with the queue line the assistant
speaks: "Já está na fila; ainda estou fazendo <current>." or how many tasks are
waiting. Only explicitly queued items enter this queue; background Qoder agents
remain independent.
"""
from __future__ import annotations

import json
import re

from core import task_queue


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _summary(snap: dict) -> dict:
    return {
        "line": snap.get("line", ""),
        "current": snap.get("current"),
        "queued": snap.get("queued", 0),
        "next": snap.get("next"),
    }


def _status_snapshot() -> dict:
    try:
        return task_queue.status()
    except Exception:  # noqa: BLE001 - response must survive a broken queue file
        return {"line": "A fila está vazia.", "current": None,
                "queued": 0, "next": None}


def _ok(action: str, **extra) -> str:
    snap = _status_snapshot()
    payload = {"status": "ok", "action": action, **_summary(snap)}
    payload.update(extra)
    payload.setdefault("tasks", [])
    return _json(payload)


def _error(action: str, message: str) -> str:
    snap = _status_snapshot()
    payload = {"status": "error", "action": action, "error": str(message)[:300],
               **_summary(snap), "tasks": []}
    return _json(payload)


def _open_tasks() -> list[dict]:
    try:
        return task_queue.list_tasks("open")
    except Exception:  # noqa: BLE001
        return []


def _details(snap: dict) -> str:
    current = snap.get("current") or {}
    queued = int(snap.get("queued") or 0)
    nxt = snap.get("next") or {}
    parts: list[str] = []
    if current:
        parts.append(f"Fazendo agora: {current.get('title', '')}.")
        if nxt:
            parts.append(f"Próxima: {nxt.get('title', '')}.")
        elif queued:
            parts.append(f"{queued} aguardando.")
    elif nxt:
        parts.append(f"{queued} na fila; próxima: {nxt.get('title', '')}.")
    else:
        parts.append("Sem tarefas abertas.")
    return " ".join(parts)


def _split_text(value) -> list[str]:
    return [part.strip() for part in re.split(r"[\n;]+", str(value or ""))
            if part.strip()]


def _collect_entries(p: dict) -> list[str | dict]:
    entries: list[str | dict] = []

    def collect(value) -> None:
        if value is None:
            return
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            entries.append(dict(value))
        else:
            entries.extend(_split_text(value))

    collect(p.get("title"))
    collect(p.get("items"))
    if not entries:  # tolerate the model reusing the common single-task key
        collect(p.get("task") or p.get("text"))
    return entries


def _add(p: dict) -> str:
    raw_entries = _collect_entries(p)
    entries: list[dict] = []
    for raw in raw_entries:
        data = raw if isinstance(raw, dict) else {"title": raw}
        title = data.get("title") or data.get("text") or data.get("task")
        title = str(title or "").strip()
        if not title:
            continue
        row: dict = {"title": title}
        for key in ("due", "kind", "priority", "persona", "project", "payload"):
            value = data[key] if key in data else p.get(key)
            if value is not None:
                row[key] = value
        entries.append(row)
    if not entries:
        return _error("add", "Nenhuma tarefa informada; mande o título ou a lista.")

    added = task_queue.add_many(entries)
    return _ok("add", tasks=added, count=len(added),
               kinds=[row.get("kind") for row in added])


def _list(p: dict) -> str:
    wanted = p.get("status")
    wanted = str(wanted).strip() if wanted is not None else None
    if wanted and wanted.lower() in ("all", "todas", "*"):
        wanted = None
    rows = task_queue.list_tasks(wanted) if wanted else task_queue.list_tasks()
    return _ok("list", tasks=rows, count=len(rows))


def _status(p: dict) -> str:
    snap = _status_snapshot()
    return _ok("status", **_summary(snap), tasks=_open_tasks(),
               details=_details(snap))


def _task_id(p: dict) -> str:
    return str(p.get("id") or p.get("task_id") or "").strip()


def _cancel(p: dict) -> str:
    task_id = _task_id(p)
    if not task_id:
        return _error("cancel", "Informe o id da tarefa para cancelar.")
    row = task_queue.cancel(task_id)
    if row is None:
        return _error("cancel", f"Tarefa '{task_id}' não encontrada ou já finalizada.")
    return _ok("cancel", task=row, tasks=_open_tasks())


def _done(p: dict) -> str:
    task_id = _task_id(p)
    if not task_id:
        return _error("done", "Informe o id da tarefa para concluir.")
    result = str(p.get("result") or p.get("text") or "")
    row = task_queue.finish(task_id, ok=True, result=result)
    if row is None:
        return _error("done", f"Tarefa '{task_id}' não encontrada.")
    return _ok("done", task=row, tasks=_open_tasks())


def tasks(parameters: dict, player=None, speak=None,
          session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "status").strip().lower()
    try:
        if action in ("add", "adicionar", "queued", "queue", "enfileirar",
                      "lembrar", "remember"):
            return _add(p)
        if action in ("list", "listar", "itens", "items"):
            return _list(p)
        if action in ("status", "estado", "fila", "andamento", "progresso"):
            return _status(p)
        if action in ("cancel", "cancelar", "remover", "remove"):
            return _cancel(p)
        if action in ("done", "concluir", "concluida", "concluída", "finish",
                      "feito", "feita"):
            return _done(p)
        return _error(action, "Ação inválida; use add, list, status, cancel ou done.")
    except Exception as exc:  # noqa: BLE001 - an action must return JSON
        return _error(action, str(exc))


TOOL = {
    "name": "tasks",
    "description": (
        "Fila persistente de tarefas, lembretes e eventos do dono. Use "
        "action=add quando ele mandar fazer uma lista de coisas ou marcar um "
        "lembrete/evento: 'title' aceita uma tarefa ou várias separadas por "
        "linha/ponto e vírgula, e 'items' aceita uma lista; due é ISO e kind é "
        "task/reminder/event. Use action=list para ver os itens, action=status "
        "para responder 'já está na fila; ainda estou fazendo ...' com a tarefa "
        "atual, e action=cancel/done com o id para fechar. Só entra aqui o que "
        "foi explicitamente enfileirado; agentes Qoder em background são "
        "independentes."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["add", "list", "status", "cancel", "done"],
                "description": "O que fazer na fila.",
            },
            "title": {
                "type": "STRING",
                "description": "Uma tarefa, ou lista separada por linhas/ponto "
                               "e vírgula.",
            },
            "items": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Tarefas individuais (aceita também objetos com "
                               "title/due/kind/priority).",
            },
            "due": {
                "type": "STRING",
                "description": "Quando vence, em ISO (ex.: 2026-11-01T09:00). "
                               "Para lembretes/eventos.",
            },
            "kind": {
                "type": "STRING",
                "enum": ["task", "reminder", "event"],
                "description": "task (padrão), reminder ou event.",
            },
            "priority": {
                "type": "INTEGER",
                "description": "Maior valor sai primeiro (padrão 0).",
            },
            "persona": {
                "type": "STRING",
                "description": "Persona responsável, se houver.",
            },
            "project": {
                "type": "STRING",
                "description": "Projeto relacionado, se houver.",
            },
            "id": {
                "type": "STRING",
                "description": "Id da tarefa para cancel/done.",
            },
            "result": {
                "type": "STRING",
                "description": "Resumo do que foi feito em done.",
            },
            "status": {
                "type": "STRING",
                "description": "Filtro opcional de list "
                               "(queued, running, done, error, cancelled, open).",
            },
        },
        "required": ["action"],
    },
    "handler": tasks,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
