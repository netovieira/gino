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


# ── PORTÃO DECLARATIVO DO ADENDO DE 09/11/2026 ─────────────────────────────
#
# Item 1 do adendo aprovado (``skills/fluxos/FLUXOS.md``, decisão 10 do plano
# ``PLANO-2026-11-09-fluxo-agendamento-e-avisos-por-evento``):
#
#     "UM PORTAO DECLARATIVO POR TIPO DE FRASE: ORDEM COM HORA OU COM ESTADO
#      APONTADO NAO EXECUTA NA HORA — VIRA REGISTRO COM RECIBO."
#
# Item 4 do mesmo adendo diz ONDE a checagem acontece: antes de abrir
# execução, o cérebro vê se a frase tem **horário fixo** (→ agendador,
# mecanismo A) ou **estado apontado** (→ ouvinte, mecanismo B); só o que não
# tem nenhum dos dois executa na hora.
#
# O portão é DADO + CHECAGEM, não mecanismo: ele não agenda, não escuta e não
# fala. Recebe a frase e o que JÁ EXISTE como registro (a linha do
# ``core/scheduler.py`` / a escuta do ouvinte) e devolve veredito. Assim o
# fluxo de fala e o fluxo de trabalho consultam a MESMA regra — o que o portão
# pode, o fluxo pode (lei C1).

#: Nome do portão nos recibos, na linha de log e na recusa.
SCHEDULE_GATE = "require_schedule_or_watcher"

#: As três variações que o dono pediu para o ouvinte (item 3 do adendo).
#: vocabulário fechado: "estado apontado" sem um destes eventos não é escuta.
EVENTOS_DO_OUVINTE = ("iniciar", "encerrar", "mudou status")

#: O que existe hoje do lado do ouvinte: nada. ``core/event_watcher.py`` é a
#: tarefa ``backend-ouvinte-eventos`` do mesmo plano. Enquanto ele não existe, o
#: portão recusa a rota "estado apontado" dizendo exatamente isto — recusa com
#: recibo, nunca sucesso mudo (lei C4).
OUVINTE_INDISPONIVEL = ("não existe escuta registrada: o ouvinte de execução "
                        "(mecanismo B, core/event_watcher.py) ainda não tem "
                        "backend nesta casa")

#: Quem pede "avisar quando acontecer" — verbo de aviso/agendamento. Sem um
#: destes na frase, o portão não é chamado a falar: "que horas são?" tem hora
#: no texto e continua executando na hora. Substantivos soltos ficaram de
#: fora de propósito ("agenda", "aviso", "chamada" casariam perguntas
#: inofensivas como "o que tem na agenda hoje?" — falso positivo custaria ao
#: dono uma ordem legítima travada).
AVISOS = (
    "avisa", "avisar", "avise", "lembra", "lembrar", "lembre", "lembrete",
    "notifica", "notificar", "agendar", "agendei", "programar", "programme",
    "programa ai", "chamar", "chame", "me fala", "fala quando",
    "fica de olho", "olha quando", "vigia", "monitora", "monitorar",
    "escuta", "escutar", "confira quando", "verifica quando",
    "dispara quando", "roda quando", "execute quando",
)

#: Hora fixa (mecanismo A): instante marcado no texto. "depois" ficou fora:
#: sem instante, não é agendamento (regra 1 do dono).
_HORA_FIXA_RE = (
    r"\b(?:a[s]s?|pelas?)\s*\d{1,2}(?:[:.]\d{2})?\b"
    r"|\b\d{1,2}(?:[:.]\d{2})?\s*(?:horas?|h|min(?:utos?)?)\b"
    r"|\b(?:hoje|amanha|depois de amanha|sexta|sabado|domingo|segunda|"
    r"terca|quarta|quinta)\b"
    r"|\bdaqui a\s*\d*\s*(?:minutos?|min|horas?|h|dias?)\b"
    r"|\bem\s+(?:\d+|uma?|dois?|tres|quatro|cinco|seis?|sete|oito|nove|dez|"
    r"quinze|vinte|trinta|meia|meio)\s*(?:minutos?|min|horas?|h|dias?)\b"
)

