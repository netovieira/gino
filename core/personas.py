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

MANDATORY SKILL
    The owner's rule (02/11/2026): every persona carries ``caveman`` (terse
    communication), except the main assistant. The main assistant prompt is
    built elsewhere (qoder_live: RUNTIME_SKILLS); this store only enforces the
    rule on persona records, never injects anything into the assistant.

READ SCOPE
    ``allowed_dirs`` holds absolute read roots for one persona. It is empty by
    default at all times: empty means the persona reads everywhere, exactly
    like every record written before this field existed. A filled list (the UI
    and the voice tool accept a comma-separated string) restricts Read, Glob
    and Grep to those roots and their subdirectories. The main assistant has
    no such field and is never limited. The field never touches the gates for
    writing, deleting, sending messages or power actions.

PAID MODELS
    The live catalog (memory/models.json) carries Fish/Qoder price factors.
    A persona may use a paid model only with an explicit ``confirm_paid=True``
    and only while its factor is within ``max_price_factor``. The factor is
    snapshotted into the persona for display; the catalog stays the truth on
    the next edit.

DIRECT WHATSAPP
    The owner's rule (09/11/2026): a persona may exist without a WhatsApp
    number of her own. ``direct_whatsapp`` is the boolean that grants her one.

      * **empty/undeclared = False** — no number. The persona still answers by
        name whenever the owner calls her in the assistant's own chat, but
        there is no number to talk to her directly, she never receives a
        message through an account, and no message is ever routed to her
        because it landed on an account.
      * an account is only hers with BOTH ``direct_whatsapp=True`` AND a
        non-empty ``whatsapp_account``. Declaring the account explicitly in an
        upsert implies the number (that is how a brand-new persona the
        assistant defines with a WhatsApp gets one without saying twice).
      * ``direct_whatsapp=False`` clears ``whatsapp_account`` in the same
        save, so "remove o whats da Aline" is one call and leaves no stale
        account behind.
      * empty/undeclared ``whatsapp_account`` means NONE, never the main
        account (owner order 07/11/2026). Whoever wants the main account must
        say "principal" in full; it resolves to the live paired account and is
        never a hardcoded number.

    The main assistant is not a route in this store: his direct line is the
    paired default account, read live (``assistant_account()``), and is always
    on (``MAIN_ASSISTANT_DIRECT_WHATSAPP``). He does have a record — the
    orchestrator himself (``MAIN_PERSONA_ID``, cargo ``Orquestrador``) — and
    that record never carries a WhatsApp of its own.

CARGOS
    Owner order (09/11/2026): every persona has a ``role`` (cargo), and the
    assistant's own record is the orchestrator. The role is what says which
    subject belongs to whom, so a task that matches a persona is delegated
    (:func:`core.persona_ops.delegation.plan`) instead of being done by the
    orchestrator. ``DEFAULT_ROLES`` keeps the owner's founding roster honest
    even in records written before the field existed.

MODEL CASCADE
    ``MODEL_CASCADE`` is the owner's quality order, best first. Nothing is
    used without checking the live catalog: a model counts as free only with
    price factor 0.0, and a model reported as at its usage limit is skipped
    for :data:`EXHAUSTION_TTL_S`. :func:`model_for` answers "which model does
    this persona run right now"; when nothing free is available it answers
    None rather than spending credits. Aisha, Bernardo and Ariel start in the
    cascade (``CASCADE_PERSONAS``).

VOICE IDS
    A ``voice_id`` is confirmed against Fish Audio before it is stored
    (:func:`validate_voice_id`); an id the service does not know blocks the
    registration and is named in the error. The API key is only ever sent in
    a request header — never printed, never returned in a message.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from core import projects as _projects

BASE_DIR = Path(__file__).resolve().parent.parent
PERSONAS_PATH = BASE_DIR / "memory" / "personas.json"
MODELS_PATH = BASE_DIR / "memory" / "models.json"
SKILLS_DIR = BASE_DIR / "skills"

