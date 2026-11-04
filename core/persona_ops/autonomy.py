"""Autonomia de persona como DADO declarativo, não como código.

A tabela fica em ``config/personas_autonomy.json`` e é lida pela mesma
:class:`~core.flow_ops.rules.RulesTable` que já roteia os fluxos. Assim a
precedência é a que o dono já conhece:

1. ``overrides`` — regras do dono, na ordem do arquivo, com precedência máxima.
2. ``rules`` — ordem de ``priority`` (maior primeiro) e, no empate, ordem do
   arquivo.

Cada regra carrega um bloco ``autonomy`` (chave extra que a ``RulesTable``
ignora). O ``match`` escolhe a persona pelo ``route`` (o slug), por ``any``
(nome/alias) ou por ``route: "*"`` (todas). Exemplo::

    {
      "id": "persona-aline",
      "priority": 10,
      "match": {"route": "aline"},
      "autonomy": {
        "tools": ["Read", "Glob", "Grep", "memory_read"],
        "read_roots": ["/projects/therotv"],
        "requires_confirm": [],
        "max_tokens": 12000,
        "session_ttl_s": 900
      }
    }

O bloco ``defaults.autonomy`` define o que valer quando a persona não declara a
chave. A tabela **não** executa ferramenta nenhuma: ela só responde
:class:`ToolDecision` para quem já é o dono do portão (o sidecar/mostrador
vivo). Negar é sempre mais seguro que permitir; ``deny_tools`` vence até
``confirm=True``.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.flow_ops.rules import RulesError, RulesTable, norm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUTONOMY_PATH = PROJECT_ROOT / "config" / "personas_autonomy.json"
ENV_AUTONOMY_PATH = "JARVIS_PERSONAS_AUTONOMY"

DEFAULT_TOOLS = (
    "read", "glob", "grep", "webfetch", "websearch",
    "memory_read", "memory_search", "memory_note",
)
READ_TOOLS = frozenset({"read", "glob", "grep"})
WRITE_TOOLS = frozenset({"edit", "write", "notebookedit", "bash", "agent",
                         "task", "computer", "terminal", "jarvis_call"})

MAX_TOKENS_CEILING = 2_000_000
MAX_TTL_S = 86_400.0

_TOOL_PREFIXES = ("mcp__persona__", "mcp__jarvis__")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,95}$")


class AutonomyError(ValueError):
    """Tabela de autonomia inválida (arquivo, campo ou ferramenta)."""

    def __init__(self, message: str, *, path: str = "", rule_id: str = ""):
        super().__init__(message)
        self.path = path
        self.rule_id = rule_id


def tool_key(name: Any) -> str:
    """Canonical tool name: lowercase, no MCP prefix, no separators."""
    key = norm(name)
    for prefix in _TOOL_PREFIXES:
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    key = key.replace("-", "_").replace(" ", "_")
    return key


def _as_tools(value: Any, *, field_name: str, rule_id: str = "") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise AutonomyError(
            f"regra {rule_id or '?'}: {field_name} deve ser lista ou texto",
            rule_id=rule_id)
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        key = tool_key(part)
        if not key:
            continue
        if not _SAFE_ID.match(key):
            raise AutonomyError(
                f"regra {rule_id or '?'}: ferramenta inválida {part!r}",
                rule_id=rule_id)
        if key not in out:
            out.append(key)
    return tuple(out)


def _as_roots(value: Any, *, rule_id: str = "") -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        raise AutonomyError(
            f"regra {rule_id or '?'}: read_roots deve ser lista ou texto",
            rule_id=rule_id)
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        expanded = os.path.expanduser(os.path.expandvars(text))
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(PROJECT_ROOT), expanded)
        normalized = os.path.normpath(os.path.abspath(expanded))
        if normalized not in out:
            out.append(normalized)
    return tuple(out)


def _as_int(value: Any, *, field_name: str, rule_id: str = "",
            default: int, minimum: int = 0,
            maximum: int = MAX_TOKENS_CEILING) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise AutonomyError(
            f"regra {rule_id or '?'}: {field_name} deve ser inteiro",
            rule_id=rule_id)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise AutonomyError(
            f"regra {rule_id or '?'}: {field_name}={value!r} não é inteiro",
            rule_id=rule_id) from None
    if not minimum <= number <= maximum:
        raise AutonomyError(
            f"regra {rule_id or '?'}: {field_name}={number} fora de "
            f"{minimum}..{maximum}", rule_id=rule_id)
    return number


def _as_ttl(value: Any, *, rule_id: str = "",
            default: float = 900.0) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise AutonomyError(
            f"regra {rule_id or '?'}: session_ttl_s deve ser número",
            rule_id=rule_id)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AutonomyError(
            f"regra {rule_id or '?'}: session_ttl_s={value!r} não é número",
            rule_id=rule_id) from None
    if not 0.0 <= number <= MAX_TTL_S:
        raise AutonomyError(
            f"regra {rule_id or '?'}: session_ttl_s={number} fora de "
            f"0..{MAX_TTL_S}", rule_id=rule_id)
    return number


@dataclass(frozen=True)
class ToolDecision:
    """Resultado de uma tentativa de ferramenta; nunca executa nada."""

    allowed: bool
    requires_confirm: bool
    reason: str
    persona: str
    tool: str
    path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_confirm": self.requires_confirm,
            "reason": self.reason,
            "persona": self.persona,
            "tool": self.tool,
            "path": self.path,
        }


@dataclass(frozen=True)
class PersonaGrant:
    """Autonomia efetiva de uma persona, já com o override do dono aplicado."""

    persona: str
    tools: frozenset[str]
    deny_tools: frozenset[str]
    read_roots: tuple[str, ...]
    requires_confirm: frozenset[str]
    max_tokens: int
    session_ttl_s: float
    sources: tuple[str, ...] = ()
    note: str = ""

    def allows(self, tool: str) -> bool:
        key = tool_key(tool)
        if not key or key in self.deny_tools:
            return False
        return key in self.tools

    def confirm_needed(self, tool: str) -> bool:
        return tool_key(tool) in self.requires_confirm

    def read_allowed(self, path: str = "") -> bool:
        if not self.read_roots:
            return True
        target = str(path or "").strip()
        if not target or target in (".", "./"):
            # The sidecar pins the session cwd to a configured root; a relative
            # Glob/Grep with no path is already inside it.
            return True
        expanded = os.path.expanduser(os.path.expandvars(target))
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(PROJECT_ROOT), expanded)
        candidate = Path(expanded).resolve(strict=False)
        for root in self.read_roots:
            base = Path(root).resolve(strict=False)
            if candidate == base or base in candidate.parents:
                return True
        return False

    def check_tool(self, tool: str, *, path: str = "",
                   confirm: bool = False) -> ToolDecision:
        """Portão declarativo: nega, pede confirmação ou libera."""
        key = tool_key(tool)
        if not key:
            return ToolDecision(False, False, "ferramenta_vazia", self.persona,
                                key, str(path or ""))
        if key in self.deny_tools:
            return ToolDecision(False, False, "negado_pelo_dono", self.persona,
                                key, str(path or ""))
        if key not in self.tools:
            return ToolDecision(False, False, "fora_da_lista_de_autonomia",
                                self.persona, key, str(path or ""))
        if key in self.requires_confirm and not confirm:
            return ToolDecision(False, True, "exige_confirmacao_do_dono",
                                self.persona, key, str(path or ""))
        if key in READ_TOOLS and not self.read_allowed(path):
            return ToolDecision(False, False, "leitura_fora_dos_diretorios",
                                self.persona, key, str(path or ""))
        return ToolDecision(True, False, "permitido", self.persona, key,
                            str(path or ""))

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "tools": sorted(self.tools),
            "deny_tools": sorted(self.deny_tools),
            "read_roots": list(self.read_roots),
            "requires_confirm": sorted(self.requires_confirm),
            "max_tokens": self.max_tokens,
            "session_ttl_s": self.session_ttl_s,
            "sources": list(self.sources),
            "note": self.note,
        }


def _autonomy_block(value: Any, *, rule_id: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AutonomyError(
            f"regra {rule_id or '?'}: 'autonomy' deve ser objeto",
            rule_id=rule_id)
    known = {"tools", "deny_tools", "read_roots", "requires_confirm",
             "max_tokens", "session_ttl_s", "note"}
    unknown = set(value) - known
    if unknown:
        raise AutonomyError(
            f"regra {rule_id or '?'}: chave(s) desconhecida(s) em autonomy: "
            f"{sorted(unknown)}", rule_id=rule_id)
    return value


def _defaults(raw: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise AutonomyError("'defaults' deve ser objeto", path=path)
    block = _autonomy_block(defaults.get("autonomy"), rule_id="defaults")
    return {
        "tools": set(_as_tools(block.get("tools", DEFAULT_TOOLS),
                               field_name="tools")),
        "deny_tools": set(_as_tools(block.get("deny_tools"),
                                    field_name="deny_tools")),
        "read_roots": _as_roots(block.get("read_roots")),
        "requires_confirm": set(_as_tools(block.get("requires_confirm"),
                                          field_name="requires_confirm")),
        "max_tokens": _as_int(block.get("max_tokens"), field_name="max_tokens",
                              default=12_000),
        "session_ttl_s": _as_ttl(block.get("session_ttl_s"), default=900.0),
        "note": str(block.get("note") or ""),
    }


def _matches(rule: Any, slug: str) -> bool:
    """A persona casa pela rota (slug), pelo nome/alias ou por ``*``."""
    key = norm(slug)
    route = norm(getattr(rule, "route", "") or "")
    if route == "*" or (route and route == key):
        return True
    for pattern in getattr(rule, "contains", ()) or ():
        if pattern and (pattern == key or key in pattern):
            return True
    regex = str(getattr(rule, "regex", "") or "")
    if regex:
        try:
            if re.search(regex, key):
                return True
        except re.error:
            return False
    return False


class _Builder:
    def __init__(self, defaults: Mapping[str, Any]):
        self.tools = set(defaults["tools"])
        self.deny_tools = set(defaults["deny_tools"])
        self.read_roots = tuple(defaults["read_roots"])
        self.requires_confirm = set(defaults["requires_confirm"])
        self.max_tokens = int(defaults["max_tokens"])
        self.session_ttl_s = float(defaults["session_ttl_s"])
        self.note = str(defaults.get("note") or "")
        self.sources: list[str] = []
        self._locked: set[str] = set()

    def apply(self, block: Mapping[str, Any], *, source_id: str,
              owner: bool) -> None:
        if "deny_tools" in block:
            # Negações acumulam: um override do dono nunca perde uma negação.
            self.deny_tools.update(_as_tools(
                block["deny_tools"], field_name="deny_tools",
                rule_id=source_id))
        if "requires_confirm" in block:
            self.requires_confirm.update(_as_tools(
                block["requires_confirm"], field_name="requires_confirm",
                rule_id=source_id))
        for key in ("tools", "read_roots", "max_tokens", "session_ttl_s"):
            if key not in block:
                continue
            if owner and key in self._locked:
                # O primeiro override que fala de uma chave é o do dono e vence.
                continue
            if key == "tools":
                self.tools = set(_as_tools(block[key], field_name="tools",
                                           rule_id=source_id))
            elif key == "read_roots":
                self.read_roots = _as_roots(block[key], rule_id=source_id)
            elif key == "max_tokens":
                self.max_tokens = _as_int(
                    block[key], field_name="max_tokens", rule_id=source_id,
                    default=self.max_tokens)
            elif key == "session_ttl_s":
                self.session_ttl_s = _as_ttl(
                    block[key], rule_id=source_id,
                    default=self.session_ttl_s)
            if owner:
                self._locked.add(key)
        if block.get("note"):
            text = str(block["note"]).strip()
            if text:
                self.note = f"{self.note} | {text}".strip(" |")
        self.sources.append(source_id)

    def grant(self, slug: str) -> PersonaGrant:
        return PersonaGrant(
            persona=slug,
            tools=frozenset(self.tools),
            deny_tools=frozenset(self.deny_tools),
            read_roots=tuple(self.read_roots),
            requires_confirm=frozenset(self.requires_confirm),
            max_tokens=int(self.max_tokens),
            session_ttl_s=float(self.session_ttl_s),
            sources=tuple(self.sources),
            note=self.note,
        )


@dataclass
class AutonomyTable:
    """Tabela de autonomia por persona, recarregável sem reiniciar o processo."""

    table: RulesTable
    raw: dict[str, Any]
    defaults: dict[str, Any]
    source: str = ""
    version: int = 1
    _mtime: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                   repr=False)

    def __post_init__(self) -> None:
        self._raw_by_id = {}
        for key in ("overrides", "rules"):
            items = self.raw.get(key) or []
            if not isinstance(items, list):
                raise AutonomyError(f"'{key}' deve ser lista", path=self.source)
            for item in items:
                if isinstance(item, Mapping) and item.get("id"):
                    self._raw_by_id[str(item["id"])] = item

    # ── carga ───────────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *,
                  source: str = "") -> "AutonomyTable":
        if not isinstance(data, Mapping):
            raise AutonomyError("raiz da tabela deve ser objeto", path=source)
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError):
            raise AutonomyError("'version' deve ser inteiro",
                                path=source) from None
        if version != 1:
            raise AutonomyError(
                f"tabela de autonomia com version {version} incompatível; "
                "esta metade fala 1", path=source)
        try:
            table = RulesTable.from_dict(data, source=source)
        except RulesError as exc:
            raise AutonomyError(str(exc), path=source) from None
        defaults = _defaults(data, path=source)
        parsed = cls(table=table, raw=dict(data), defaults=defaults,
                     source=source, version=version)
        # Valida TODOS os blocos agora: um erro de dado aparece na carga, não
        # quando uma persona é chamada no meio da conversa.
        for rule_id in parsed._raw_by_id:
            item = parsed._raw_by_id[rule_id]
            _autonomy_block(item.get("autonomy"), rule_id=rule_id)
        return parsed

    @classmethod
    def from_file(cls, path: str | Path | None = None, *,
                  missing_ok: bool = False) -> "AutonomyTable":
        target = Path(path or default_autonomy_path())
        if not target.is_file():
            if missing_ok:
                return cls(table=RulesTable(source=str(target)), raw={},
                           defaults=_defaults({}, path=str(target)),
                           source=str(target))
            raise AutonomyError(
                f"tabela de autonomia não encontrada: {target}",
                path=str(target))
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AutonomyError(f"não consegui ler {target}: {exc}",
                                path=str(target)) from None
        table = cls.from_dict(raw, source=str(target))
        try:
            table._mtime = target.stat().st_mtime
        except OSError:
            table._mtime = 0.0
        return table

    def reload_if_changed(self) -> bool:
        if not self.source:
            return False
        target = Path(self.source)
        try:
            mtime = target.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        fresh = type(self).from_file(target)
        with self._lock:
            self.table = fresh.table
            self.raw = fresh.raw
            self.defaults = fresh.defaults
            self._raw_by_id = fresh._raw_by_id
            self._mtime = fresh._mtime
        return True

    # ── resolução ───────────────────────────────────────────────────────────
    def _block_for(self, rule_id: str) -> Mapping[str, Any]:
        item = self._raw_by_id.get(rule_id) or {}
        return _autonomy_block(item.get("autonomy"), rule_id=rule_id)

    def grant(self, persona: str) -> PersonaGrant:
        slug = norm(persona)
        if not slug:
            raise AutonomyError("persona vazia")
        builder = _Builder(self.defaults)
        # Regras normais: da menor prioridade para a maior (a última aplicada
        # é a de maior prioridade, como na RulesTable).
        for rule in reversed(self.table.rules):
            if _matches(rule, slug):
                builder.apply(self._block_for(rule.id), source_id=rule.id,
                              owner=False)
        # Overrides do dono por último e na ordem do arquivo: precedência máxima.
        for rule in self.table.overrides:
            if _matches(rule, slug):
                builder.apply(self._block_for(rule.id), source_id=rule.id,
                              owner=True)
        return builder.grant(slug)

    def personas(self) -> list[str]:
        out: list[str] = []
        for rule in (*self.table.overrides, *self.table.rules):
            route = norm(rule.route)
            if route and route != "*" and route not in out:
                out.append(route)
        return sorted(out)

    def describe(self, persona: str | None = None) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "version": self.version,
            "personas": self.personas(),
            "defaults": {
                "tools": sorted(self.defaults["tools"]),
                "deny_tools": sorted(self.defaults["deny_tools"]),
                "read_roots": list(self.defaults["read_roots"]),
                "requires_confirm": sorted(self.defaults["requires_confirm"]),
                "max_tokens": self.defaults["max_tokens"],
                "session_ttl_s": self.defaults["session_ttl_s"],
            },
        }
        if persona:
            payload["grant"] = self.grant(persona).as_dict()
        return payload


def default_autonomy_path() -> Path:
    return Path(os.environ.get(ENV_AUTONOMY_PATH) or DEFAULT_AUTONOMY_PATH)


def load_autonomy(path: str | Path | None = None, *,
                  missing_ok: bool = False) -> AutonomyTable:
    return AutonomyTable.from_file(path, missing_ok=missing_ok)
