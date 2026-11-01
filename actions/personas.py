"""The voice tool for creating and steering personas.

The owner says "crie uma persona pra React chamada Fulana..." and the main
assistant calls this action. It validates, reads the model multiplier back,
and only writes after an explicit confirmation when the model is paid. Every
reply is JSON (utf-8, accents preserved) so the model can quote exact fields
instead of inventing them.

Read-only actions (list/show/models/for_project) touch nothing. create/update
require ``confirm_paid=true`` for a paid model; delete and link_project are
ordinary high-risk tools handled by the bridge's confirmation window.
"""
from __future__ import annotations

import json
import math
import re

from core import personas as persona_store
from core import projects

_SECRET_RE = re.compile(
    r"(?i)(api[_\-\s]?key|access[_\-\s]?key|secret[_\-\s]?key|password|"
    r"passwd|senha|token|bearer\s+[a-z0-9]+|sk-[a-z0-9]{4,})"
)

_FIELD_KEYS = ("id", "name", "gender", "voice_id", "personality", "skills",
               "stacks", "model", "reasoning", "context_size")

_ACTION_ALIASES = {
    "criar": "create", "crie": "create", "nova": "create",
    "editar": "update", "atualizar": "update", "alterar": "update",
    "apagar": "delete", "remover": "delete", "excluir": "delete",
    "listar": "list", "mostrar": "show", "ver": "show",
    "modelos": "models", "projeto": "for_project", "para_projeto": "for_project",
    "vincular": "link_project", "vincular_projeto": "link_project",
}


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("[segredo removido]", str(text or ""))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {
        "1", "true", "sim", "yes", "pode", "confirmado", "autorizado",
    }


