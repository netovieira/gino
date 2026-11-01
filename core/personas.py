"""Personas: named specialist assistants the owner creates by voice.

WHY THIS EXISTS
    The main assistant stays the only chat with the owner; personas are
    specialists it moderates ("Vou pedir pra Fulana ver"). Each one has its own
    name, grammatical gender, Fish Audio voice, personality, skills, project
    stacks, model, reasoning effort and context window.

STORAGE
    memory/personas.json (local, git-ignored), written atomically::

        {"version": 1,
         "personas": {"fulana-front": {...}},
         "retired": {"fulana-antiga": {...}}}

    Active records carry ``aliases`` (old names, at most 20) and ``history``
    (rename entries). Deleting or retiring never erases a persona: the record
    moves to ``retired`` with ``retired_at``/``retired_status`` (left,
    retired, fired or renamed) and an optional ``successor``, so the owner can
    still call an old name and hear an in-character farewell.

    A malformed or missing store never raises: ``load()`` returns an empty
    board (or whatever slices are still valid), and the first ``save()``
    repairs it. Nothing outside that file is ever written.

PAID MODELS
    The live catalog (memory/models.json) carries Fish/Qoder price factors.
    A persona may use a paid model only with an explicit ``confirm_paid=True``
    and only while its factor is within ``max_price_factor``. The factor is
    snapshotted into the persona for display; the catalog stays the truth on
    the next edit.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

from core import projects as _projects

BASE_DIR = Path(__file__).resolve().parent.parent
PERSONAS_PATH = BASE_DIR / "memory" / "personas.json"
MODELS_PATH = BASE_DIR / "memory" / "models.json"
SKILLS_DIR = BASE_DIR / "skills"

MAX_PERSONAS = 12
MAX_PERSONALITY = 2000
MAX_ALIASES = 20
DEFAULT_MODEL = "Qwen3.8-Flash"
REASONING_LEVELS = ("low", "medium", "high", "max")
RETIRED_STATUSES = ("left", "retired", "fired", "renamed")
CONTEXT_MIN = 16_000
CONTEXT_MAX = 1_000_000
DEFAULT_CONTEXT = 200_000

_GENDERS = {"f", "m", "n"}
_GENDER_ALIASES = {
    "f": "f", "fem": "f", "feminino": "f", "feminina": "f", "female": "f",
    "m": "m", "masc": "m", "masculino": "m", "masculina": "m", "male": "m",
    "n": "n", "neutro": "n", "neutra": "n", "neutral": "n", "nb": "n",
}

# Anything that smells like a credential is refused in free text. A persona is
# spoken aloud and stored in a local JSON file; it is never the place for keys.
_SECRET_RE = re.compile(
    r"(?i)(api[_\-\s]?key|access[_\-\s]?key|secret[_\-\s]?key|password|"
    r"passwd|senha|token|bearer\s+[a-z0-9]|sk-[a-z0-9]{4,})"
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_LOCK = threading.RLock()

_FALLBACK_CATALOG = (
    ("qfmodel", "Qwen3.8-Flash"),
    ("efficient", "Efficient"),
)


# ── small helpers ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _slugify(text) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = raw.encode("ascii", "ignore").decode("ascii").lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:64].strip("-")


def _as_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_int(value):
    """Positive int from 200000, '200k', '1m' or '200.000'; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    text = str(value or "").strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*([km])?", text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    multiplier = {"k": 1_000, "m": 1_000_000}.get(match.group(2) or "", 1)
    return int(number * multiplier)


def format_multiplier(value) -> str:
    """Price factor as the owner hears it: 0.0x, 0.1x, 0.5x, 1.0x."""
    factor = _as_float(value)
    text = f"{abs(factor):.3f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return ("-" if factor < 0 else "") + text + "x"


def _text(value, limit: int = 0) -> str:
    text = str(value if value is not None else "").strip()
    return text[:limit] if limit else text


def _contains_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text or ""))


def _normalize_gender(value) -> str:
    key = _text(value).casefold()
    gender = _GENDER_ALIASES.get(key)
    if gender is None:
        raise ValueError("gênero inválido: use 'f', 'm' ou 'n'")
    return gender


def _validate_personality(value) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > MAX_PERSONALITY:
        raise ValueError(
            f"personalidade acima de {MAX_PERSONALITY} caracteres "
            f"({len(text)})")
    if _contains_secret(text):
        raise ValueError(
            "personalidade não pode conter segredos (api_key, password, "
            "token, sk-...)")
    return text