MAX_PERSONALITY = 2000
MAX_ALIASES = 20
MAX_ALLOWED_DIRS = 32
DEFAULT_MODEL = "Qwen3.8-Flash"
REASONING_LEVELS = ("low", "medium", "high", "max")
# The form shows these labels; the value is what the catalog and the store use.
# ``xhigh`` is the qfmodel/qmodel ceiling and is shown as "Max", the name the
# owner sees — never store the label itself.
REASONING_LABELS = {
    "low": "Baixo", "medium": "Médio", "high": "Alto",
    "max": "Max", "xhigh": "Max",
}
# Owner rule: all personas (never the main assistant) speak caveman.
PERSONA_REQUIRED_SKILLS = ("caveman",)
RETIRED_STATUSES = ("left", "retired", "fired", "renamed")
CONTEXT_MIN = 16_000
CONTEXT_MAX = 1_000_000
DEFAULT_CONTEXT = 200_000

# ── cargos (ordem do dono, 09/11/2026) ──────────────────────────────────────
#
# Toda persona tem um CARGO: é o que diz ao orquestrador para que assunto ela
# existe. O Gino é o dono do cargo de ``Orquestrador`` e não executa assunto que
# tenha persona — delega (``persona_ask``).
MAX_ROLE = 120
ORCHESTRATOR_ROLE = "Orquestrador"
MAIN_PERSONA_ID = "gino"
MAIN_PERSONA_NAME = "Gino"
# Cargo de origem do quadro do dono. Um registro sem ``role`` ainda tem cargo:
# ``resolve_role`` cai nesta tabela antes de dizer "sem cargo".
DEFAULT_ROLES = {
    "gino": ORCHESTRATOR_ROLE,
    "aisha": "Analista de Monitoramento e Segurança",
    "bernardo": "Engenheiro de Arquitetura",
    "ariel": "Designer de Interface",
}

# ── cascata de modelos (ordem do dono, 09/11/2026) ──────────────────────────
#
# "Defina o melhor sem custo algum": a lista abaixo é a ordem de QUALIDADE, do
# melhor para o pior. Antes de usar um nome a cascata olha o catálogo vivo
# (``memory/models.json``): multiplicador 0.0 é gratuito; qualquer coisa acima
# disso, ou preço desconhecido, não é. Nome esgotado (limite de uso atingido)
# também sai da frente. Nunca se gasta crédito por engano: se nada na cascata
# está livre, a cascata devolve None e quem chama decide — ela não inventa
# modelo pago.
MODEL_CASCADE = (
    "Qwen3.8-Max", "Kimi-K3.1-Pro", "DeepSeek-V4-Pro", "GLM-5.3",
    "MiniMax-M3.1-Flash", "Kimi-K2.8-Preview", "Qwen3.8-Flash",
    "GLM-5.3-Flash", "DeepSeek-Flash", "Efficient", "Auto",
)
# Personas que entram na cascata desde o cadastro (ordem do dono).
CASCADE_PERSONAS = ("aisha", "bernardo", "ariel")
# "Chegar ao limite de uso" é momentâneo: o nome fica de fora por este tempo e
# depois volta a concorrer, sem reiniciar nada.
EXHAUSTION_TTL_S = 900.0

# ── Fish Audio ───────────────────────────────────────────────────────────────
# Voz é cadastrada, não adivinhada: o ``voice_id`` é confirmado no serviço
# ANTES de gravar. A chave nunca aparece em mensagem de erro, log ou print.
FISH_API_BASE = "https://api.fish.audio"
FISH_TIMEOUT = 15

_GENDERS = {"f", "m", "n"}
# Owner rule (09/11/2026): the main assistant always keeps his direct
# WhatsApp. He is not a record in this store, so the rule lives in code and
# his number is whatever account is paired as the default (never hardcoded).
MAIN_ASSISTANT_DIRECT_WHATSAPP = True
# "principal" por extenso é o único jeito de pedir a conta principal; vazio
# nunca é a principal, vazio é nenhuma (ordem do dono, 07/11/2026).
MAIN_ACCOUNT_WORDS = ("principal", "conta principal", "a principal",
                      "main", "padrao", "padrão")
_DIRECT_TRUE = ("1", "true", "sim", "s", "yes", "y", "verdadeiro", "direto",
                "com whatsapp", "com número", "com numero")