#: Estado apontado (mecanismo B): o disparo depende de um processo, não de
#: relógio. "quando terminar", "assim que iniciar", "quando mudar o status".
_ESTADO_APONTADO_RE = (
    r"\b(?:quando|assim que|logo que|depois que|no momento que|sempre que)\b"
    r"[^.;]{0,60}?\b(?:terminar|terminou|acabar|acabou|acabe|finalizar|"
    r"finaliza|concluir|concluiu|pronto|encerrar|encerra|iniciar|inicia|"
    r"comecar|comeca|começar|mudar|muda|mudou|alterar|altera|status|estado|"
    r"estiver|chegar|chegou|receber|recebeu|responder|responde|subir|subiu|"
    r"baixar|baixou|rodar|rodou|executar|executa|fallhar|falhar|falhou|"
    r"erro|abrir|abriu|fechar|fechou|disponivel|no ar)\b"
    r"|\b(?:ao|quando)\s+(?:terminar|acabar|iniciar|finalizar|mudar)\b"
)

_HORA_FIXA = re.compile(_HORA_FIXA_RE)
_ESTADO_APONTADO = re.compile(_ESTADO_APONTADO_RE, re.DOTALL)


@dataclass(frozen=True)
class GateVerdict:
    """Veredito do portão: roda/agenda/escuta + o motivo legível da recusa."""

    ok: bool
    gate: str = SCHEDULE_GATE
    rota: str = ""
    classe: str = "nenhuma"
    motivo: str = ""
    faltou: tuple[str, ...] = ()
    texto: str = ""

    def __bool__(self) -> bool:
        return bool(self.ok)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "gate": self.gate, "rota": self.rota,
                "classe": self.classe, "motivo": self.motivo,
                "faltou": list(self.faltou), "texto": self.texto}


def tem_horario_fixo(texto: Any) -> bool:
    """A frase nomeia um instante? (o parse de hora real é do agendador)."""
    return bool(_HORA_FIXA.search(norm(texto)))


def tem_estado_apontado(texto: Any) -> bool:
    """A frase aponta um estado de processo em vez de relógio?"""
    return bool(_ESTADO_APONTADO.search(norm(texto)))


def pede_aviso_quando(texto: Any) -> bool:
    """A frase é um pedido de "avisar quando acontecer" (verbo + gatilho)."""
    body = norm(texto)
    if not body:
        return False
    return any(word in body for word in AVISOS)


def _registros(valor: Any) -> list[dict]:
    """Aceita um registro, uma lista de registros ou nada ("" / None)."""
    if valor is None or valor == "" or valor is False:
        return []
    items = valor if isinstance(valor, (list, tuple)) else [valor]
    saida = []
    for item in items:
        if isinstance(item, Mapping):
            saida.append(dict(item))
    return saida


def _agendamento_valido(linha: Mapping[str, Any]) -> tuple[bool, str]:
    """Regra 2 do dono: não existe agendamento sem gatilho resolvível."""
    faltam: list[str] = []
    if not str(linha.get("quando") or "").strip():
        faltam.append("quando")
    if linha.get("cancelado_em") or linha.get("disparou_em"):
        return False, "o agendamento apontado já se encerrou"
    gatilho = linha.get("gatilho")
    if not isinstance(gatilho, Mapping) or not gatilho:
        faltam.append("gatilho")
    else:
        tipo = norm(gatilho.get("tipo"))
        if tipo == "funcao":
            if not (str(gatilho.get("modulo") or "").strip()
                    and str(gatilho.get("funcao") or "").strip()):
                faltam.append("gatilho (modulo+funcao)")
        elif tipo == "script":
            if not str(gatilho.get("script") or "").strip():
                faltam.append("gatilho (script)")
        else:
            faltam.append(f"gatilho (tipo {tipo or 'vazio'} não é funcao/script)")
    if faltam:
        return False, "faltou no agendamento: " + ", ".join(faltam)
    return True, ""