def _number(value, default: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


def _max_price_factor() -> float:
    """Configurable ceiling for a persona's paid model (default 1.0)."""
    try:
        from memory import config_manager
        return _number(
            config_manager.get_plugin_setting(
                "personas", "max_price_factor", 1.0), 1.0)
    except Exception:
        return 1.0


def _multiplier(persona_or_model) -> str:
    if isinstance(persona_or_model, dict):
        value = persona_or_model.get("model_price_factor")
        if value is None:
            value = persona_or_model.get("priceFactor", 0.0)
    else:
        value = persona_or_model
    return persona_store.format_multiplier(value)


def _summary(persona: dict) -> dict:
    return {
        "id": persona.get("id"),
        "name": persona.get("name"),
        "gender": persona.get("gender"),
        "address": persona_store.address(persona),
        "model": persona.get("model"),
        "multiplier": _multiplier(persona),
        "reasoning": persona.get("reasoning"),
        "context_size": persona.get("context_size"),
        "skills": list(persona.get("skills") or []),
        "stacks": list(persona.get("stacks") or []),
    }


def _fields_from(parameters: dict) -> dict:
    fields = {}
    for key in _FIELD_KEYS:
        if key not in parameters:
            continue
        value = parameters[key]
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        fields[key] = value
    return fields


def _model_message(parameters: dict, existing: dict | None):
    """(model, proposed_value) or raise ValueError for an unknown model."""
    proposed = str(parameters.get("model") or "").strip()
    if not proposed and existing:
        proposed = str(existing.get("model") or "")
    proposed = proposed or persona_store.DEFAULT_MODEL
    model = persona_store.find_model(proposed)
    if model is None:
        options = ", ".join(m["value"] for m in persona_store.model_catalog())
        raise ValueError(
            f"modelo {proposed!r} não está no catálogo (disponíveis: {options})")
    return model


def _confirmation(persona_id: str, model: dict, limit: float) -> dict:
    factor = float(model.get("priceFactor") or 0.0)
    multiplier = persona_store.format_multiplier(factor)
    return {
        "status": "confirmation_required",
        "multiplier": multiplier,
        "model": model["value"],
        "displayName": model.get("displayName") or model["value"],
        "price_factor": factor,
        "max_price_factor": limit,
        "id": persona_id,
        "message": (f"O modelo {model.get('displayName') or model['value']} "
                    f"custa {multiplier} por uso. Posso salvar a persona?"),
    }


def _create(parameters: dict) -> dict:
    fields = _fields_from(parameters)
    wanted = str(fields.get("id") or fields.get("name") or "").strip()
    existing = persona_store.get(wanted) if wanted else None
    if existing:
        raise ValueError(f"já existe a persona {existing.get('name')} "
                         f"({existing.get('id')}); use action=update")

    model = _model_message(parameters, None)
    confirm = _truthy(parameters.get("confirm_paid"))
    limit = _max_price_factor()
    factor = float(model.get("priceFactor") or 0.0)
    if factor > 0:
        if factor > limit:
            raise ValueError(
                f"modelo {model.get('displayName')} custa "
                f"{persona_store.format_multiplier(factor)}, acima do limite "
                f"{persona_store.format_multiplier(limit)}")
        if not confirm:
            return _confirmation(
                persona_store._slugify(fields.get("id") or fields.get("name")),
                model, limit)

    persona = persona_store.upsert(fields, confirm_paid=confirm,
                                   max_price_factor=limit)
    return {
        "status": "created",
        "persona": persona,
        "multiplier": _multiplier(persona),
        "message": (f"Persona {persona['name']} criada "
                    f"({persona_store.address(persona)}), modelo "
                    f"{persona['model']} {_multiplier(persona)}."),
    }


def _update(parameters: dict) -> dict:
    fields = _fields_from(parameters)
    wanted = str(fields.get("id") or fields.get("name") or "").strip()
    existing = persona_store.get(wanted) if wanted else None
    if not existing:
        raise ValueError("persona não encontrada; informe id ou name")
    fields["id"] = existing["id"]

    model = _model_message(parameters, existing)
    confirm = _truthy(parameters.get("confirm_paid"))
    limit = _max_price_factor()
    factor = float(model.get("priceFactor") or 0.0)
    if factor > 0 and not confirm:
        if factor > limit:
            raise ValueError(
                f"modelo {model.get('displayName')} custa "
                f"{persona_store.format_multiplier(factor)}, acima do limite "
                f"{persona_store.format_multiplier(limit)}")
        return _confirmation(existing["id"], model, limit)

    persona = persona_store.upsert(fields, confirm_paid=confirm,
                                   max_price_factor=limit)
    return {
        "status": "updated",
        "persona": persona,
        "multiplier": _multiplier(persona),
        "message": f"Persona {persona['name']} atualizada.",
    }


def _delete(parameters: dict) -> dict:
    wanted = str(parameters.get("id") or parameters.get("name")
                 or parameters.get("persona") or "").strip()
    existing = persona_store.get(wanted) if wanted else None
    if not existing:
        raise ValueError("persona não encontrada; informe id ou name")
    persona_store.delete(existing["id"])
    return {
        "status": "deleted",
        "id": existing["id"],
        "name": existing.get("name"),
        "message": f"Persona {existing.get('name')} removida.",
    }


def _list() -> dict:
    items = persona_store.list_personas()
    return {
        "status": "ok",
        "count": len(items),
        "limit": persona_store.MAX_PERSONAS,
        "personas": [_summary(persona) for persona in items],
    }


def _show(parameters: dict) -> dict:
    wanted = str(parameters.get("id") or parameters.get("name") or "").strip()
    persona = persona_store.get(wanted) if wanted else None
    if not persona:
        return {"status": "not_found",
                "message": "Persona não encontrada."}
    return {
        "status": "ok",
        "persona": persona,
        "address": persona_store.address(persona),
        "multiplier": _multiplier(persona),
    }


def _models() -> dict:
    out = []
    for model in persona_store.model_catalog():
        out.append({
            "value": model["value"],
            "displayName": model["displayName"],
            "isFree": bool(model["isFree"]),
            "multiplier": persona_store.format_multiplier(
                model.get("priceFactor")),
            "context_windows": list(model.get("availableContextWindows") or []),
            "efforts": list(model.get("efforts") or []),
            "max_input_tokens": model.get("maxInputTokens"),
            "default_effort": model.get("defaultEffort"),
        })
    return {"status": "ok", "count": len(out), "models": out}


def _for_project(parameters: dict) -> dict:
    path = str(parameters.get("path") or "").strip()
    if not path:
        raise ValueError("informe path do projeto")
    result = projects.resolve(path, persona_store.list_personas())
    if result.get("status") == "match":
        persona = result["persona"]
        result["address"] = persona_store.address(persona)
        result["lead"] = (
            f"Vou pedir {persona_store.address(persona, contraction=True)} ver.")
        result["message"] = result["lead"]
    elif result.get("status") == "ambiguous":
        names = ", ".join(
            str(p.get("name") or p.get("id")) for p in result.get("candidates", []))
        result["message"] = (
            f"Mais de uma persona combina com o projeto ({names}); qual usar?")
    else:
        result["message"] = "Nenhuma persona combina com as stacks do projeto."
    return result


def _link_project(parameters: dict) -> dict:
    path = str(parameters.get("path") or "").strip()
    wanted = str(parameters.get("persona") or parameters.get("id") or "").strip()
    if not path:
        raise ValueError("informe path do projeto")
    persona = persona_store.get(wanted) if wanted else None
    if not persona:
        raise ValueError("persona não encontrada; informe persona (id ou nome)")
    record = projects.link(path, persona["id"])
    return {
        "status": "linked",
        "project": record,
        "persona": _summary(persona),
        "message": (f"Projeto {record['name']} vinculado "
                    f"{persona_store.address(persona, contraction=True)}."),
    }


_DISPATCH = {
    "create": lambda p: _create(p),
    "update": lambda p: _update(p),
    "delete": lambda p: _delete(p),
    "list": lambda p: _list(),
    "show": lambda p: _show(p),
    "models": lambda p: _models(),
    "for_project": lambda p: _for_project(p),
    "link_project": lambda p: _link_project(p),
}


def personas(parameters: dict, player=None, speak=None,
             session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "").strip().casefold()
    action = _ACTION_ALIASES.get(action, action)
    handler = _DISPATCH.get(action)
    if handler is None:
        return _json({
            "status": "error",
            "error": "action inválida; use create, update, delete, list, "
                     "show, models, for_project ou link_project",
        })
    try:
        return _json(handler(p))
    except ValueError as exc:
        return _json({"status": "error", "error": _scrub(str(exc))})
    except Exception as exc:  # never leak a raw payload into the reply
        return _json({
            "status": "error",
            "error": _scrub(f"falha ao processar personas: {exc}"),
        })


TOOL = {
    "name": "personas",
    "description": (
        "Cria e administra as personas especialistas do assistente (Fulana do "
        "frontend, Beltrano do backend...). Use action=list/show/models para "
        "consultar, for_project para descobrir qual persona cuida da stack de "
        "um projeto, link_project para fixar uma persona a um projeto, e "
        "create/update/delete para alterar o quadro. Toda persona tem nome, "
        "gênero (f/m/n), voice_id do Fish Audio, personalidade, skills, stacks, "
        "modelo, reasoning e contexto. Modelo pago só é salvo com confirmação "
        "explícita: mostre o multiplicador ao dono e chame de novo com "
        "confirm_paid=true depois do 'pode'. Nunca grave segredos na "
        "personalidade."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["create", "update", "delete", "list", "show",
                         "models", "for_project", "link_project"],
                "description": "Ação a executar.",
            },
            "id": {
                "type": "STRING",
                "description": "Slug da persona (ex.: fulana-front).",
            },
            "name": {
                "type": "STRING",
                "description": "Nome falado da persona (ex.: Fulana).",
            },
            "gender": {
                "type": "STRING",
                "description": "'f', 'm' ou 'n' — define artigo e concordância.",
            },
            "voice_id": {
                "type": "STRING",
                "description": "reference_id da voz no Fish Audio.",
            },
            "personality": {
                "type": "STRING",
                "description": "Personalidade em texto (máx. 2000 chars, sem "
                               "segredos).",
            },
            "skills": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Nomes de skills existentes em skills/.",
            },
            "stacks": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Stacks do projeto (react, nextjs, typescript, "
                               "python, fastapi, rust...).",
            },
            "model": {
                "type": "STRING",
                "description": "Modelo do catálogo (padrão Qwen3.8-Flash, grátis).",
            },
            "reasoning": {
                "type": "STRING",
                "description": "'low', 'medium', 'high' ou 'max'.",
            },
            "context_size": {
                "type": "INTEGER",
                "description": "Janela de contexto em tokens (ex.: 200000).",
            },
            "path": {
                "type": "STRING",
                "description": "Pasta do projeto para for_project/link_project.",
            },
            "persona": {
                "type": "STRING",
                "description": "Id ou nome da persona para link_project.",
            },
            "confirm_paid": {
                "type": "BOOLEAN",
                "description": "True somente depois que o dono confirmou o "
                               "multiplicador de um modelo pago.",
            },
        },
        "required": ["action"],
    },
    "handler": personas,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