_DIRECT_FALSE = ("0", "false", "nao", "não", "n", "no", "falso", "sem",
                 "sem whatsapp", "sem número", "sem numero")

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


def _normalize_direct_whatsapp(value, *, default: bool = False) -> bool:
    """direct_whatsapp como booleano honesto: vazio/não-declarado = False.

    Nothing here ever guesses a number into existence: an unrecognised word is
    refused with a spoken reason instead of falling back to True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    key = _text(value).casefold()
    if not key:
        return default
    if key in _DIRECT_TRUE:
        return True
    if key in _DIRECT_FALSE:
        return False
    raise ValueError("direct_whatsapp inválido: use sim ou não")


def direct_whatsapp(persona) -> bool:
    """True só quando o cadastro declara o número direto; vazio = False."""
    if not isinstance(persona, dict):
        return False
    return _normalize_direct_whatsapp(persona.get("direct_whatsapp"))


def own_account(persona) -> str:
    """A conta de WhatsApp da persona; '' quando ela não tem número.

    Ponto único do "ela tem um número?": exige ``direct_whatsapp=True`` e
    ``whatsapp_account`` preenchido. Quem só tem a conta no cadastro (registro
    antigo) ou só o booleano fica sem conta — nunca se empresta a conta do
    assistente.
    """
    if not direct_whatsapp(persona):
        return ""
    return _text(persona.get("whatsapp_account"))


def assistant_account() -> str:
    """A conta direta do assistente principal, lida viva ('' se nada pareado).

    O Gino não é persona: o número dele é a conta pareada como principal.
    Nenhum número é hardcoded aqui.
    """
    return _main_account_name()


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


def _validate_role(value) -> str:
    """Cargo falável: uma linha, sem segredo. Vazio é permitido."""
    text = str(value if value is not None else "").strip()
    if len(text) > MAX_ROLE:
        raise ValueError(
            f"cargo acima de {MAX_ROLE} caracteres ({len(text)})")
    if _contains_secret(text):
        raise ValueError("cargo não pode conter segredos (api_key, token...)")
    return text


def _clean_role(value) -> str:
    """Non-raising variant for records loaded from disk."""
    try:
        return _validate_role(value)
    except ValueError:
        return ""


def resolve_role(persona) -> str:
    """O cargo de uma persona: o cadastro vence; depois a tabela do dono.

    Aceita o registro, o id ou o nome. Quem não existe devolve '' — cargo
    nunca é inventado.
    """
    if isinstance(persona, (str, int)):
        record = get(persona) or {"name": str(persona)}
    elif isinstance(persona, dict):
        record = persona
    else:
        return ""
    role = _clean_role(record.get("role"))
    if role:
        return role
    for key in (_slugify(record.get("id")), _slugify(record.get("name"))):
        if key and key in DEFAULT_ROLES:
            return DEFAULT_ROLES[key]
    return ""


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


def _normalize_allowed_dirs(value) -> list[str]:
    """Read roots for one persona; an empty list means "reads everywhere".

    The form and the voice tool send a comma-separated string (the UI may also
    hand a list). ``~``/env vars are expanded and relative entries resolve
    against the project root, so the stored roots never depend on the process
    cwd. Only LEITURA uses this field: write/send/power gates are untouched.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError("diretórios permitidos inválidos")

    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        expanded = os.path.expanduser(os.path.expandvars(text))
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(BASE_DIR), expanded)
        normalized = os.path.normpath(expanded)
        if normalized not in out:
            out.append(normalized)
    if len(out) > MAX_ALLOWED_DIRS:
        raise ValueError(
            f"diretórios permitidos acima de {MAX_ALLOWED_DIRS} entradas")
    return out


def _clean_allowed_dirs(value) -> list[str]:
    """Non-raising variant for records loaded from disk (never breaks load)."""
    try:
        return _normalize_allowed_dirs(value)
    except (TypeError, ValueError):
        return []