def _available_skills() -> list[str]:
    base = Path(SKILLS_DIR)
    if not base.is_dir():
        return []
    names = []
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_dir() and (Path(entry.path) / "SKILL.md").is_file():
                names.append(entry.name)
        except OSError:
            continue
    return sorted(names, key=str.casefold)


def _normalize_skills(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]

    available = _available_skills()
    known = {name.casefold(): name for name in available}
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        if not _SKILL_RE.match(text) or ".." in text:
            raise ValueError(f"nome de skill inválido: {text!r}")
        canonical = known.get(text.casefold())
        if canonical is None:
            options = ", ".join(available) or "nenhuma"
            raise ValueError(
                f"skill {text!r} não existe em skills/ (disponíveis: {options})")
        if canonical not in out:
            out.append(canonical)
    return out


# ── model catalog ───────────────────────────────────────────────────────────

def _fallback_catalog() -> list[dict]:
    return [
        {
            "value": value,
            "displayName": display,
            "isFree": True,
            "priceFactor": 0.0,
            "originalPriceFactor": 0.0,
            "availableContextWindows": [],
            "maxInputTokens": None,
            "efforts": list(REASONING_LEVELS),
            "defaultEffort": "medium",
        }
        for value, display in _FALLBACK_CATALOG
    ]


def _int_list(value) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,;]", value)
    else:
        items = value if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for item in items:
        number = _as_int(item)
        if number and number > 0 and number not in out:
            out.append(number)
    return out


def _effort_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,;]", value)
    else:
        items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("value") or item.get("name") or item.get("id")
        text = _text(item).casefold()
        if text and text not in out:
            out.append(text)
    return out


