"""Voice/tool access to the memory spider web (subject/time/persona/project)."""
from __future__ import annotations

import json
from pathlib import Path

from core import memory_index, obsidian


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def memory_index_action(parameters: dict, player=None, speak=None,
                        session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "query").strip().lower()
    try:
        if action in ("tag", "mark", "focus"):
            active = memory_index.set_active(
                project=p.get("project"), subject=p.get("subject"))
            event = memory_index.record_event(
                "note", str(p.get("text") or "foco"),
                persona=p.get("persona"), project=p.get("project"),
                subject=p.get("subject"), source=str(p.get("source") or "voice"))
            return _json({"status": "ok", "active": active, "event": event})
        if action in ("query", "search", "find"):
            events = memory_index.query(
                persona=p.get("persona"), project=p.get("project"),
                subject=p.get("subject"), since=p.get("since"),
                until=p.get("until"), limit=_int(p.get("limit"), 20))
            return _json({"status": "ok", "count": len(events), "events": events})
        if action in ("link", "connect"):
            memory_index.link(p.get("from") or p.get("a"),
                              p.get("to") or p.get("b"),
                              kind=str(p.get("kind") or "related"))
            return _json({"status": "ok"})
        if action in ("render", "rebuild"):
            text = memory_index.render_spider()
            destination = obsidian.mirror_index(text, memory_index.load_graph())
            return _json({"status": "ok", "path": str(destination),
                          "markdown": text[:4000]})
        if action in ("open", "obsidian"):
            text = memory_index.render_spider()
            destination = obsidian.mirror_index(text, memory_index.load_graph())
            message = obsidian.open_path(destination)
            return _json({"status": "ok", "path": str(destination),
                          "message": message})
        if action in ("active", "status"):
            active = memory_index.active()
            return _json({"status": "ok", "active": active})
        return _json({"status": "error", "error":
                      "action inválida; use tag, query, link, render, open ou active"})
    except Exception as exc:  # noqa: BLE001
        return _json({"status": "error", "error": str(exc)[:300]})


TOOL = {
    "name": "memory_index",
    "description": (
        "Consulta e alimenta a teia de índices da conversa por assunto, hora, "
        "persona e projeto. Use action=query para achar o que uma persona fez, "
        "o que aconteceu num projeto/assunto ou numa janela de tempo; "
        "action=tag para marcar o projeto/assunto atual (os próximos turnos "
        "herdam); action=link para ligar dois nós; action=render/open para "
        "regenerar/abrir a teia no Obsidian. Tudo é markdown/JSON local."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["tag", "query", "link", "render", "open", "active"],
                "description": "O que fazer na teia.",
            },
            "subject": {"type": "STRING",
                        "description": "Assunto/tópico (ex.: 'corte de fala')."},
            "project": {"type": "STRING",
                        "description": "Projeto/caminho/nome (ex.: 'jarvis')."},
            "persona": {"type": "STRING",
                        "description": "Slug/nome da persona que cuidou do evento."},
            "text": {"type": "STRING",
                     "description": "Nota curta para o evento de tag."},
            "source": {"type": "STRING",
                       "description": "voz|texto|whatsapp|persona."},
            "since": {"type": "STRING",
                      "description": "ISO/YYYY-MM-DD HH:MM inicial."},
            "until": {"type": "STRING",
                      "description": "ISO/YYYY-MM-DD HH:MM final."},
            "limit": {"type": "INTEGER", "description": "Máximo de eventos."},
            "from": {"type": "STRING", "description": "Nó de origem (link)."},
            "to": {"type": "STRING", "description": "Nó de destino (link)."},
            "kind": {"type": "STRING", "description": "Tipo da ligação."},
        },
        "required": ["action"],
    },
    "handler": memory_index_action,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