def _ensure_required_skills(skills: list[str]) -> list[str]:
    """Append every PERSONA_REQUIRED_SKILLS entry, preserving the given order."""
    out = list(skills)
    known = {name.casefold(): name for name in _available_skills()}
    for wanted in PERSONA_REQUIRED_SKILLS:
        canonical = known.get(wanted.casefold())
        if canonical is None:
            raise ValueError(
                f"skill obrigatória {wanted!r} ausente em skills/")
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
            "priceFactorKnown": True,
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
    known = raw.get("priceFactorKnown", raw.get("price_factor_known"))
    if known is None:
        known = raw.get("priceFactor", raw.get("price_factor")) is not None
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
        "priceFactorKnown": bool(known),
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


# ── cascata de modelos: melhor → pior, grátis sempre ────────────────────────

_EXHAUSTED: dict[str, float] = {}
_EXHAUSTION_WHY: dict[str, str] = {}


def _cascade_rank() -> dict[str, int]:
    """Nome (value ou displayName, em minúsculas) → posição na cascata."""
    rank: dict[str, int] = {}
    for index, name in enumerate(MODEL_CASCADE):
        rank[_slugify(name)] = index
        rank.setdefault(name.casefold(), index)
    return rank


def _rank_of(entry: dict) -> int:
    rank = _cascade_rank()
    for key in (entry.get("value"), entry.get("displayName")):
        text = _text(key)
        hit = rank.get(_slugify(text)) or rank.get(text.casefold())
        if hit is not None:
            return hit
    # Fora da lista do dono: entra no fim, na ordem do próprio catálogo.
    return len(MODEL_CASCADE)


def cascade_models() -> list[dict]:
    """O catálogo inteiro, ordenado do melhor para o pior.

    Não filtra nada: é a ordem que o dono definiu, servida para a UI e para o
    relatório da cascata mostrarem também quem está caro ou esgotado.
    """
    out = list(model_catalog())
    out.sort(key=lambda entry: (_rank_of(entry),
                                _text(entry.get("displayName")).casefold()))
    return out


