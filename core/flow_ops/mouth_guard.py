"""Guarda antiasserção no ponto de escrita da boca.

A boca é um 7b local: fala bem e inventa resultado. A guarda mora **onde a
própria boca escreve a frase**, não no orquestrador. Toda frase passa por
:meth:`MouthWriter.write`, que devolve o texto final: ou a frase do modelo,
quando ela só afirma o que um trabalhador devolveu, ou um **template
determinístico**, quando ela tenta dizer "abri", "tocando", "feito" sem fato.

Fato autorizado é fato devolvido de verdade: ``ok=True``, ``assertion`` não
desmentido, sem ``simulated``, sem ``pending_confirm`` e sem ``cancelled``.
Fato simulado nunca autoriza asserção. Número na frase que não aparece em fato
nenhum é invenção e cai no template. Nome de ferramenta/JSON na fala também.

O template não afirma sucesso: pede a confirmação exata quando há
``pending_confirm``, diz que nada foi executado quando há ``cancelled``, repete
o primeiro fato autorizado quando existe, e nos demais casos diz que ainda não
há resultado — nunca inventa.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping

ASSERTION_WORDS = (
    "aberto", "abri", "abriu", "feito", "pronto", "prontinho", "tocando",
    "anotado", "enviado", "concluido", "concluida", "deu certo", "ja esta",
    "entrei", "reiniciado", "executado", "ligado", "desligado", "pausado",
    "reiniciei", "fiz", "fechado", "confirmado", "atualizado", "salvo",
    "criado", "apagado", "instalado", "baixado",
)

#: Vazamento técnico que não pode virar fala.
LEAK_WORDS = ("media_control", "open_app", "reminder", "restart_assistant",
              "mcp__", "jarvis_call", "tool_call", "{\"", "}}")

NEUTRAL_TEMPLATE = "Ainda não tenho resultado para te dar."
BRIEF_TEMPLATE = "Conferindo o pedido, criador."
SIMULATED_TEMPLATE = "Isso ainda é simulação; resultado de verdade não veio."
CANCELLED_TEMPLATE = "Nada foi executado, criador; o pedido ficou no meio."
FAILED_TEMPLATE = "Ainda não tenho resultado confirmado."

MAX_SPEECH_CHARS = 240

_STOP_WORDS = {
    "para", "pelo", "pela", "com", "sem", "mais", "menos", "esta", "estava",
    "esse", "essa", "isso", "aqui", "agora", "muito", "pouco", "sobre",
}


def _keywords(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9%:]+", norm(text))
            if len(word) > 4 and word not in _STOP_WORDS}


def norm(text: Any) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


def clean(text: Any) -> str:
    """Uma frase só, sem aspas/prefixo, com espaço colapsado."""
    value = str(text or "").strip()
    value = re.split(r"\r?\n", value)[0].strip()
    value = value.strip(" \t\"'“”`")
    value = re.sub(r"^(frase|resposta|gino|boca)\s*:\s*", "", value,
                   flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > MAX_SPEECH_CHARS:
        cut = value[:MAX_SPEECH_CHARS]
        value = cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return value


def assertion_hit(text: Any) -> str:
    body = norm(text)
    for word in ASSERTION_WORDS:
        if re.search(_assertion_regex(word), body):
            return word
    return ""


@lru_cache(maxsize=None)
def _assertion_regex(word: str) -> str:
    parts = [re.escape(part) for part in norm(word).split()]
    if not parts:
        return r"$^"
    # "deu certo" também precisa pegar "deu tudo certo" (até 2 palavras no meio)
    body = r"(?:\s+\w+){0,2}\s+".join(parts)
    return rf"\b{body}\b"


def numbers_in(text: Any) -> list[str]:
    return re.findall(r"\d+", str(text or ""))


def fact_spoken(fact: Mapping[str, Any]) -> str:
    return str(fact.get("spoken") or fact.get("text")
               or fact.get("detail") or "").strip()


def fact_authorizes(fact: Mapping[str, Any]) -> bool:
    """Só fato devolvido, real e sem pendência autoriza asserção."""
    if not isinstance(fact, Mapping):
        return False
    if fact.get("simulated") or fact.get("cancelled") \
            or fact.get("pending_confirm") or fact.get("requires_confirm"):
        return False
    if fact.get("assertion") is False:
        return False
    if "ok" in fact:
        return fact.get("ok") is True
    return fact.get("assertion") is True


def fact_pending(fact: Mapping[str, Any]) -> str:
    if not isinstance(fact, Mapping):
        return ""
    return str(fact.get("pending_confirm") or "").strip()


def fact_blob(facts: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for fact in facts or ():
        if not isinstance(fact, Mapping):
            continue
        parts.append(fact_spoken(fact))
        parts.append(str(fact.get("detail") or ""))
        extra = fact.get("numbers")
        if isinstance(extra, (list, tuple)):
            parts.extend(str(n) for n in extra)
        elif extra:
            parts.append(str(extra))
    return norm(" ".join(parts))


@dataclass(frozen=True)
class MouthSpeech:
    text: str
    accepted: bool
    reason: str = ""
    template: bool = False
    missing_facts: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "accepted": self.accepted,
                "reason": self.reason, "template": self.template,
                "missing_facts": list(self.missing_facts)}


def template_for(facts: Iterable[Mapping[str, Any]] | None = None,
                 fallback: str = "") -> str:
    """Frase determinística: a boca não escolhe, o contrato escolhe."""
    facts = list(facts or ())
    pending = [fact_pending(f) for f in facts if fact_pending(f)]
    if pending:
        return f'Diga "{pending[0]}" se for isso mesmo.'
    if any(isinstance(f, Mapping) and f.get("cancelled") for f in facts):
        return CANCELLED_TEMPLATE
    authorized = [f for f in facts if fact_authorizes(f)]
    for fact in authorized:
        spoken = fact_spoken(fact)
        if spoken:
            sentence = spoken[:1].upper() + spoken[1:].rstrip(" .") + "."
            return clean(sentence)
    if facts and any(isinstance(f, Mapping) and f.get("simulated")
                     for f in facts):
        return SIMULATED_TEMPLATE
    if facts and all(isinstance(f, Mapping) and not f.get("ok", False)
                     for f in facts):
        return FAILED_TEMPLATE
    fallback_text = clean(fallback)
    return fallback_text or NEUTRAL_TEMPLATE


class MouthWriter:
    """Porta única de saída da boca: valida e devolve o texto que pode falar."""

    def __init__(self, *, max_chars: int = MAX_SPEECH_CHARS,
                 allow_simulated: bool = False):
        self.max_chars = int(max_chars)
        self.allow_simulated = bool(allow_simulated)
        self.rejected = 0
        self.templates = 0

    def _authorized(self, facts: list[Mapping[str, Any]]
                    ) -> list[Mapping[str, Any]]:
        out = []
        for fact in facts:
            if not isinstance(fact, Mapping):
                continue
            if self.allow_simulated and fact.get("simulated") \
                    and fact.get("ok") is True and not fact.get("cancelled") \
                    and not fact.get("pending_confirm"):
                out.append(fact)
            elif fact_authorizes(fact):
                out.append(fact)
        return out

    def _problem(self, sentence: str, facts: list[Mapping[str, Any]]
                 ) -> str:
        if not sentence:
            return "frase vazia"
        if len(sentence) > self.max_chars:
            return "frase longa demais"
        for bad in LEAK_WORDS:
            if bad in sentence:
                return f"vazou ferramenta/json: {bad}"
        authorized = self._authorized(facts)
        blob = fact_blob(authorized)
        hit = assertion_hit(sentence)
        if hit and not authorized:
            return f"asserção sem fato devolvido: {hit!r}"
        if hit and authorized:
            keys: set[str] = set()
            for fact in authorized:
                keys |= _keywords(fact_spoken(fact))
            sentence_norm = norm(sentence)
            if keys and not any(key in sentence_norm for key in keys):
                return f"asserção sem fato correspondente: {hit!r}"
        for number in numbers_in(sentence):
            if number not in blob:
                return f"número inventado: {number}"
        pending = [fact_pending(f) for f in facts if fact_pending(f)]
        if pending:
            body = norm(sentence)
            if "confirm" not in body:
                return "não pediu a confirmação falada"
            if not any(norm(p) in body for p in pending):
                return "frase exata de confirmação ausente"
        cancelled = any(f.get("cancelled") for f in facts
                        if isinstance(f, Mapping))
        if cancelled and hit:
            return f"asserção com pedido cancelado: {hit!r}"
        return ""

    def write(self, candidate: Any, facts: Iterable[Mapping[str, Any]] | None = None,
              *, phase: str = "complement", fallback: str = "") -> MouthSpeech:
        """Recebe o que o modelo escreveu e devolve o que a boca pode falar."""
        if phase not in ("brief", "complement"):
            raise ValueError(f"fase desconhecida: {phase!r}")
        facts = [f for f in (facts or ()) if isinstance(f, Mapping)]
        sentence = clean(candidate)
        if phase == "brief":
            reason = self._problem(sentence, []) if sentence else "frase vazia"
            if reason:
                self.rejected += 1
                self.templates += 1
                return MouthSpeech(text=clean(fallback) or BRIEF_TEMPLATE,
                                   accepted=False, reason=reason,
                                   template=True)
            return MouthSpeech(text=sentence, accepted=True)
        reason = self._problem(sentence, facts)
        if reason:
            self.rejected += 1
            self.templates += 1
            return MouthSpeech(text=template_for(facts, fallback),
                               accepted=False, reason=reason, template=True)
        return MouthSpeech(text=sentence, accepted=True)

    def say(self, candidate: Any, facts: Iterable[Mapping[str, Any]] | None,
            speak: Callable[[str], Any], *, phase: str = "complement",
            fallback: str = "") -> MouthSpeech:
        """Valida e só então entrega ao TTS — não há caminho que pule a guarda."""
        speech = self.write(candidate, facts, phase=phase, fallback=fallback)
        try:
            speak(speech.text)
        except Exception as exc:  # falha de áudio não vira fato
            return MouthSpeech(text=speech.text, accepted=speech.accepted,
                               reason=f"{speech.reason}; fala falhou: "
                                      f"{type(exc).__name__}: {exc}",
                               template=speech.template,
                               missing_facts=speech.missing_facts)
        return speech


_DEFAULT = MouthWriter()


def speak_guarded(candidate: Any, facts: Iterable[Mapping[str, Any]] | None = None,
                  *, phase: str = "complement",
                  fallback: str = "") -> MouthSpeech:
    """Atalho sem estado para o ponto de escrita existente."""
    return _DEFAULT.write(candidate, facts, phase=phase, fallback=fallback)
