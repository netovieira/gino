"""Delegação: de quem é o assunto? (ordem do dono, 09/11/2026)

"Você (Gino) não vai executar tudo mais, você vai executar tudo que não
existir personas para o mesmo, quando existir você vai falar/pedir a persona
pra fazer." Este módulo responde essa pergunta em código, com o quadro e os
cargos reais, e devolve o pedido pronto para ``persona_ask``:

>>> plan("refaça o telão com react e ts", personas)   # -> Aline, delegada
>>> plan("crie uma persona nova", personas)            # -> Gino executa

Três sinais, nesta ordem:

1. **vocativo** — o dono chamou a persona pelo nome (``relay``);
2. **projeto** — a tarefa aponta para uma pasta e o ``projects`` decide pela
   stack dela (pino do dono vence detecção);
3. **assunto** — as palavras da tarefa batem com o cargo, as stacks ou as
   skills de uma persona.

Assunto do próprio orquestrador (cadastro de personas, limites, cascata de
modelos, esta rota) nunca é delegado: não há para quem. Ninguém é delegado
duas vezes por engado — empate entre personas vira pergunta, não sorteio.

Nada aqui abre processo nem fala: é decisão. A execução continua no caminho
``persona_ask`` que o pipeline vivo já tem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from core import personas as _personas
from core import projects as _projects

from .limits import SLOTS
from .relay import find_addressed_persona, norm

ASK_TOOL = "persona_ask"

# Assunto do orquestrador: o que ele faz ele mesmo, porque não existe persona
# para isso. Casar por palavra solta ("persona", "cargos", "limites").
ORCHESTRATOR_SUBJECTS = (
    "persona", "personas", "cargo", "cargos", "orquestrador", "cascata",
    "limite", "limites", "delegacao", "delegar", "configuracao",
    "configuracoes", "voice_id", "voice", "modelos", "catalogo", "quadro",
)

# Conector de cargo não é assunto: "Analista **de** Monitoramento **e**
# Segurança" só contribui monitoramento e segurança.
_STOP_WORDS = {
    "de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "um", "uma",
    "para", "por", "com", "sem", "na", "no", "nas", "nos", "que", "em",
}

# Palavras que abrem a frente do assunto (verbo de tarefa), nunca contadas.
_IGNORE_WORDS = {"faz", "faca", "fazer", "ve", "ver", "olha", "olhar",
                 "muda", "mudar", "ajusta", "ajustar", "revisa", "revisar",
                 "implementa", "implementar", "conserta", "consertar",
                 "preciso", "quero", "pode", "consegue", "agora", "hoje",
                 "pra", "para", "meu", "minha", "seu", "sua", "isso",
                 "nesse", "nessa", "deste", "daqui", "como", "qual"}

_PATH_RE = re.compile(r"(~?/[^\s,';:]+|[A-Za-z]:\\[^\s,';]+)")


@dataclass(frozen=True)
class DelegationPlan:
    """Para quem a tarefa vai, e o que o Gino diz ao encaminhar."""

    delegated: bool
    slug: str = ""
    name: str = ""
    role: str = ""
    reason: str = ""
    task: str = ""
    lead: str = ""
    blocked: str = ""
    candidates: tuple[str, ...] = ()
    scores: Mapping[str, int] = field(default_factory=dict)

    @property
    def tool_call(self) -> dict[str, str]:
        """O pedido pronto: o chamador só entrega este dicionário."""
        return {"tool": ASK_TOOL, "persona": self.slug, "task": self.task}

    def as_dict(self) -> dict[str, Any]:
        return {
            "delegated": self.delegated,
            "persona": self.slug,
            "name": self.name,
            "role": self.role,
            "reason": self.reason,
            "task": self.task,
            "lead": self.lead,
            "blocked": self.blocked,
            "candidates": list(self.candidates),
            "scores": dict(self.scores),
            "tool_call": self.tool_call if self.delegated else {},
        }


def subject_words(persona: Mapping[str, Any]) -> set[str]:
    """O vocabulário de uma persona: cargo, stacks e skills."""
    words: set[str] = set()
    role = norm(_personas.resolve_role(persona))
    words |= {part for part in re.split(r"[^a-z0-9]+", role) if len(part) > 3
              and part not in _STOP_WORDS}
    for stack in persona.get("stacks") or []:
        tag = norm(stack)
        if tag and tag not in _STOP_WORDS:
            words.add(tag)
    for skill in persona.get("skills") or []:
        for part in re.split(r"[^a-z0-9]+", norm(skill)):
            if len(part) > 3 and part not in _STOP_WORDS:
                words.add(part)
    return words


def _words(text: str) -> set[str]:
    body = norm(text)
    parts = [part for part in re.split(r"[^a-z0-9_]+", body) if part]
    return {part for part in parts
            if len(part) > 2 and part not in _IGNORE_WORDS}


def _stack_tags(text: str) -> set[str]:
    """Stacks ditas na frase ("react", "ts", "next.js") já normalizadas."""
    return set(_projects.normalize_stacks(re.split(r"[^A-Za-z0-9.+#-]+",
                                                   str(text or ""))))


def _paths_in(text: str) -> list[str]:
    return [part for part in _PATH_RE.findall(str(text or ""))]


def _own_business(text: str) -> bool:
    body = norm(text)
    words = {part for part in re.split(r"[^a-z0-9_]+", body) if part}
    return bool(words & set(ORCHESTRATOR_SUBJECTS))


def _lead(persona: Mapping[str, Any]) -> str:
    """A frase do Gino ao encaminhar — ele avisa, não some."""
    name = str(persona.get("name") or persona.get("id") or "")
    role = _personas.resolve_role(persona)
    address = _personas.address(persona, contraction=True)
    if role:
        return f"Vou pedir {address} ver — {name}, {role}."
    return f"Vou pedir {address} ver."


def score_personas(text: str,
                   personas: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Pontuação de assunto por persona; 0 é "não é dela"."""
    spoken = _words(text) | _stack_tags(text)
    scores: dict[str, int] = {}
    for persona in personas or []:
        if not isinstance(persona, Mapping):
            continue
        slug = str(persona.get("id") or persona.get("name") or "").strip()
        if not slug:
            continue
        # Nome dito na tarefa é o sinal mais forte que existe: o dono quis ela.
        if {norm(persona.get("name")), norm(persona.get("id"))} & spoken:
            scores[slug] = scores.get(slug, 0) + 3
        hits = spoken & subject_words(persona)
        if hits:
            scores[slug] = scores.get(slug, 0) + len(hits)
    return scores