def model_is_free(entry) -> bool:
    """Grátis é multiplicador 0.0 no catálogo vivo. Preço desconhecido não é.

    ``isFree`` do catálogo sozinho não basta: um modelo em promoção continua
    cobrando quando o multiplicador sobe, e a ordem do dono é não gastar.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("priceFactorKnown") is False:
        return False
    return _as_float(entry.get("priceFactor"), 1.0) <= 0.0


def _entry_name(model) -> str:
    """Nome falável de um modelo: aceita o registro do catálogo ou o nome."""
    if isinstance(model, dict):
        return _text(model.get("displayName") or model.get("value"))
    return _text(model)


def mark_model_exhausted(model, reason: str = "",
                         ttl_s: float = EXHAUSTION_TTL_S) -> None:
    """Registra "limite de uso atingido" de um modelo por um tempo."""
    name = _text(_entry_name(model)).casefold()
    if not name:
        return
    seconds = _as_float(ttl_s, EXHAUSTION_TTL_S)
    if seconds <= 0:
        seconds = EXHAUSTION_TTL_S
    with _LOCK:
        _EXHAUSTED[name] = time.monotonic() + seconds
        _EXHAUSTION_WHY[name] = _text(reason) or "limite de uso"


def model_exhausted(model) -> bool:
    """True enquanto o modelo está com o limite de uso estourado."""
    name = _text(_entry_name(model)).casefold()
    if not name:
        return False
    with _LOCK:
        until = _EXHAUSTED.get(name)
        if until is None:
            return False
        if time.monotonic() >= until:
            _EXHAUSTED.pop(name, None)
            _EXHAUSTION_WHY.pop(name, None)
            return False
        return True


def exhaustion_reason(model) -> str:
    name = _text(_entry_name(model)).casefold()
    with _LOCK:
        return _text(_EXHAUSTION_WHY.get(name))


def clear_model_exhaustion(model=None) -> None:
    """Esquece esgotamentos (todos, ou só o de ``model``). Também em teste."""
    with _LOCK:
        if model is None:
            _EXHAUSTED.clear()
            _EXHAUSTION_WHY.clear()
            return
        name = _text(_entry_name(model)).casefold()
        _EXHAUSTED.pop(name, None)
        _EXHAUSTION_WHY.pop(name, None)


def cascade_available(preferred=None, exclude=()) -> list[dict]:
    """Da melhor para a pior: só o que está grátis e dentro do limite."""
    blocked = {_text(name).casefold() for name in exclude if _text(name)}
    out: list[dict] = []
    for entry in cascade_models():
        names = {_text(entry.get("displayName")).casefold(),
                 _text(entry.get("value")).casefold()}
        if names & blocked:
            continue
        if not model_is_free(entry) or model_exhausted(entry):
            continue
        out.append(entry)
    preferred_name = _text(preferred).casefold()
    if preferred_name:
        # A preferência do cadastro abre a fila quando ela mesma é aproveitável.
        head = [entry for entry in out if preferred_name in (
            _text(entry.get("displayName")).casefold(),
            _text(entry.get("value")).casefold())]
        if head:
            rest = [entry for entry in out
                    if _text(entry.get("displayName")).casefold()
                    != preferred_name]
            out = head + rest
    return out


def next_free_model(preferred=None, exclude=()) -> dict | None:
    """Primeiro modelo aproveitável da cascata; None quando nada é grátis."""
    usable = cascade_available(preferred=preferred, exclude=exclude)
    return usable[0] if usable else None


def in_cascade(persona) -> bool:
    """Esta persona usa a cascata (ordem do dono: Aisha, Bernardo, Ariel)."""
    if not isinstance(persona, dict):
        persona = get(persona) or {}
    slug = _slugify(persona.get("id") or persona.get("name"))
    return bool(slug) and slug in CASCADE_PERSONAS


def model_for(persona=None, *, exclude=()) -> dict:
    """O modelo que a persona vai usar AGORA, depois de checar a cascata.

    Devolve ``{"model", "previous", "switched", "reason"}``. Uma persona da
    cascata começa na cascata: o modelo do cadastro é a ponta da fila quando é
    gratuito e está disponível, e vira o próximo aproveitável quando não for.
    Quem está fora da cascata mantém o cadastro — a cascata não troca nome de
    persona sem ela ter pedido.
    """
    record = persona if isinstance(persona, dict) else (
        get(persona) or {} if persona else {})
    wanted = _text(record.get("model")) if record else ""
    entry = find_model(wanted) if wanted else None
    current = _entry_name(entry) if entry else ""
    if record and not in_cascade(record):
        usable = entry if (entry and model_is_free(entry)
                           and not model_exhausted(entry)) else None
        return {
            "model": usable or entry,
            "previous": None,
            "switched": False,
            "reason": "fora da cascata: cadastro respeitado" if usable else
                      "fora da cascata",
        }
    usable = next_free_model(preferred=current or None, exclude=exclude)
    if usable is None:
        return {
            "model": None,
            "previous": current or None,
            "switched": False,
            "reason": "nenhum modelo gratuito disponível na cascata",
        }
    chosen = _entry_name(usable)
    if current and chosen.casefold() != current.casefold():
        why = (f"{current} não está gratuito"
               if entry and not model_is_free(entry)
               else f"{current} com o limite de uso atingido")
        return {"model": usable, "previous": current, "switched": True,
                "reason": f"{why}; cascata escolheu {chosen}"}
    return {"model": usable, "previous": None, "switched": False,
            "reason": f"cascata: {chosen} está gratuito e disponível"}


def cascade_report(preferred=None) -> list[dict]:
    """A cascata como o dono quer ver: cada nome, seu preço e seu estado."""
    out = []
    for entry in cascade_models():
        if not model_is_free(entry):
            state = "pago"
        elif model_exhausted(entry):
            state = f"esgotado ({exhaustion_reason(entry)})"
        else:
            state = "livre"
        row = {
            "model": _entry_name(entry),
            "value": _text(entry.get("value")),
            "multiplier": format_multiplier(entry.get("priceFactor")),
            "state": state,
        }
        out.append(row)
    wanted = _text(preferred).casefold()
    if wanted:
        for row in out:
            if wanted in (_text(row["model"]).casefold(),
                          _text(row["value"]).casefold()):
                row["preferred"] = True
    return out


# ── voz: validação do voice_id antes de gravar ──────────────────────────────

def _fish_api_key() -> str:
    """A chave do Fish Audio, lida do cérebro de economia. Nunca é impressa."""
    try:
        from memory import config_manager
        cfg = config_manager.get_plugin_config("brain")
    except Exception:
        return ""
    return _text(cfg.get("fish_api_key") if isinstance(cfg, dict) else "")


def fish_voice_lookup(voice_id: str, key: str = "",
                      timeout: float = FISH_TIMEOUT):
    """Confirma no Fish Audio se ``voice_id`` existe.

    ``(True, título)`` quando o serviço mostra a voz, ``(False, motivo)``
    quando ele diz que não há nada naquele id, ``(None, motivo)`` quando não
    deu para perguntar (sem chave, rede, chave recusada). Nenhuma resposta
    devolve a chave: ela só sai no cabeçalho da requisição.
    """
    vid = _text(voice_id)
    if not vid:
        return False, "voice_id vazio"
    api_key = key or _fish_api_key()
    if not api_key:
        return None, "sem chave do Fish Audio"
    try:
        import requests
        r = requests.get(f"{FISH_API_BASE}/model/{vid}",
                         headers={"Authorization": f"Bearer {api_key}"},
                         timeout=timeout)
    except Exception as exc:
        return None, f"Fish Audio inacessível ({type(exc).__name__})"
    status = getattr(r, "status_code", 0)
    if status == 200:
        title = ""
        try:
            title = _text(r.json().get("title"))
        except Exception:
            title = ""
        return True, title or vid
    if status == 404:
        return False, "não encontrada no Fish Audio (404)"
    if status == 401:
        return None, "chave do Fish Audio recusada (401)"
    if status == 403:
        return None, "voz fora do alcance desta chave (403)"
    if status in (429, 500, 502, 503, 504):
        return None, f"Fish Audio indisponível ({status})"
    return None, f"Fish Audio respondeu {status}"


def validate_voice_id(voice_id: str, *, key: str = "") -> str:
    """Confirma a voz antes de gravar; levanta ValueError nomeando o id.

    Devolve uma nota quando não deu para confirmar (sem chave/rede): o cadastro
    não para por defeito do serviço, mas a nota vai na resposta para o dono ver
    que aquela voz entrou sem checagem.
    """
    vid = _text(voice_id)
    if not vid:
        raise ValueError("voice_id do Fish Audio é obrigatório")
    exists, detail = fish_voice_lookup(vid, key=key)
    if exists is False:
        raise ValueError(
            f"voice_id {vid!r} {detail}; pegue outro id no site do Fish Audio")
    if exists is None:
        return f"voz {vid} não verificada: {detail}"
    return ""


def find_by_voice_id(voice_id) -> dict | None:
    """A persona dona daquela voz (pelo ``voice_id`` exato); None quando é a
    voz do Gino ou de ninguém. É o gancho do fallback de fala no brain."""
    wanted = _text(voice_id)
    if not wanted:
        return None
    for persona in list_personas():
        if _text(persona.get("voice_id")) == wanted:
            return persona
    return None


def _context_bounds(model: dict) -> tuple[int, int]:
    """Accepted context range, derived from the model's published windows.

    ``availableContextWindows`` is the catalog's own list of selectable context
    sizes (e.g. ``[200000, 400000, 1000000]``): the biggest one is the ceiling.
    ``maxInputTokens`` is only the per-request input cap and must never shrink
    that list; it is used solely when a model publishes no window at all.
    """
    windows = [int(w) for w in (model.get("availableContextWindows") or [])
               if int(w) > 0]
    if windows:
        lower, upper = max(1, min(windows)), max(windows)
    else:
        lower = CONTEXT_MIN
        upper = int(model.get("maxInputTokens") or CONTEXT_MAX)
    if upper < lower:
        lower = upper
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
            f"context_size {size} fora da janela de {model['displayName']} "
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


# ── form options (one source of truth for the UI) ────────────────────────────
#
# The persona form never keeps its own list of context sizes or effort names:
# it asks these helpers, so a model that publishes 1M offers 1M and a model
# whose efforts say ``xhigh`` offers "Max" with ``xhigh`` underneath.

def _model_of(value) -> dict | None:
    """Catalog entry from a dict, a model name/value, or None (default model)."""
    if isinstance(value, dict):
        return value or None
    return find_model(_text(value) or DEFAULT_MODEL)


def context_windows(model=None) -> list[int]:
    """Selectable context sizes: the model's published windows, ascending."""
    entry = _model_of(model)
    windows = [int(w) for w in
               ((entry or {}).get("availableContextWindows") or [])
               if _as_int(w)]
    if windows:
        return sorted(dict.fromkeys(windows))
    lower, upper = _context_bounds(entry) if entry else (CONTEXT_MIN, CONTEXT_MAX)
    return [upper] if upper >= lower else [lower]


