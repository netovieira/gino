"""Control Home Assistant through its REST API.

This action is deliberately narrow: it reads states, turns devices on/off,
adjusts a few values (brightness, temperature, volume), and activates scenes.
It never exposes Home Assistant configuration, automation deletion or arbitrary
services — those are not reachable from here at all.

The long-lived access token lives in the same vault as the other secrets
(``config/api_keys.json``, mode 600, namespace ``plugin_config.
home_assistant_secrets.token``). It is read at call time and never printed,
logged or returned in an error message.

Name resolution is by entity id or by the human friendly name, accent- and
case-insensitive, with a short in-memory state cache (``GET /api/states``).
Every network call has a 5 s timeout and every failure becomes a sentence the
assistant can speak instead of a traceback.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import unicodedata

try:
    import requests
except Exception:  # pragma: no cover - requests is a project dependency
    requests = None

BASE_URL = "http://127.0.0.1:8123"
TIMEOUT = 5.0
VAULT_NAMESPACE = "home_assistant_secrets"
VAULT_KEY = "token"
CACHE_TTL = 15.0

# Domains the action will toggle. Anything else (sensor, automation, script,
# group, …) is refused before a service call is built.
_TOGGLE_DOMAINS = frozenset({
    "light", "switch", "fan", "climate", "media_player", "humidifier",
    "water_heater", "input_boolean",
})

# Domains counted in the spoken "status" (scenes are not devices).
_DEVICE_DOMAINS = _TOGGLE_DOMAINS | {"cover", "vacuum"}

_UNAVAILABLE = frozenset({"unavailable"})
_UNKNOWN = frozenset({"unknown"})

# Words that name a domain in Portuguese and must not be used as a search
# token: "acende a luz da sala" is a light in the sala, not a device literally
# called "luz".
_DOMAIN_WORDS = {
    "luz": "light", "luzes": "light", "lampada": "light", "lampadas": "light",
    "abajur": "light", "abajures": "light", "lustre": "light", "lustres": "light",
    "led": "light", "leds": "light",
    "tomada": "switch", "tomadas": "switch", "interruptor": "switch",
    "interruptores": "switch", "switch": "switch", "plug": "switch",
    "ventilador": "fan", "ventiladores": "fan", "ventoinha": "fan",
    "ar": "climate", "clima": "climate", "condicionado": "climate",
    "termo": "climate", "termostato": "climate", "aquecimento": "climate",
    "tv": "media_player", "televisao": "media_player", "som": "media_player",
    "caixa": "media_player", "speaker": "media_player", "musica": "media_player",
    "cortina": "cover", "cortinas": "cover", "persiana": "cover",
    "persianas": "cover", "portao": "cover", "garagem": "cover",
    "aspirador": "vacuum", "robo": "vacuum",
    "cena": "scene", "cenas": "scene", "scene": "scene",
    "umidificador": "humidifier", "umidade": "humidifier",
}

_STOPWORDS = frozenset({
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "no", "na",
    "nos", "nas", "em", "e", "para", "pra", "por", "com", "meu", "minha",
    "the", "my", "of", "in", "on", "and", "to", "gino", "jarvis",
    "favor", "por", "gentileza",
})

_ACTION_ALIASES = {
    "status": "status", "estado": "status", "situacao": "status",
    "list": "list", "listar": "list", "lista": "list",
    "turn_on": "turn_on", "ligar": "turn_on", "acender": "turn_on",
    "abrir": "turn_on", "liga": "turn_on", "acende": "turn_on",
    "turn_off": "turn_off", "desligar": "turn_off", "apagar": "turn_off",
    "fechar": "turn_off", "desliga": "turn_off", "apaga": "turn_off",
    "set": "set", "ajustar": "set", "ajuste": "set", "ajusta": "set",
    "scene": "scene", "cena": "scene", "ativar_cena": "scene",
}

_cache_lock = threading.Lock()
_cache: dict = {"at": 0.0, "states": []}


class HomeControlError(Exception):
    """A failure whose message is already safe to show the owner."""


# ── vault / HTTP ─────────────────────────────────────────────────────────────

def _token() -> str:
    """The long-lived token, from the environment or the existing vault."""
    for env_name in ("JARVIS_HA_TOKEN", "HOME_ASSISTANT_TOKEN", "HASS_TOKEN"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    try:
        from memory.config_manager import get_plugin_setting
        return str(get_plugin_setting(VAULT_NAMESPACE, VAULT_KEY, "") or "").strip()
    except Exception:
        return ""


def _api(method: str, path: str, payload: dict | None = None):
    """One REST call. Raises HomeControlError with a speakable message."""
    if requests is None:
        raise HomeControlError("A biblioteca requests não está disponível.")
    token = _token()
    if not token:
        raise HomeControlError(
            "Token do Home Assistant não configurado no cofre do JARVIS.")
    url = f"{BASE_URL}{path}"
    kwargs = {
        "headers": {"Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"},
        "timeout": TIMEOUT,
    }
    if payload is not None:
        kwargs["json"] = payload
    try:
        response = requests.request(method, url, **kwargs)
    except requests.Timeout:
        raise HomeControlError(
            f"O Home Assistant não respondeu em {int(TIMEOUT)} segundos.")
    except requests.ConnectionError:
        raise HomeControlError(
            f"Não consegui falar com o Home Assistant em {BASE_URL}.")
    except Exception as e:
        # Exception text from requests never contains the Authorization header.
        raise HomeControlError(f"Falha ao falar com o Home Assistant: {e}")

    if response.status_code in (401, 403):
        raise HomeControlError(
            f"O Home Assistant recusou o token (HTTP {response.status_code}). "
            "Gere outro token e atualize o cofre do JARVIS.")
    if response.status_code == 404:
        raise HomeControlError("Entidade ou serviço não encontrado no Home Assistant.")
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise HomeControlError(
            f"O Home Assistant respondeu HTTP {response.status_code}"
            + (f": {detail}" if detail else "."))
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def _error_detail(response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            message = body.get("message") or body.get("error")
            if message:
                return " ".join(str(message).split())[:200]
    except Exception:
        pass
    return ""


def _invalidate() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["states"] = []


def _states(force: bool = False) -> list[dict]:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["states"]
        if not force and cached and now - float(_cache["at"] or 0.0) < CACHE_TTL:
            return list(cached)
    data = _api("GET", "/api/states")
    if not isinstance(data, list):
        raise HomeControlError("O Home Assistant não devolveu a lista de estados.")
    states = [item for item in data if isinstance(item, dict)
              and isinstance(item.get("entity_id"), str)]
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["states"] = states
    return list(states)


def _call_service(domain: str, service: str, data: dict) -> None:
    _api("POST", f"/api/services/{domain}/{service}", data)
    _invalidate()


# ── names and matching ───────────────────────────────────────────────────────

def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    for char in ("_", "-", "/", ".", ",", ":", ";", "(", ")", "[", "]"):
        text = text.replace(char, " ")
    return " ".join(text.lower().split())


def _name(entity: dict) -> str:
    attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
    friendly = str(attrs.get("friendly_name") or "").strip()
    return friendly or str(entity.get("entity_id") or "")


def _object_id(entity: dict) -> str:
    return str(entity.get("entity_id") or "").split(".", 1)[-1]


def _domain_of(entity: dict) -> str:
    return str(entity.get("entity_id") or "").split(".", 1)[0].lower()


def _tokens(value: object) -> set[str]:
    return {token for token in _fold(value).split()
            if token not in _STOPWORDS and token not in _DOMAIN_WORDS}


def _infer_domain(value: object) -> str:
    found = ""
    for token in _fold(value).split():
        domain = _DOMAIN_WORDS.get(token)
        if domain and not found:
            found = domain
    return found


def _options(entities: list[dict], limit: int = 5) -> str:
    names = [f"{_name(entity)} ({entity['entity_id']})" for entity in entities[:limit]]
    more = len(entities) - len(names)
    text = ", ".join(names)
    return text + (f" e mais {more}" if more > 0 else "")


def resolve_entity(query: object, states: list[dict],
                   domain: str | None = None) -> tuple[dict | None, str]:
    """Resolve an entity id or a human name. Returns (entity, error_message)."""
    raw = str(query or "").strip()
    if not raw:
        return None, "Diga qual dispositivo (ou passe entity_id)."

    by_id = {str(entity.get("entity_id", "")).lower(): entity for entity in states}
    lowered = raw.lower()
    if lowered in by_id and (not domain or _domain_of(by_id[lowered]) == domain.lower()):
        return by_id[lowered], ""

    inferred = _infer_domain(raw)
    domain = (domain or inferred or "").lower() or None
    pool = [entity for entity in states
            if not domain or _domain_of(entity) == domain]
    if not pool:
        if domain:
            return None, f"Nenhuma entidade do domínio '{domain}' no Home Assistant."
        return None, "O Home Assistant não devolveu entidades."

    folded = _fold(raw)
    for entity in pool:
        if folded and (folded == _fold(_name(entity))
                       or folded == _fold(_object_id(entity))):
            return entity, ""

    query_tokens = _tokens(raw)
    # A bare domain word ("luz", "tomada") is a request for the only device of
    # that kind, or a question when there are several.
    if not query_tokens:
        if len(pool) == 1:
            return pool[0], ""
        return None, (f"Qual deles? {_options(pool)}" if len(pool) <= 5
                      else f"Há {len(pool)} opções nesse tipo; diga o nome completo.")

    scored: list[tuple[int, dict]] = []
    for entity in pool:
        candidate = _tokens(_name(entity)) | _tokens(_object_id(entity))
        overlap = len(query_tokens & candidate)
        if overlap == 0:
            continue
        if query_tokens <= candidate:
            score = 1000 - len(candidate)
        else:
            score = overlap * 10 - len(query_tokens - candidate)
        if score > 0:
            scored.append((score, entity))

    if not scored:
        return None, (f"Não encontrei '{raw}' no Home Assistant."
                      + (f" Opções: {_options(pool, 4)}." if domain else ""))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    tied = [entity for score, entity in scored if score == best_score]
    if len(tied) > 1:
        return None, f"Tem mais de um '{raw}': {_options(tied)}. Qual?"
    return scored[0][1], ""


# ── formatting ───────────────────────────────────────────────────────────────

def _is_on(entity: dict) -> bool:
    state = str(entity.get("state") or "").lower()
    domain = _domain_of(entity)
    if domain in ("light", "switch", "fan", "input_boolean", "humidifier",
                  "water_heater"):
        return state == "on"
    if domain == "climate":
        return state not in ("off",) and state not in _UNAVAILABLE | _UNKNOWN
    if domain == "media_player":
        return state not in ("off", "idle", "standby") and state not in _UNAVAILABLE | _UNKNOWN
    if domain == "cover":
        return state in ("open", "opening")
    if domain == "vacuum":
        return state in ("cleaning", "returning")
    return state == "on"


def _names_text(entities: list[dict], limit: int = 8) -> str:
    names = [_name(entity) for entity in entities[:limit]]
    more = len(entities) - len(names)
    return ", ".join(names) + (f" e mais {more}" if more > 0 else "")


def _status(states: list[dict]) -> str:
    devices = [entity for entity in states if _domain_of(entity) in _DEVICE_DOMAINS]
    if not devices:
        return (f"Home Assistant tem {len(states)} entidades, mas nenhuma "
                "controlável (luz, tomada, clima ou similar).")
    on = [entity for entity in devices if _is_on(entity)]
    unavailable = [entity for entity in devices
                   if str(entity.get("state") or "").lower() in _UNAVAILABLE]
    unknown = [entity for entity in devices
               if str(entity.get("state") or "").lower() in _UNKNOWN]
    off = len(devices) - len(on) - len(unavailable) - len(unknown)
    parts = [f"{len(devices)} dispositivos: {len(on)} ligados, {max(0, off)} "
             f"desligados, {len(unavailable)} offline"]
    if unknown:
        parts.append(f"{len(unknown)} sem estado")
    text = "Casa: " + parts[0]
    if len(parts) > 1:
        text += ", " + parts[1]
    text += "."
    if on:
        text += f" Ligados: {_names_text(on)}."
    if unavailable:
        text += f" Offline: {_names_text(unavailable)}."
    if unknown:
        text += f" Sem estado: {_names_text(unknown)}."
    return text


def _list(states: list[dict], domain: str) -> str:
    domain = str(domain or "").strip().lower()
    if domain:
        pool = [entity for entity in states if _domain_of(entity) == domain]
        if not pool:
            return f"Nenhuma entidade do domínio '{domain}' no Home Assistant."
        lines = [f"{_name(entity)} ({entity['entity_id']}): "
                 f"{entity.get('state', '?')}" for entity in pool[:60]]
        more = len(pool) - len(lines)
        if more > 0:
            lines.append(f"... e mais {more}.")
        return f"{len(pool)} entidade(s) em {domain}:\n" + "\n".join(lines)

    grouped: dict[str, list[dict]] = {}
    for entity in states:
        grouped.setdefault(_domain_of(entity), []).append(entity)
    if not grouped:
        return "O Home Assistant não devolveu entidades."
    lines = []
    for name in sorted(grouped):
        items = grouped[name]
        lines.append(f"{name} ({len(items)}): {_names_text(items, 6)}")
    return f"{len(states)} entidades em {len(grouped)} domínios:\n" + "\n".join(lines)


def _state_label(entity: dict, verb: str) -> str:
    state = str(entity.get("state") or "").lower()
    if state in _UNAVAILABLE:
        return (f"{_name(entity)} está indisponível no Home Assistant "
                f"({verb} enviado, mas o aparelho não está online).")
    return f"{_name(entity)}: {verb}."


# ── actions ──────────────────────────────────────────────────────────────────

def _target(parameters: dict) -> str:
    for key in ("entity_id", "target", "name", "device"):
        value = parameters.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _turn(parameters: dict, turn_on: bool) -> str:
    target = _target(parameters)
    if not target:
        return ("Diga qual dispositivo ligar/desligar "
                "(entity_id ou nome, ex.: 'luz da sala').")
    domain = str(parameters.get("domain") or "").strip().lower() or None
    states = _states()
    entity, error = resolve_entity(target, states, domain=domain)
    if entity is None:
        return error
    entity_domain = _domain_of(entity)
    if entity_domain not in _TOGGLE_DOMAINS:
        return (f"Não sei ligar/desligar {_name(entity)} "
                f"(domínio {entity_domain}).")
    service = "turn_on" if turn_on else "turn_off"
    _call_service(entity_domain, service, {"entity_id": entity["entity_id"]})
    return _state_label(entity, "ligado" if turn_on else "desligado")


_LIGHT_KEYS = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct",
    "color_name", "color_temp", "color_temp_kelvin", "rgb_color", "rgbw_color",
    "rgbww_color", "hs_color", "xy_color", "effect", "flash", "transition",
})
_LIGHT_NUMERIC_KEYS = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct",
    "color_temp", "color_temp_kelvin", "transition",
})


def _values(parameters: dict) -> dict:
    raw = parameters.get("params", parameters.get("values"))
    values: dict = {}
    if isinstance(raw, dict):
        values.update(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                values.update(parsed)
        except Exception:
            for part in text.replace(";", ",").split(","):
                key, sep, value = part.partition("=")
                if not sep:
                    continue
                key = key.strip().lower()
                value = value.strip()
                try:
                    value = float(value) if "." in value else int(value)
                except ValueError:
                    pass
                values[key] = value
    for key in ("brightness", "brightness_pct", "color_temp_kelvin", "temperature",
                "target_temp", "hvac_mode", "fan_mode", "swing_mode",
                "preset_mode", "position", "volume_level", "priority"):
        if key in parameters and key not in values:
            values[key] = parameters[key]
    if "value" in parameters and "value" not in values:
        values["value"] = parameters["value"]
    return values


def _number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _light_data(entity: dict, values: dict) -> dict | None:
    data = {"entity_id": entity["entity_id"]}
    for key in _LIGHT_KEYS:
        if key not in values:
            continue
        value = values[key]
        if key in _LIGHT_NUMERIC_KEYS:
            number = _number(value)
            if number is None:
                return None
            value = number
        data[key] = value
    if "value" in values and "brightness_pct" not in data and "brightness" not in data:
        level = _number(values["value"])
        if level is not None:
            if 0 <= level <= 100:
                data["brightness_pct"] = level
            elif level <= 255:
                data["brightness"] = level
    return data if len(data) > 1 else None


def _set(entity: dict, values: dict) -> tuple[str, str, dict] | tuple[None, str, None]:
    """(domain, service, data) for one entity, or a clear refusal."""
    domain = _domain_of(entity)
    eid = entity["entity_id"]

    if domain == "light":
        data = _light_data(entity, values)
        if data is None:
            return None, ("Para luz use brightness_pct, color_temp_kelvin, "
                          "rgb_color ou value (0–100)."), None
        return "light", "turn_on", data

    if domain == "climate":
        if any(key in values for key in ("temperature", "target_temp", "value")):
            raw = values.get("temperature", values.get("target_temp",
                                                        values.get("value")))
            temp = _number(raw)
            if temp is None:
                return None, "A temperatura precisa ser um número.", None
            return "climate", "set_temperature", {"entity_id": eid,
                                                  "temperature": temp}
        for key, service in (("hvac_mode", "set_hvac_mode"),
                             ("mode", "set_hvac_mode"),
                             ("fan_mode", "set_fan_mode"),
                             ("swing_mode", "set_swing_mode"),
                             ("preset_mode", "set_preset_mode")):
            if key in values:
                field = "hvac_mode" if key == "mode" else key
                return "climate", service, {"entity_id": eid, field: values[key]}
        return None, ("Para clima use temperature, hvac_mode, fan_mode, "
                      "swing_mode ou preset_mode."), None

    if domain == "media_player":
        if "volume_level" in values or "volume" in values:
            raw = values.get("volume_level", values.get("volume"))
            level = _number(raw)
            if level is None:
                return None, "O volume precisa ser um número.", None
            if level > 1:
                level = level / 100.0
            level = min(1.0, max(0.0, level))
            return "media_player", "volume_set", {
                "entity_id": eid, "volume_level": level}
        if "source" in values:
            return "media_player", "select_source", {
                "entity_id": eid, "source": values["source"]}
        return None, "Para o player use volume_level (0–1) ou source.", None

    if domain == "fan":
        if "percentage" in values or "value" in values:
            raw = values.get("percentage", values.get("value"))
            level = _number(raw)
            if level is None:
                return None, "A velocidade precisa ser um número de 0 a 100.", None
            return "fan", "set_percentage", {
                "entity_id": eid, "percentage": int(min(100, max(0, level)))}
        if "preset_mode" in values or "speed" in values:
            preset = values.get("preset_mode", values.get("speed"))
            return "fan", "set_preset_mode", {
                "entity_id": eid, "preset_mode": preset}
        return None, "Para ventilador use percentage (0–100) ou preset_mode.", None

    if domain == "cover":
        if "position" in values or "value" in values:
            raw = values.get("position", values.get("value"))
            position = _number(raw)
            if position is None:
                return None, "A posição precisa ser um número de 0 a 100.", None
            return "cover", "set_cover_position", {
                "entity_id": eid, "position": int(min(100, max(0, position)))}
        return None, "Para cortina/portão use position (0–100).", None

    if domain == "humidifier":
        raw = values.get("humidity", values.get("target_humidity",
                                                 values.get("value")))
        humidity = _number(raw)
        if humidity is None:
            return None, "A umidade precisa ser um número de 0 a 100.", None
        return "humidifier", "set_humidity", {
            "entity_id": eid, "humidity": int(min(100, max(0, humidity)))}

    if domain == "water_heater":
        raw = values.get("temperature", values.get("value"))
        temp = _number(raw)
        if temp is None:
            return None, "A temperatura precisa ser um número.", None
        return "water_heater", "set_temperature", {
            "entity_id": eid, "temperature": temp}

    return None, f"Não sei ajustar valores de {_name(entity)} (domínio {domain}).", None


def _set_action(parameters: dict) -> str:
    target = _target(parameters)
    if not target:
        return "Diga qual dispositivo ajustar (entity_id ou nome)."
    values = _values(parameters)
    if not values:
        return ("Diga o que ajustar: brightness_pct, temperature, "
                "volume_level, position ou value.")
    domain = str(parameters.get("domain") or "").strip().lower() or None
    entity, error = resolve_entity(target, _states(), domain=domain)
    if entity is None:
        return error
    service_domain, service, data = _set(entity, values)
    if service_domain is None:
        return service  # the refusal sentence
    _call_service(service_domain, service, data)
    return f"{_name(entity)} ajustado ({service})."


def _scene(parameters: dict) -> str:
    name = _target(parameters)
    scenes = [entity for entity in _states() if _domain_of(entity) == "scene"]
    if not name:
        if not scenes:
            return "Nenhuma cena cadastrada no Home Assistant."
        return f"Cenas disponíveis: {_options(scenes, 10)}."
    if not scenes:
        return "Nenhuma cena cadastrada no Home Assistant."
    entity, error = resolve_entity(name, scenes, domain="scene")
    if entity is None:
        return error
    _call_service("scene", "turn_on", {"entity_id": entity["entity_id"]})
    return f"Cena {_name(entity)} ativada."


def home_control(parameters: dict, player=None, speak=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    action = _ACTION_ALIASES.get(action, action)
    try:
        if action == "status":
            return _status(_states())
        if action == "list":
            domain = str(params.get("domain") or "").strip().lower()
            return _list(_states(), domain)
        if action == "turn_on":
            return _turn(params, True)
        if action == "turn_off":
            return _turn(params, False)
        if action == "set":
            return _set_action(params)
        if action == "scene":
            return _scene(params)
        return (f"Ação desconhecida: '{action}'. Use status, list, turn_on, "
                "turn_off, set ou scene.")
    except HomeControlError as e:
        return str(e)
    except Exception as e:  # a tool must always answer, never crash the call
        return f"Falha no controle do Home Assistant: {e}"


def auto_authorized(parameters: dict | None = None) -> bool:
    """True for the simple house actions that need no spoken confirmation.

    The bridge uses this as a narrow standing grant: only the actions this
    tool actually implements qualify. Destructive Home Assistant operations
    (deleting automations, editing configuration) are not implemented here at
    all, so there is nothing else to grant.
    """
    action = str((parameters or {}).get("action") or "status").strip().lower()
    return _ACTION_ALIASES.get(action, action) in {
        "status", "list", "turn_on", "turn_off", "set", "scene"}


def bridge_preauthorized(name: str, args: dict | None = None) -> bool:
    """The same narrow grant with the bridge's ``(name, args)`` signature."""
    if str(name) != "home_control":
        return False
    return auto_authorized(args if isinstance(args, dict) else {})