def _escuta_valida(escuta: Mapping[str, Any]) -> tuple[bool, str]:
    """Escuta registrada é alvo + evento do vocabulário fechado (item 3)."""
    if not str(escuta.get("alvo") or "").strip():
        return False, "faltou na escuta: alvo (processo, porta, arquivo ou fila)"
    eventos = _as_list(escuta.get("eventos") or escuta.get("evento"))
    if not eventos:
        return False, ("faltou na escuta: evento de "
                       f"{list(EVENTOS_DO_OUVINTE)}")
    desconhecidos = [e for e in eventos if e not in EVENTOS_DO_OUVINTE]
    if desconhecidos:
        return False, (f"evento(s) fora do vocabulário do ouvinte: "
                       f"{desconhecidos}; o dono definiu {list(EVENTOS_DO_OUVINTE)}")
    if escuta.get("cancelado_em"):
        return False, "a escuta apontada foi cancelada"
    return True, ""


def require_schedule_or_watcher(texto: Any = "", *, agendamento: Any = None,
                                escuta: Any = None) -> GateVerdict:
    """O portão do adendo: ordem com hora/estado não executa na hora.

    Aceita (``ok=True``):

    * frase que **não** é pedido de aviso, ou é aviso sem hora e sem estado
      apontado — nada aqui a ver, rota ``executar-agora``;
    * pedido com hora fixa **e** um agendamento válido (linha real do
      ``core/scheduler.py``, com ``quando`` + gatilho resolvível);
    * pedido com estado apontado **e** uma escuta válida (alvo + evento de
      ``iniciar``/``encerrar``/``mudou status``).

    Recusa (``ok=False``, ``motivo`` nomeia o que faltou — lei C4):

    * aviso com hora fixa sem agendamento registrado;
    * aviso com estado apontado sem escuta registrada (e hoje nunca há, porque
      o mecanismo B ainda não tem backend);
    * registro apontado mas inválido (sem gatilho, já disparado/cancelado,
      evento fora do vocabulário).

    ``classe``/``rota`` seguem o item 4 do adendo: hora → ``agendador``,
    estado → ``ouvinte``. Quando a frase tem os dois, a classe é ``hora`` (só o
    relógio é verificável agora) e a recusa aceita qualquer um dos dois
    registros — edge case aceito de propósito, declarado aqui.
    """
    body = norm(texto)
    tem_hora = tem_horario_fixo(body)
    tem_estado = tem_estado_apontado(body)
    if not pede_aviso_quando(body) or not (tem_hora or tem_estado):
        return GateVerdict(ok=True, rota="executar-agora", classe="nenhuma",
                           motivo="a frase não é pedido de aviso com hora nem "
                                  "com estado apontado: o portão não trava nada",
                           texto=body)
    classe = "hora" if tem_hora else "estado"
    rota = "agendador" if classe == "hora" else "ouvinte"
    agendamentos = _registros(agendamento)
    escutas = _registros(escuta)
    erro_agendamento = ""
    for linha in agendamentos:
        valido, motivo = _agendamento_valido(linha)
        if valido:
            return GateVerdict(ok=True, rota=rota, classe=classe,
                               motivo="existe agendamento com gatilho: a ordem "
                                      "virou registro, não execução na hora",
                               texto=body)
        erro_agendamento = motivo or erro_agendamento
    erro_escuta = ""
    for registro in escutas:
        valido, motivo = _escuta_valida(registro)
        if valido:
            return GateVerdict(ok=True, rota=rota, classe=classe,
                               motivo="existe escuta registrada no ouvinte: a "
                                      "ordem virou registro, não execução na hora",
                               texto=body)
        erro_escuta = motivo or erro_escuta
    if classe == "hora":
        faltou = ("um agendamento no core/scheduler.py com quando + gatilho "
                  "(funcao ou script) — sem gatilho é recusa, não 'aviso padrão'")
        motivo = (f"a ordem tem horário fixo e nada foi agendado: {faltou}. "
                  "Não executa na hora.")
    else:
        faltou = OUVINTE_INDISPONIVEL
        motivo = (f"a ordem aponta um estado de processo e nada foi escutado: "
                  f"{faltou}. Não executa na hora.")
    invalido = erro_agendamento or erro_escuta
    if invalido and (agendamentos or escutas):
        motivo = (f"o registro apontado não vale: {invalido}. "
                  f"Para frase de {classe} é preciso {faltou}.")
    return GateVerdict(ok=False, rota=rota, classe=classe, motivo=motivo,
                       faltou=(faltou,) + ((invalido,) if invalido else ()),
                       texto=body)
