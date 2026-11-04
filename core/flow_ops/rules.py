"""Regras do roteador como DADO, não como código.

A tabela fica em ``config/flows_rules.json`` (caminho configurável). O roteador
não conhece frase nenhuma: ele carrega o arquivo e casa o texto com as regras.
Assim, mudar rota, prazo ou exigência de confirmação — inclusive o **override do
dono** — é editar dados, sem tocar em código e sem reiniciar o processo
(:meth:`RulesTable.reload_if_changed`).

Precedência, da maior para a menor:

1. ``overrides`` — regras do dono. Casam antes de tudo, na ordem do arquivo. Um
   override pode apontar para um ``turn_id`` específico ou para um padrão de
   texto e, se quiser, vetar (``action="deny"``).
2. ``rules`` — ordem de ``priority`` (maior primeiro) e, no empate, ordem do
   arquivo.

Formato de uma regra::

    {
      "id": "hora",
      "flow": "reasoning",
      "action": "lookup_time",
      "deadline_ms": 4000,
      "requires_confirm": false,
      "priority": 50,
      "enabled": true,
      "match": {
        "any": ["que horas", "hora agora"],
        "regex": "\\\\bhoras?\\\\b",
        "route": "hora",
        "trust": "owner",
        "source": "owner",
        "turn_id": ""
      }
    }

Todas as condições de ``match`` são conjuntivas; ``any`` casa se **qualquer**
frase aparecer; ``route``/``routes`` casam a rota já decidida pela onda 1;
``trust``/``source`` casam o contexto. Texto e frases são normalizados (minúsculo
e sem acento) antes de comparar; ``regex`` roda no texto normalizado.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "flows_rules.json"
ENV_RULES_PATH = "JARVIS_FLOWS_RULES"

DEFAULT_FLOW = "reasoning"
DEFAULT_ACTION = "reason"
DEFAULT_DEADLINE_MS = 8000
MAX_DEADLINE_MS = 600_000

_MATCH_KEYS = {"any", "regex", "route", "routes", "trust", "source", "turn_id"}
_RULE_KEYS = {"id", "flow", "action", "deadline_ms", "requires_confirm",
              "priority", "enabled", "match", "note"}


class RulesError(ValueError):
    """Tabela de regras inválida (arquivo, versão, regra ou regex)."""

    def __init__(self, message: str, *, path: str = "", rule_id: str = ""):
        super().__init__(message)
        self.path = path
        self.rule_id = rule_id


def norm(text: Any) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


def _as_list(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (norm(value),)
    if isinstance(value, (list, tuple)):
        return tuple(norm(v) for v in value if str(v or "").strip())
    raise RulesError(f"lista de frases inválida: {value!r}")


@dataclass(frozen=True)
class Rule:
    id: str
    flow: str = DEFAULT_FLOW
    action: str = DEFAULT_ACTION
    deadline_ms: int = DEFAULT_DEADLINE_MS
    requires_confirm: bool = False
    priority: int = 0
    enabled: bool = True
    contains: tuple[str, ...] = ()
    regex: str = ""
    route: str = ""
    routes: tuple[str, ...] = ()
    trust: str = ""
    source: str = ""
    turn_id: str = ""
    origin: str = "rule"
    note: str = ""

    def matches(self, text: str, *, route: str = "", trust: str = "",
                source: str = "", turn_id: str = "") -> bool:
        if not self.enabled:
            return False
        if self.turn_id and norm(self.turn_id) != norm(turn_id):
            return False
        if self.trust and norm(self.trust) != norm(trust):
            return False
        if self.source and norm(self.source) != norm(source):
            return False
        if self.routes:
            if norm(route) not in {norm(r) for r in self.routes}:
                return False
        elif self.route and norm(self.route) != norm(route):
            return False
        body = norm(text)
        if self.contains and not any(p in body for p in self.contains):
            return False
        if self.regex:
            try:
                if not re.search(self.regex, body):
                    return False
            except re.error as exc:  # validação já barra, mas não confie
                raise RulesError(f"regex inválida: {exc}", rule_id=self.id)
        if not (self.contains or self.regex or self.route or self.routes
                or self.trust or self.source or self.turn_id):
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "flow": self.flow, "action": self.action,
            "deadline_ms": self.deadline_ms,
            "requires_confirm": self.requires_confirm,
            "priority": self.priority, "enabled": self.enabled,
            "origin": self.origin, "note": self.note,
        }


@dataclass(frozen=True)
class RuleHit:
    rule: Rule
    origin: str
    text: str

    @property
    def id(self) -> str:
        return self.rule.id


def _coerce_int(value: Any, *,
                field_name: str = "", rule_id: str = "",
                default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        if value is None:
            return default
        raise RulesError(
            f"regra {rule_id or '?'}: {field_name}={value!r} não é inteiro",
            rule_id=rule_id)


def _parse_rule(data: Mapping[str, Any], *, origin: str,
                path: str = "") -> Rule:
    if not isinstance(data, Mapping):
        raise RulesError(f"regra deve ser objeto, veio {type(data).__name__}",
                         path=path)
    rule_id = str(data.get("id") or "").strip()
    if not rule_id:
        raise RulesError("regra sem 'id'", path=path)
    match = data.get("match") or {}
    if not isinstance(match, Mapping):
        raise RulesError(f"regra {rule_id}: 'match' deve ser objeto",
                         path=path, rule_id=rule_id)
    unknown_match = set(match) - _MATCH_KEYS
    if unknown_match:
        raise RulesError(
            f"regra {rule_id}: chave(s) desconhecida(s) em match: "
            f"{sorted(unknown_match)}", path=path, rule_id=rule_id)
    try:
        contains = _as_list(match.get("any"))
    except RulesError as exc:
        raise RulesError(f"regra {rule_id}: {exc}", path=path,
                         rule_id=rule_id) from None
    regex = str(match.get("regex") or "").strip()
    if regex:
        try:
            re.compile(regex)
        except re.error as exc:
            raise RulesError(
                f"regra {rule_id}: regex inválida ({regex!r}): {exc}",
                path=path, rule_id=rule_id) from None
        if len(regex) > 400:
            raise RulesError(f"regra {rule_id}: regex longa demais",
                             path=path, rule_id=rule_id)
    routes = _as_list(match.get("routes"))
    route = ""
    if "route" in match and match.get("route"):
        route = norm(match.get("route"))
    flow = str(data.get("flow") or DEFAULT_FLOW).strip() or DEFAULT_FLOW
    action = str(data.get("action") or DEFAULT_ACTION).strip() or DEFAULT_ACTION
    deadline_ms = _coerce_int(data.get("deadline_ms"),
                              field_name="deadline_ms", rule_id=rule_id,
                              default=DEFAULT_DEADLINE_MS)
    if not 1 <= deadline_ms <= MAX_DEADLINE_MS:
        raise RulesError(
            f"regra {rule_id}: deadline_ms={deadline_ms} fora de "
            f"1..{MAX_DEADLINE_MS}", path=path, rule_id=rule_id)
    priority = _coerce_int(data.get("priority"), field_name="priority",
                           rule_id=rule_id, default=0)
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RulesError(f"regra {rule_id}: 'enabled' deve ser booleano",
                         path=path, rule_id=rule_id)
    return Rule(
        id=rule_id, flow=flow, action=action, deadline_ms=deadline_ms,
        requires_confirm=bool(data.get("requires_confirm", False)),
        priority=priority, enabled=enabled, contains=contains, regex=regex,
        route=route, routes=routes,
        trust=norm(match.get("trust") or ""),
        source=norm(match.get("source") or ""),
        turn_id=norm(match.get("turn_id") or ""),
        origin=origin, note=str(data.get("note") or ""))


def _parse_rules(items: Any, *, origin: str, path: str) -> list[Rule]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise RulesError(f"{origin!r} deve ser lista", path=path)
    rules: list[Rule] = []
    seen: set[str] = set()
    for item in items:
        rule = _parse_rule(item, origin=origin, path=path)
        if rule.id in seen:
            raise RulesError(f"id de regra duplicado: {rule.id!r}",
                             path=path, rule_id=rule.id)
        seen.add(rule.id)
        rules.append(rule)
    return rules


@dataclass
class RulesTable:
    """Tabela imutável de regras + overrides do dono (dados)."""

    overrides: list[Rule] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    source: str = ""
    version: int = 1
    default_flow: str = DEFAULT_FLOW
    default_action: str = DEFAULT_ACTION
    default_deadline_ms: int = DEFAULT_DEADLINE_MS
    _mtime: float = 0.0
    _index: dict[str, Rule] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._reindex()

    def _reindex(self) -> None:
        self._index = {r.id: r for r in (*self.overrides, *self.rules)}
        ordered = sorted(enumerate(self.rules),
                         key=lambda pair: (-pair[1].priority, pair[0]))
        self.rules = [rule for _idx, rule in ordered]

    # ── carga ───────────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *,
                  source: str = "") -> "RulesTable":
        if not isinstance(data, Mapping):
            raise RulesError("raiz da tabela deve ser objeto", path=source)
        try:
            version = int(data.get("version", 1))
        except (TypeError, ValueError):
            raise RulesError("'version' deve ser inteiro", path=source) from None
        if version != 1:
            raise RulesError(
                f"tabela com version {version} incompatível; esta metade fala 1",
                path=source)
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, Mapping):
            raise RulesError("'defaults' deve ser objeto", path=source)
        table = cls(
            overrides=_parse_rules(data.get("overrides"), origin="override",
                                   path=source),
            rules=_parse_rules(data.get("rules"), origin="rule", path=source),
            source=source, version=version,
            default_flow=str(defaults.get("flow") or DEFAULT_FLOW),
            default_action=str(defaults.get("action") or DEFAULT_ACTION),
            default_deadline_ms=_coerce_int(
                defaults.get("deadline_ms"), field_name="deadline_ms",
                default=DEFAULT_DEADLINE_MS))
        return table

    @classmethod
    def from_file(cls, path: str | Path | None = None,
                  *, missing_ok: bool = False) -> "RulesTable":
        target = Path(path or default_rules_path())
        if not target.is_file():
            if missing_ok:
                return cls(source=str(target))
            raise RulesError(f"tabela de regras não encontrada: {target}",
                             path=str(target))
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RulesError(f"não consegui ler {target}: {exc}",
                             path=str(target)) from None
        table = cls.from_dict(raw, source=str(target))
        try:
            table._mtime = target.stat().st_mtime
        except OSError:
            table._mtime = 0.0
        return table

    def reload_if_changed(self) -> bool:
        """Recarrega se o arquivo mudou (override do dono sem reiniciar)."""
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
        self.overrides = fresh.overrides
        self.rules = fresh.rules
        self.default_flow = fresh.default_flow
        self.default_action = fresh.default_action
        self.default_deadline_ms = fresh.default_deadline_ms
        self._mtime = fresh._mtime
        self._reindex()
        return True

    # ── override do dono ────────────────────────────────────────────────────
    def add_override(self, spec: Mapping[str, Any]) -> Rule:
        """Adiciona um override em memória, à frente de todos (dado do dono)."""
        data = dict(spec)
        data.setdefault("flow", self.default_flow)
        data.setdefault("action", self.default_action)
        data.setdefault("deadline_ms", self.default_deadline_ms)
        rule = _parse_rule(data, origin="override")
        if rule.id in self._index:
            raise RulesError(f"id de regra duplicado: {rule.id!r}",
                             rule_id=rule.id)
        self.overrides.insert(0, rule)
        self._reindex()
        return rule

    def remove_override(self, rule_id: str) -> bool:
        for index, rule in enumerate(self.overrides):
            if rule.id == rule_id:
                del self.overrides[index]
                self._reindex()
                return True
        return False

    # ── roteamento ──────────────────────────────────────────────────────────
    def match(self, text: str, *, route: str = "", trust: str = "owner",
              source: str = "owner", turn_id: str = "") -> RuleHit | None:
        for rule in self.overrides:
            if rule.matches(text, route=route, trust=trust,
                            source=source, turn_id=turn_id):
                return RuleHit(rule=rule, origin="override",
                               text=norm(text))
        for rule in self.rules:
            if rule.matches(text, route=route, trust=trust,
                            source=source, turn_id=turn_id):
                return RuleHit(rule=rule, origin="rule", text=norm(text))
        return None

    def defaults(self) -> dict[str, Any]:
        return {"flow": self.default_flow, "action": self.default_action,
                "deadline_ms": self.default_deadline_ms}

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "overrides": [r.as_dict() for r in self.overrides],
            "rules": [r.as_dict() for r in self.rules],
            "defaults": self.defaults(),
        }

    def __len__(self) -> int:
        return len(self.overrides) + len(self.rules)


def default_rules_path() -> Path:
    import os
    return Path(os.environ.get(ENV_RULES_PATH) or DEFAULT_RULES_PATH)


def load_rules(path: str | Path | None = None, *,
               missing_ok: bool = False) -> RulesTable:
    """Atalho de carga usado pelo despachante quente."""
    return RulesTable.from_file(path, missing_ok=missing_ok)
