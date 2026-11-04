"""Recado do dono para uma persona, sem acionar a boca do Gino.

O dono diz "Aline, revisa o card" ou "fala com o Arnaldo sobre o player". A
decisão de que isso é um recado PARA A PERSONA é feita aqui, em código, por
padrões explícitos de vocativo/encaminhamento — não por convenção de prompt.

O :class:`OwnerRelay` recebe a função que realmente entrega a tarefa à persona
(injetada; no pipeline vivo é o caminho ``persona_ask``) e a única boca que ela
pode tocar é a da persona, pelo :class:`~core.persona_ops.delivery.SpeakerArbiter`.
A boca do Gino é um parâmetro aceito só para auditoria e **nunca** é chamada.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .delivery import (Addressee, DeliveryDecision, SpeakerArbiter,
                       decide_delivery)


def norm(text: Any) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True)
class PersonaAddress:
    slug: str
    name: str
    pattern: str

    def as_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "name": self.name, "pattern": self.pattern}


@dataclass(frozen=True)
class RelayPlan:
    is_relay: bool
    slug: str = ""
    name: str = ""
    addressee: Addressee = Addressee.GINO
    suppress_gino_speech: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_relay": self.is_relay,
            "slug": self.slug,
            "name": self.name,
            "addressee": self.addressee.value,
            "suppress_gino_speech": self.suppress_gino_speech,
            "reason": self.reason,
        }


_RELAY_VERBS = (
    "fala", "falar", "fale", "conversa", "conversar", "converse",
    "chama", "chamar", "chame", "pergunta", "perguntar", "pergunte",
    "pede", "pedir", "peca", "consulta", "consultar", "consulte",
)


def _persona_names(persona: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for value in (persona.get("name"), persona.get("id"),
                  *(persona.get("aliases") or [])):
        text = str(value or "").strip()
        if text and norm(text) not in names:
            names.append(norm(text))
    return names


def find_addressed_persona(
        text: str, personas: Iterable[Mapping[str, Any]]
) -> PersonaAddress | None:
    """Quem o dono chamou pelo nome, se chamou.

    Só casam padrões de vocativo/encaminhamento (nome no início seguido de
    vírgula, "fala com a Aline", "chama o Arnaldo", "quero falar com a Aline").
    O nome solto no meio de uma frase não basta: "a Aline disse que" não é um
    recado para ela.
    """
    body = norm(text)
    if not body:
        return None
    for persona in personas:
        if not isinstance(persona, Mapping):
            continue
        slug = str(persona.get("id") or persona.get("name") or "").strip()
        name = str(persona.get("name") or slug).strip()
        if not slug:
            continue
        for candidate in _persona_names(persona):
            escaped = re.escape(candidate)
            patterns = (
                rf"^\s*{escaped}\s*[,:;!?-]",
                rf"\b(?:{'|'.join(_RELAY_VERBS)})\s+(?:com\s+)?"
                rf"(?:a|o|pra|pro|para|pela|pelo)?\s*{escaped}\b",
                rf"\b(?:quero|queria|preciso|vou|deixa eu)\s+"
                rf"(?:falar|conversar|chamar)\s+(?:com\s+)?"
                rf"(?:a|o|pra|pro|para)?\s*{escaped}\b",
            )
            for pattern in patterns:
                if re.search(pattern, body):
                    return PersonaAddress(slug=slug, name=name,
                                          pattern=pattern)
    return None


def classify_owner_relay(
        text: str, personas: Iterable[Mapping[str, Any]]
) -> RelayPlan:
    """Plano de entrega de um turno: relay para a persona ou turno do Gino."""
    address = find_addressed_persona(text, personas)
    if address is None:
        return RelayPlan(False, addressee=Addressee.GINO,
                         reason="sem_vocativo_de_persona")
    return RelayPlan(
        True, slug=address.slug, name=address.name,
        addressee=Addressee.OWNER, suppress_gino_speech=True,
        reason=f"dono_chamou_{address.slug}")


@dataclass(frozen=True)
class RelayResult:
    delivered: bool
    persona: str
    addressee: str
    gino_spoken: bool
    persona_spoken: bool
    decision: Mapping[str, Any]
    text: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "persona": self.persona,
            "addressee": self.addressee,
            "gino_spoken": self.gino_spoken,
            "persona_spoken": self.persona_spoken,
            "decision": dict(self.decision),
            "text": self.text,
            "reason": self.reason,
        }


class OwnerRelay:
    """Entrega o recado do dono à persona; a boca do Gino fica intocada."""

    def __init__(self, ask_persona: Callable[[str, str], Any],
                 arbiter: SpeakerArbiter, *,
                 gino_speak: Callable[..., Any] | None = None,
                 audit: Callable[[dict[str, Any]], None] | None = None):
        self.ask_persona = ask_persona
        self.arbiter = arbiter
        # Existe para o chamador provar (e o teste verificar) que não é usado.
        self.gino_speak = gino_speak
        self.audit = audit

    def _event(self, payload: dict[str, Any]) -> None:
        if self.audit is not None:
            try:
                self.audit(payload)
            except Exception:
                pass

    def deliver(self, message: str, persona: str, *,
                ask: Callable[[str, str], Any] | None = None,
                channel: str = "voice") -> RelayResult:
        body = str(message or "").strip()
        slug = str(persona or "").strip()
        if not body or not slug:
            result = RelayResult(False, slug, Addressee.OWNER.value, False,
                                 False, {}, "", "recado_ou_persona_vazio")
            self._event({"event": "relay", **result.as_dict()})
            return result
        decision: DeliveryDecision = decide_delivery(
            slug, Addressee.OWNER, channel=channel)
        try:
            answer = (ask or self.ask_persona)(slug, body)
        except Exception as exc:
            result = RelayResult(
                False, slug, Addressee.OWNER.value, False, False,
                decision.as_dict(), "",
                f"persona_falhou: {type(exc).__name__}: {exc}")
            self._event({"event": "relay", **result.as_dict()})
            return result
        text = ""
        if isinstance(answer, Mapping):
            text = str(answer.get("text") or answer.get("short") or "")
        else:
            text = str(answer or "")
        spoken = False
        if decision.spoken and text.strip():
            ticket = self.arbiter.submit(
                text, owner="persona", persona=slug, priority=20,
                decision=decision.as_dict())
            spoken = ticket is not None
        result = RelayResult(
            True, slug, Addressee.OWNER.value, False, spoken,
            decision.as_dict(), text,
            "entregue_a_persona" if spoken else "entregue_sem_fala")
        self._event({"event": "relay", **result.as_dict()})
        return result