TOOL = {
    "name": "home_control",
    "description": (
        "Controls the owner's Home Assistant: reports which devices are on or "
        "offline, lists entities, turns lights/switches/fans/climate on or off, "
        "adjusts brightness, temperature, volume or cover position, and "
        "activates scenes. Targets may be an entity_id or a human name in "
        "Portuguese (e.g. 'luz da sala'). Use action='status' for 'quais luzes "
        "estão ligadas?' and action='list' with a domain for 'lista as tomadas'. "
        "This action cannot change Home Assistant configuration or delete "
        "automations; do not promise that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status, list, turn_on, turn_off, set or scene."),
                "enum": ["status", "list", "turn_on", "turn_off", "set", "scene"],
            },
            "entity_id": {
                "type": "STRING",
                "description": ("Entity id (e.g. light.luz_da_sala) or human "
                                "name in Portuguese (e.g. 'luz da sala')."),
            },
            "target": {
                "type": "STRING",
                "description": "Alias for entity_id: the device or scene name.",
            },
            "name": {
                "type": "STRING",
                "description": "For action='scene': the scene name.",
            },
            "domain": {
                "type": "STRING",
                "description": ("Optional domain filter: light, switch, climate, "
                                "media_player, fan, cover, scene."),
            },
            "params": {
                "type": "OBJECT",
                "description": ("For action='set': brightness_pct, color_temp_kelvin, "
                                "temperature, hvac_mode, fan_mode, volume_level, "
                                "position or value."),
            },
            "value": {
                "type": "STRING",
                "description": ("Convenience value for action='set': light 0–100, "
                                "climate temperature, volume 0–100, cover 0–100."),
            },
        },
    },
    "handler": home_control,
}