def plan(task: str, personas: Iterable[Mapping[str, Any]] | None = None,
         *, path: str = "", account: str = "") -> DelegationPlan:
    """Decide a quem pertence a tarefa e prepara o ``persona_ask``.

    ``personas`` é o quadro (registros); vazio lê o store. ``path`` força o
    projeto; sem ``path``, um caminho escrito na tarefa já basta para a rota
    por projeto entrar.
    """
    body = str(task or "").strip()
    if not body:
        return DelegationPlan(False, reason="tarefa_vazia", task=body)
    items = list(personas) if personas is not None else _personas.list_personas()
    items = [p for p in items if isinstance(p, Mapping)]
    if not items:
        return DelegationPlan(False, reason="sem_personas_cadastradas",
                              task=body)

    if _own_business(body):
        return DelegationPlan(False, reason="assunto_do_orquestrador",
                              task=body)

    # 1) o dono chamou pelo nome
    addressed = find_addressed_persona(body, items)
    if addressed is not None:
        persona = _personas.get(addressed.slug) or next(
            (p for p in items if str(p.get("id")) == addressed.slug), {})
        return _ready(persona, body, f"dono_chamou_{addressed.slug}")

    # 2) a tarefa aponta para um projeto
    wanted_path = str(path or "").strip() or next(iter(_paths_in(body)), "")
    if wanted_path:
        hit = _projects.resolve(wanted_path, items)
        if hit.get("status") == "match":
            return _ready(hit["persona"], body,
                          f"projeto_{hit.get('source', 'stack')}")
        if hit.get("status") == "ambiguous":
            names = tuple(str(c.get("id") or c.get("name") or "")
                          for c in hit.get("candidates", []))
            return DelegationPlan(False, reason="projeto_ambiguo", task=body,
                                  candidates=names,
                                  lead="Mais de uma persona cuida desse "
                                       "projeto; qual eu chamo?")

    # 3) assunto: cargo, stacks, skills
    scores = score_personas(body, items)
    if scores:
        best = max(scores.values())
        winners = [slug for slug, value in scores.items() if value == best]
        if len(winners) > 1:
            return DelegationPlan(False, reason="assunto_ambiguo", task=body,
                                  candidates=tuple(winners), scores=scores,
                                  lead="Esse assunto é de mais de uma persona "
                                       f"({', '.join(winners)}); qual usar?")
        persona = _personas.get(winners[0]) or next(
            (p for p in items if str(p.get("id")) == winners[0]), {})
        return _ready(persona, body, "assunto_da_persona", scores)

    return DelegationPlan(False, reason="sem_persona_para_o_assunto",
                          task=body, scores=scores)


def _ready(persona: Mapping[str, Any], task: str, reason: str,
           scores: Mapping[str, int] | None = None) -> DelegationPlan:
    """Plano de uma persona encontrada; o teto de concorrência é avisado aqui."""
    slug = str(persona.get("id") or persona.get("name") or "")
    blocked = ""
    if slug:
        ok, why = SLOTS.can_request(slug)
        if not ok:
            blocked = why
    return DelegationPlan(
        delegated=bool(slug),
        slug=slug,
        name=str(persona.get("name") or slug),
        role=_personas.resolve_role(persona),
        reason=reason,
        task=task,
        lead=_lead(persona) if slug else "",
        blocked=blocked,
        scores=dict(scores or {}),
    )