def default_context(model=None) -> int:
    entry = _model_of(model)
    return _default_context(entry) if entry else DEFAULT_CONTEXT


def reasoning_choices(model=None) -> list[tuple[str, str]]:
    """(value, label) pairs the form offers, in the model's own order."""
    entry = _model_of(model)
    efforts = [str(effort) for effort in ((entry or {}).get("efforts") or [])]
    if not efforts:
        efforts = list(REASONING_LEVELS)
    return [(effort, REASONING_LABELS.get(effort, effort)) for effort in efforts]


def default_reasoning(model=None) -> str:
    entry = _model_of(model)
    if entry is None:
        return "medium"
    try:
        return _reasoning(entry)
    except ValueError:
        efforts = entry.get("efforts") or []
        return efforts[0] if efforts else "medium"


def _whatsapp_accounts() -> list[dict]:
    """Configured WhatsApp accounts as ``{name, uuid, ...}``; [] when absent."""
    try:
        from core import whatsapp as _wa
        return [dict(account) for account in _wa.list_accounts()]
    except Exception:
        return []


def _main_account_name() -> str:
    """The paired account the assistant himself uses, read live (never a number).

    The settings' ``default_account`` wins (``default_account_name``); the
    ``default`` flag published by the account list is the fallback for older
    configs. Empty when nothing is paired — no name is ever invented.
    """
    accounts = _whatsapp_accounts()
    try:
        from core import whatsapp as _wa
        chosen = _text(_wa.default_account_name())
    except Exception:
        chosen = ""
    if chosen:
        return chosen
    for account in accounts:
        if account.get("default"):
            name = _text(account.get("name"))
            if name:
                return name
    return ""


