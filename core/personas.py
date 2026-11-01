"""Personas: named specialist assistants the owner creates by voice.

WHY THIS EXISTS
    The main assistant stays the only chat with the owner; personas are
    specialists it moderates ("Vou pedir pra Fulana ver"). Each one has its own
    name, grammatical gender, Fish Audio voice, personality, skills, project
    stacks, model, reasoning effort and context window.

STORAGE
    memory/personas.json (local, git-ignored), written atomically::

        {"version": 1, "personas": {"fulana-front": {...}}}

    A malformed or missing store never raises: ``load()`` returns an empty
    board, and the first ``save()`` repairs it. Nothing outside that file is
    ever written.

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
DEFAULT_MODEL = "Qwen3.8-Flash"
REASONING_LEVELS = ("low", "medium", "high", "max")
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
    return {"version": 1, "personas": {}}


def _clean_persona(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid = _slugify(raw.get("id") or raw.get("name"))
    if not pid:
        return None
    persona = dict(raw)
    persona["id"] = pid
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
    personas = data.get("personas")
    if not isinstance(personas, dict):
        return _empty()
    clean = {}
    for key, raw in personas.items():
        persona = _clean_persona(raw)
        if persona:
            clean[str(persona["id"])] = persona
    return {"version": 1, "personas": clean}


def _write_store(data: dict) -> None:
    personas = data.get("personas") if isinstance(data, dict) else None
    payload = {"version": 1,
               "personas": personas if isinstance(personas, dict) else {}}
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
    """The persona store; a missing or malformed file yields an empty board."""
    with _LOCK:
        return _read_store()


def save(data: dict) -> None:
    """Atomically persist the store (tmp + os.replace) under the lock."""
    with _LOCK:
        _write_store(data)


def list_personas() -> list[dict]:
    """All personas, ordered by name, with the id as the tiebreaker."""
    with _LOCK:
        items = [dict(p) for p in _read_store()["personas"].values()]
    items.sort(key=lambda p: (str(p.get("name") or "").casefold(),
                              str(p.get("id") or "")))
    return items


def get(id_or_name):
    """Persona by id (slug) or name, case-insensitive; None when absent."""
    wanted = _text(id_or_name).casefold()
    if not wanted:
        return None
    slug = _slugify(wanted)
    with _LOCK:
        personas = _read_store()["personas"]
        if wanted in personas:
            return dict(personas[wanted])
        for persona in personas.values():
            if str(persona.get("name") or "").casefold() == wanted:
                return dict(persona)
        if slug in personas:
            return dict(personas[slug])
    return None


def delete(id_or_name) -> bool:
    """Remove a persona by id or name; False when there was nothing to remove."""
    wanted = _text(id_or_name)
    if not wanted:
        return False
    slug = _slugify(wanted)
    with _LOCK:
        data = _read_store()
        keys = list(data["personas"].keys())
        target = None
        if wanted.casefold() in keys:
            target = wanted.casefold()
        elif slug in keys:
            target = slug
        else:
            for key, persona in data["personas"].items():
                if str(persona.get("name") or "").casefold() == wanted.casefold():
                    target = key
                    break
        if target is None:
            return False
        data["personas"].pop(target, None)
        _write_store(data)
        return True


# ── upsert ──────────────────────────────────────────────────────────────────

_FIELDS = ("name", "gender", "voice_id", "personality", "skills", "stacks",
           "model", "reasoning", "context_size")


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

        now = _now()
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