def _catalog_entry(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    value = _text(raw.get("value") or raw.get("id") or raw.get("name"))
    if not value:
        return None
    factor = _as_float(raw.get("priceFactor", raw.get("price_factor")), 0.0)
    original = _as_float(
        raw.get("originalPriceFactor", raw.get("original_price_factor")),
        factor)
    is_free = raw.get("isFree", raw.get("is_free"))
    if is_free is None:
        is_free = factor <= 0.0
    max_input = _as_int(
        raw.get("maxInputTokens", raw.get("max_input_tokens")))
    efforts = _effort_list(raw.get("efforts"))
    default = _text(
        raw.get("defaultEffort") or raw.get("default_effort")).casefold()
    if default not in efforts:
        default = efforts[0] if efforts else ""
    return {
        "value": value,
        "displayName": _text(raw.get("displayName")
                             or raw.get("display_name") or value),
        "isFree": bool(is_free),
        "priceFactor": factor,
        "originalPriceFactor": original,
        "availableContextWindows": _int_list(
            raw.get("availableContextWindows")
            or raw.get("available_context_windows")),
        "maxInputTokens": max_input if max_input and max_input > 0 else None,
        "efforts": efforts,
        "defaultEffort": default,
    }


def model_catalog() -> list[dict]:
    """The live model catalog, or the free allowlist when it is absent."""
    path = Path(MODELS_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_models = data.get("models") if isinstance(data, dict) else None
        if isinstance(raw_models, list):
            models = [entry for entry in
                      (_catalog_entry(item) for item in raw_models) if entry]
            if models:
                return models
    except Exception:
        pass
    return _fallback_catalog()


def find_model(value) -> dict | None:
    wanted = _text(value).casefold()
    if not wanted:
        return None
    for model in model_catalog():
        if (model["value"].casefold() == wanted
                or model["displayName"].casefold() == wanted):
            return model
    return None


def _context_bounds(model: dict) -> tuple[int, int]:
    windows = model.get("availableContextWindows") or []
    lower, upper = CONTEXT_MIN, CONTEXT_MAX
    if windows:
        lower = max(1, min(windows))
        upper = max(windows)
    max_input = model.get("maxInputTokens")
    if max_input:
        upper = min(upper, int(max_input))
    if upper < lower:
        upper = lower
    return lower, upper


def _default_context(model: dict) -> int:
    lower, upper = _context_bounds(model)
    return max(lower, min(DEFAULT_CONTEXT, upper))


def _validate_context(value, model: dict) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        value = _default_context(model)
    size = _as_int(value)
    if size is None:
        raise ValueError("context_size deve ser um número inteiro de tokens")
    if size <= 0:
        raise ValueError("context_size deve ser positivo")
    lower, upper = _context_bounds(model)
    if size < lower or size > upper:
        raise ValueError(
            f"context_size {size} fora da janela de {model['value']} "
            f"({lower}..{upper})")
    return size


def _reasoning(model: dict, value=None) -> str:
    efforts = model.get("efforts") or list(REASONING_LEVELS)
    wanted = _text(value or model.get("defaultEffort")).casefold()
    if not wanted:
        wanted = "medium" if "medium" in efforts else efforts[0]
    if wanted not in efforts:
        raise ValueError(
            f"reasoning {wanted!r} não suportado por {model['value']} "
            f"(use: {', '.join(efforts)})")
    return wanted


# ── store ───────────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"version": 1, "personas": {}, "retired": {}}


def _clean_aliases(value) -> list[str]:
    """Unique, non-empty names, case-insensitive, capped at MAX_ALIASES."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = _text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= MAX_ALIASES:
            break
    return out


def _clean_history(value) -> list[dict]:
    """Rename/retirement entries; malformed entries are dropped, not raised."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        entry = {
            "name": name,
            "at": _text(item.get("at")),
            "status": _text(item.get("status")) or "renamed",
        }
        to = _text(item.get("to"))
        if to:
            entry["to"] = to
        out.append(entry)
    return out


def _clean_persona(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid = _slugify(raw.get("id") or raw.get("name"))
    if not pid:
        return None
    persona = dict(raw)
    persona["id"] = pid
    persona["aliases"] = _clean_aliases(raw.get("aliases"))
    persona["history"] = _clean_history(raw.get("history"))
    return persona


def _normalize_retired_status(value) -> str:
    status = _text(value).casefold() or "left"
    if status not in RETIRED_STATUSES:
        raise ValueError(
            "status de aposentadoria inválido; use left, retired, fired ou "
            "renamed")
    return status


def _clean_retired(raw) -> dict | None:
    persona = _clean_persona(raw)
    if persona is None:
        return None
    persona["retired_at"] = _text(persona.get("retired_at"))
    status = _text(persona.get("retired_status")).casefold()
    persona["retired_status"] = (
        status if status in RETIRED_STATUSES else "left")
    if persona.get("successor") is not None:
        persona["successor"] = _text(persona.get("successor"))
    return persona


def _read_store() -> dict:
    path = Path(PERSONAS_PATH)
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    store = _empty()
    personas = data.get("personas")
    if isinstance(personas, dict):
        for key, raw in personas.items():
            persona = _clean_persona(raw)
            if persona:
                store["personas"][str(persona["id"])] = persona
    retired = data.get("retired")
    if isinstance(retired, dict):
        for key, raw in retired.items():
            persona = _clean_retired(raw)
            if persona:
                store["retired"][str(persona["id"])] = persona
    return store


def _write_store(data: dict) -> None:
    personas = data.get("personas") if isinstance(data, dict) else None
    retired = data.get("retired") if isinstance(data, dict) else None
    payload = {
        "version": 1,
        "personas": personas if isinstance(personas, dict) else {},
        "retired": retired if isinstance(retired, dict) else {},
    }
    path = Path(PERSONAS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load() -> dict:
    """The persona store; malformed slices are dropped, never raised."""
    with _LOCK:
        return _read_store()


def save(data: dict) -> None:
    """Atomically persist the store (tmp + os.replace) under the lock."""
    with _LOCK:
        _write_store(data)


def list_personas() -> list[dict]:
    """Active personas only, ordered by name, with the id as tiebreaker."""
    with _LOCK:
        items = [dict(p) for p in _read_store()["personas"].values()]
    items.sort(key=lambda p: (str(p.get("name") or "").casefold(),
                              str(p.get("id") or "")))
    return items


def _match(personas: dict, id_or_name) -> tuple[dict | None, str]:
    """(persona copy, former alias) matching id, current name, then alias."""
    wanted = _text(id_or_name)
    if not wanted:
        return None, ""
    key = wanted.casefold()
    slug = _slugify(wanted)
    if key in personas:
        return dict(personas[key]), ""
    if slug in personas:
        return dict(personas[slug]), ""
    for persona in personas.values():
        if _text(persona.get("name")).casefold() == key:
            return dict(persona), ""
    for persona in personas.values():
        for alias in persona.get("aliases") or []:
            if _text(alias).casefold() == key:
                return dict(persona), _text(alias)
    return None, ""


def get(id_or_name):
    """Active persona by id, name or alias (case-insensitive); None if absent."""
    with _LOCK:
        persona, _ = _match(_read_store()["personas"], id_or_name)
    return persona


def resolve_name(id_or_name) -> dict:
    """Resolve a current, old or retired name without ever guessing.

    Active personas win over retired ones. The ``former_name`` names the alias
    that matched, so the main assistant can acknowledge "you mean Fulana,
    who is now Fulana Front"; retired records carry their successor when the
    owner appointed one.
    """
    wanted = _text(id_or_name)
    if not wanted:
        return {"status": "unknown", "name": wanted}
    with _LOCK:
        data = _read_store()
        persona, former = _match(data["personas"], wanted)
        if persona is not None:
            if not former and (
                    _text(persona.get("name")).casefold() != wanted.casefold()):
                # The id/slug matched first, but the owner spelled an old
                # name: report it so the assistant can acknowledge the rename.
                for alias in persona.get("aliases") or []:
                    if _text(alias).casefold() == wanted.casefold():
                        former = _text(alias)
                        break
            return {
                "status": "active",
                "persona": persona,
                "former_name": former,
            }
        persona, _ = _match(data["retired"], wanted)
        if persona is not None:
            successor, _ = _match(
                data["personas"], persona.get("successor"))
            return {
                "status": "retired",
                "persona": persona,
                "successor": successor,
            }
    return {"status": "unknown", "name": wanted}


def history(id_or_name) -> dict | None:
    """Aliases, rename/retirement entries and successor for any known name."""
    resolved = resolve_name(id_or_name)
    if resolved["status"] == "unknown":
        return None
    persona = resolved["persona"]
    retired = resolved["status"] == "retired"
    return {
        "persona": persona,
        "aliases": list(persona.get("aliases") or []),
        "history": [dict(entry) for entry in persona.get("history") or []],
        "retired_at": persona.get("retired_at") if retired else None,
        "retired_status": persona.get("retired_status") if retired else None,
        "successor": resolved.get("successor") if retired else None,
    }


def retire(id_or_name, status: str = "left", successor=None) -> dict | None:
    """Move an active persona to ``retired``; None when the name is unknown.

    The record keeps aliases, history, personality, voice and gender. The
    optional successor (id or name) is resolved to the active persona's id so
    later lookups can hand the owner over to whoever took the role.
    """
    wanted = _text(id_or_name)
    if not wanted:
        return None
    retired_status = _normalize_retired_status(status)
    with _LOCK:
        data = _read_store()
        persona, _ = _match(data["personas"], wanted)
        if persona is None:
            return None
        target = str(persona["id"])
        successor_ref = ""
        if successor is not None:
            chosen, _ = _match(data["personas"], successor)
            if chosen is not None:
                successor_ref = str(chosen["id"])
            else:
                successor_ref = _text(successor)
        record = dict(persona)
        record["retired_at"] = _now()
        record["retired_status"] = retired_status
        if successor_ref:
            record["successor"] = successor_ref
        else:
            record.pop("successor", None)
        data["personas"].pop(target, None)
        data["retired"][target] = record
        _write_store(data)
        return dict(record)


def delete(id_or_name, retired_status: str = "left") -> bool:
    """Retire a persona (status ``left`` by default); False when absent."""
    return retire(id_or_name, status=retired_status) is not None


# ── upsert ──────────────────────────────────────────────────────────────────

_FIELDS = ("name", "gender", "voice_id", "personality", "skills", "stacks",
           "model", "reasoning", "context_size", "whatsapp_account")


def _merge_fields(base: dict, fields: dict) -> dict:
    merged = dict(base)
    for key in _FIELDS:
        if key in fields and fields[key] is not None:
            merged[key] = fields[key]
    return merged


def upsert(fields: dict, confirm_paid: bool = False,
           max_price_factor: float = 1.0) -> dict:
    """Create or update a persona; raises ValueError with a spoken reason."""
    if not isinstance(fields, dict):
        raise ValueError("campos da persona inválidos")

    wanted_id = _text(fields.get("id"))
    wanted_name = _text(fields.get("name"))
    target_id = _slugify(wanted_id or wanted_name)
    if not target_id or not _SLUG_RE.match(target_id):
        raise ValueError("nome ou id da persona é obrigatório")

    with _LOCK:
        data = _read_store()
        existing = data["personas"].get(target_id)
        if existing is None and len(data["personas"]) >= MAX_PERSONAS:
            raise ValueError(
                f"limite de {MAX_PERSONAS} personas atingido; remova uma antes")

        merged = _merge_fields(existing or {}, fields)
        name = _text(merged.get("name"))
        if not name:
            raise ValueError("nome da persona é obrigatório")
        if len(name) > 80:
            raise ValueError("nome da persona acima de 80 caracteres")

        gender = _normalize_gender(merged.get("gender"))

        voice_id = _text(merged.get("voice_id"))
        if not voice_id:
            raise ValueError("voice_id do Fish Audio é obrigatório")
        if len(voice_id) > 200 or _contains_secret(voice_id):
            raise ValueError("voice_id inválido")

        personality = _validate_personality(merged.get("personality"))
        skills = _normalize_skills(merged.get("skills"))
        stacks = _projects.normalize_stacks(merged.get("stacks") or [])

        model_value = _text(merged.get("model")) or DEFAULT_MODEL
        model = find_model(model_value)
        if model is None:
            options = ", ".join(m["value"] for m in model_catalog())
            raise ValueError(
                f"modelo {model_value!r} não está no catálogo "
                f"(disponíveis: {options})")
        factor = _as_float(model.get("priceFactor"), 0.0)
        if factor > 0:
            multiplier = format_multiplier(factor)
            if not confirm_paid:
                raise ValueError(
                    f"modelo {model['displayName']} é pago ({multiplier} por "
                    f"uso); confirme antes de salvar")
            limit = _as_float(max_price_factor, 1.0)
            if factor > limit:
                raise ValueError(
                    f"modelo {model['displayName']} custa {multiplier}, acima "
                    f"do limite {format_multiplier(limit)}")

        reasoning = _reasoning(model, merged.get("reasoning"))
        context_size = _validate_context(merged.get("context_size"), model)

        wa_account = _text(merged.get("whatsapp_account"))
        if wa_account:
            try:
                from core import whatsapp as _wa
                names = [str(n) for n in _wa.account_names()]
                match = next((n for n in names
                              if n.casefold() == wa_account.casefold()), None)
            except Exception:
                names, match = [], None
            if match is None:
                available = ", ".join(names) or "nenhuma conta pareada"
                raise ValueError(
                    f"WhatsApp {wa_account!r} não está pareado "
                    f"(disponíveis: {available})")
            wa_account = match

        now = _now()
        old_name = _text((existing or {}).get("name"))
        aliases = _clean_aliases((existing or {}).get("aliases"))
        entries = _clean_history((existing or {}).get("history"))
        if existing is not None and old_name and (
                old_name.casefold() != name.casefold()):
            # The old name is never lost: it becomes an alias (used by
            # resolve_name) and a history entry pointing at the new name.
            if not any(_text(alias).casefold() == old_name.casefold()
                       for alias in aliases):
                aliases.append(old_name)
                del aliases[:-MAX_ALIASES]
            entries.append({
                "name": old_name,
                "at": now,
                "status": "renamed",
                "to": name,
            })
        persona = {
            "id": target_id,
            "name": name,
            "gender": gender,
            "voice_id": voice_id,
            "personality": personality,
            "skills": skills,
            "stacks": stacks,
            "model": model["displayName"],
            "model_price_factor": factor,
            "reasoning": reasoning,
            "context_size": context_size,
            "whatsapp_account": wa_account,
            "aliases": aliases,
            "history": entries,
            "created_at": _text((existing or {}).get("created_at")) or now,
            "updated_at": now,
        }
        data["personas"][target_id] = persona
        _write_store(data)
        return dict(persona)


# ── addressing ──────────────────────────────────────────────────────────────

def address(persona, contraction: bool = False) -> str:
    """'a Fulana'/'o Fulano'/'Fulana'; with contraction 'pra'/'pro'/'para'."""
    if not isinstance(persona, dict):
        return ""
    name = _text(persona.get("name") or persona.get("id"))
    if not name:
        return ""
    gender = _text(persona.get("gender")).casefold()
    if gender == "f":
        return f"pra {name}" if contraction else f"a {name}"
    if gender == "m":
        return f"pro {name}" if contraction else f"o {name}"
    return f"para {name}" if contraction else name