def whatsapp_account_choices() -> list[str]:
    """Pairing names the persona form offers (empty string = no number)."""
    return [_text(account.get("name")) for account in _whatsapp_accounts()
            if _text(account.get("name"))]


def _resolve_whatsapp_account(value: str) -> str:
    """Canonical account name for ``value`` (a name, an uuid or "principal").

    Owner rule (07/11/2026): **empty means none**. An empty or undeclared
    ``whatsapp_account`` no longer stands for the main account — the main
    account is only hers when the caller says "principal" in full, and that
    word resolves to the live paired default, never to a hardcoded number.
    """
    raw = _text(value)
    wanted = raw.casefold()
    if not wanted:
        return ""
    accounts = _whatsapp_accounts()
    for key in ("name", "uuid"):
        for account in accounts:
            if _text(account.get(key)).casefold() == wanted:
                name = _text(account.get("name"))
                if name:
                    return name
    if wanted in MAIN_ACCOUNT_WORDS:
        # "principal" dito por extenso, sem conta literalmente chamada assim:
        # vale a conta pareada como principal AGORA (lida do config).
        name = _main_account_name()
        if name:
            return name
        raise ValueError(
            f"WhatsApp {value!r} não está pareado (disponíveis: "
            f"{', '.join(whatsapp_account_choices()) or 'nenhuma conta pareada'})")
    available = ", ".join(whatsapp_account_choices()) or "nenhuma conta pareada"
    raise ValueError(
        f"WhatsApp {value!r} não está pareado (disponíveis: {available})")


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
    # Old records have no field at all: [] keeps the previous behaviour
    # (persona reads everywhere).
    persona["allowed_dirs"] = _clean_allowed_dirs(raw.get("allowed_dirs"))
    # direct_whatsapp is read on every load: an old record (field absent) has
    # no number, exactly like a persona whose account was removed — and an
    # account left behind by an old record can never be hers without the flag
    # (the store stays honest; nothing here upgrades a persona by guessing).
    persona["direct_whatsapp"] = _normalize_direct_whatsapp(
        raw.get("direct_whatsapp"))
    persona["whatsapp_account"] = own_account(persona)
    # Cargo: registro antigo sem o campo continua com o cargo de origem do
    # dono (DEFAULT_ROLES) quando for um dos nomes dele; o load nunca inventa
    # cargo de quem não está na tabela.
    role = _clean_role(raw.get("role"))
    if role:
        persona["role"] = role
    else:
        persona.pop("role", None)
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

