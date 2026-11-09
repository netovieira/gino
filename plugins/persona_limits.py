"""Concurrency of personas: the two numbers the owner can change in the UI.

O dono pode ter quantas personas quiser no quadro — o que tem teto é o que se
pede ao mesmo tempo (ordem de 09/11/2026): até quatro personas recebem pedidos
juntas e cada uma roda até quatro tasks por vez. Este plugin existe por causa
do ``PLUGIN_SETTINGS``: ele põe os dois números em ⚙ → PLUGIN SETTINGS →
PERSONA CONCURRENCY, salvos no namespace ``personas`` que
:mod:`core.persona_ops.limits` lê. A ferramenta ``persona_limits`` responde o
estado (limites, quem está trabalhando e a cascata de modelos gratuitos).

Nada aqui decide sozinho: a contagem das vagas vive em
``core.persona_ops.limits.SLOTS`` e é o orquestrador quem pede.
"""
from __future__ import annotations

import json

from core import personas as persona_store
from core.persona_ops import limits

NAMESPACE = "personas"

PLUGIN = {
    "name": "persona_limits",
    "description": (
        "Reports and sets how many personas may be asked at the same time "
        "(max_concurrent_personas) and how many tasks each one may run at "
        "once (max_tasks_per_persona) — defaults 4 and 4, also editable in "
        "⚙ → PLUGIN SETTINGS → PERSONA CONCURRENCY. Use when the user asks "
        "how many personas can work together, complains that too much was "
        "asked at once, or says 'a partir de agora são N por vez'. There is "
        "no cap on how many personas exist; the cap is on how much is asked "
        "at the same time. Also shows the free-model cascade when asked which "
        "model each persona will use."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status (default) | set",
            },
            "max_concurrent_personas": {
                "type": "INTEGER",
                "description": "Novo teto de personas pedidas ao mesmo tempo "
                               "(action=set; 1 a 64).",
            },
            "max_tasks_per_persona": {
                "type": "INTEGER",
                "description": "Novo teto de tasks por persona "
                               "(action=set; 1 a 64).",
            },
        },
        "required": [],
    },
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "PERSONA CONCURRENCY (how much may be asked at once)",
    "fields": [
        {"key": limits.KEY_CONCURRENT,
         "label": "Personas pedidas ao mesmo tempo",
         "type": "text",
         "default": str(limits.DEFAULT_MAX_CONCURRENT_PERSONAS),
         "placeholder": str(limits.DEFAULT_MAX_CONCURRENT_PERSONAS)},
        {"key": limits.KEY_TASKS,
         "label": "Tasks por persona ao mesmo tempo",
         "type": "text",
         "default": str(limits.DEFAULT_MAX_CONCURRENT_PERSONAS),
         "placeholder": str(limits.DEFAULT_MAX_CONCURRENT_PERSONAS)},
    ],
    "note": ("Não há limite de cadastro: quantas personas quiser, quantas "
             "linhas houver. Estes dois números só seguram o que é pedido AO "
             "MESMO TEMPO — 4 por padrão, de 1 a 64."),
}


def _status() -> dict:
    saved = limits.read_limits()
    snapshot = limits.slots().snapshot()
    free = [row["model"] for row in persona_store.cascade_report()
            if row["state"] == "livre"]
    return {
        "status": "ok",
        "limits": saved,
        "working": snapshot,
        "cascade_free": free,
        "personas_registered": len(persona_store.list_personas()),
        "message": (f"Posso pedir a {saved['max_concurrent_personas']} "
                    f"personas ao mesmo tempo; cada uma roda até "
                    f"{saved['max_tasks_per_persona']} tasks. "
                    f"{snapshot.get('personas_working', 0)} trabalhando agora."),
    }


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "status").strip().casefold()
    try:
        if action in {"set", "ajustar", "definir", "mudar", "salvar"}:
            saved = limits.save_limits(
                concurrent=p.get("max_concurrent_personas"),
                per_persona=p.get("max_tasks_per_persona"))
            state = _status()
            state["limits"] = saved
            state["message"] = (
                f"Ajustado: até {saved['max_concurrent_personas']} personas "
                f"pedidas juntas, {saved['max_tasks_per_persona']} tasks por "
                f"persona. Salvo nas configurações.")
            return json.dumps(state, ensure_ascii=False)
        if action not in {"", "status", "list", "show", "ver", "como_esta"}:
            return json.dumps({"status": "error",
                               "error": "action inválida; use status ou set"},
                              ensure_ascii=False)
        return json.dumps(_status(), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)},
                          ensure_ascii=False)