_FIELDS = ("name", "gender", "voice_id", "role", "personality", "skills",
           "stacks", "model", "reasoning", "context_size", "whatsapp_account",
           "direct_whatsapp", "allowed_dirs")


def _merge_fields(base: dict, fields: dict) -> dict:
    merged = dict(base)
    for key in _FIELDS:
        if key in fields and fields[key] is not None:
            merged[key] = fields[key]
    return merged


def upsert(fields: dict, confirm_paid: bool = False,
           max_price_factor: float = 1.0, check_voice: bool = True) -> dict:
    """Create or update a persona; raises ValueError with a spoken reason.

    Quantas personas existem não tem teto (ordem do dono, 09/11/2026): o que é
    limitado é o que se pede ao mesmo tempo, em
    :mod:`core.persona_ops.limits`. Com ``check_voice`` o ``voice_id`` é
    confirmado no Fish Audio ANTES de gravar — id que não existe bloqueia o
    cadastro e é nomeado na mensagem; a chave nunca é ecoada.
    """
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

        # O dono quer cadastro, não aposta: a voz é conferida no serviço antes
        # de entrar no quadro. Repetir a checagem de uma voz já confirmada só
        # faria cada edição pagar uma ida à API.
        voice_check = ""
        already_ok = (_text((existing or {}).get("voice_check")) == "ok"
                      and _text((existing or {}).get("voice_id")) == voice_id)
        if check_voice and not already_ok:
            voice_check = validate_voice_id(voice_id) or "ok"
        elif already_ok:
            voice_check = "ok"

        role = _validate_role(merged.get("role")) or resolve_role(
            {"id": target_id, "name": name})

        personality = _validate_personality(merged.get("personality"))
        skills = _ensure_required_skills(
            _normalize_skills(merged.get("skills")))
        stacks = _projects.normalize_stacks(merged.get("stacks") or [])
        allowed_dirs = _normalize_allowed_dirs(merged.get("allowed_dirs"))

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

        # direct_whatsapp: o declarado manda. Sem declaração, quem acaba de
        # receber uma conta preenchida tem número (é assim que o assistente
        # define uma persona nova com whats numa frase só). Vazio ou
        # não-declarado é FALSO — sem número, nunca a conta principal.
        declared_account = bool(_text(fields.get("whatsapp_account")))
        direct = _normalize_direct_whatsapp(merged.get("direct_whatsapp"),
                                           default=declared_account)
        wa_account = _text(merged.get("whatsapp_account"))
        if wa_account:
            wa_account = _resolve_whatsapp_account(wa_account)
        if not direct:
            # Sem número não existe conta dela: apagar aqui é o "remova os
            # whats da Aline" num passo só, sem deixar conta fantasma no
            # cadastro para o roteamento do WhatsApp achar que é dela.
            wa_account = ""
        elif not wa_account:
            raise ValueError(
                "direct_whatsapp=true exige a conta dela preenchida; diga "
                "qual conta (e para a principal diga \"principal\" por "
                "extenso)")

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
            "voice_check": voice_check,
            "role": role,
            "personality": personality,
            "skills": skills,
            "stacks": stacks,
            "model": model["displayName"],
            "model_price_factor": factor,
            "reasoning": reasoning,
            "context_size": context_size,
            "whatsapp_account": wa_account,
            "direct_whatsapp": direct,
            "allowed_dirs": allowed_dirs,
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
