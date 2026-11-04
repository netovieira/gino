"""Guardas da boca — enforcement da lei (a) e da lei (b) em código.

A boca é um modelo local de 7b. Ela fala bem e inventa resultado; por isso o
brief sai sempre com ``facts=[]`` e a frase complementar só pode afirmar o que
um trabalhador devolveu. Quando o modelo desobedece, a frase é rejeitada e o
template determinístico assume — nunca se confia no texto gerado.

O guard também recusa:

* número que não aparece em fato nenhum (hora inventada, "40%");
* ferramenta/caminho/JSON vazando para a fala;
* frase de confirmação sem o ``pending_confirm`` exato (lei b).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

ASSERT_WORDS = (
    "aberto", "abri", "abriu", "feito", "pronto", "prontinho", "tocando",
    "anotado", "enviado", "concluido", "concluida", "deu certo", "ja esta",
    "entrei", "reiniciado", "executado", "ligado", "desligado", "pausado",
    "reiniciei", "fiz", "fechado", "aberta",
)

LEAK_WORDS = ("media_control", "open_app", "reminder", "restart_assistant",
              "{", "}", "mcp__", "jarvis_call")

_STOP = {
    "para", "pelo", "pela", "com", "sem", "mais", "menos", "esta", "estava",
    "esse", "essa", "isso", "aqui", "agora", "muito", "pouco", "sobre",
}


def norm(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", value).strip()


def has_assertion(text: str) -> str:
    """Devolve a palavra de asserção encontrada, ou string vazia."""
    n = norm(text)
    for word in ASSERT_WORDS:
        if re.search(rf"\b{re.escape(norm(word))}\b", n):
            return word
    return ""


def numbers(text: str) -> list[str]:
    return re.findall(r"\d+", str(text or ""))


def guard(text: str, facts: list[dict[str, Any]] | None,
          phase: str) -> tuple[bool, str]:
    """Valida uma fala da boca contra os fatos disponíveis."""
    n = norm(text)
    facts = list(facts or [])
    if not n:
        return False, "frase vazia"

    if phase == "brief":
        hit = has_assertion(text)
        if hit:
            return False, f"asserção no brief: {hit!r}"
        num = numbers(text)
        if num:
            return False, f"número no brief: {num[0]}"
        for bad in LEAK_WORDS:
            if bad in text:
                return False, f"vazou ferramenta/json: {bad}"
        return True, ""

    if phase != "complement":
        return False, f"fase desconhecida: {phase!r}"

    # Lei (a): só afirma com fato autorizado (ok, não simulado, não pendente).
    allow = any(bool(f.get("assertion")) for f in facts)
    if not allow:
        hit = has_assertion(text)
        if hit:
            return False, f"asserção sem fato autorizado: {hit!r}"

    pending = [str(f.get("pending_confirm")) for f in facts
               if f.get("pending_confirm")]
    if pending:
        if "confirm" not in n:
            return False, "não pediu a confirmação falada"
        if not any(norm(p) in n for p in pending):
            return False, "frase exata de confirmação ausente"

    blob = norm(" ".join(
        str(f.get("spoken") or "") + " " + str(f.get("detail") or "")
        for f in facts))
    for num in numbers(text):
        if num not in blob:
            return False, f"número inventado: {num}"

    for bad in LEAK_WORDS:
        if bad in text:
            return False, f"vazou ferramenta/json: {bad}"
    return True, ""


def fact_keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9%:]+", norm(text))
    return {w for w in words if len(w) > 4 and w not in _STOP}


def ensure_facts(sentence: str, facts: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """Anexa fatos que o modelo omitiu; a boca não escolhe o que entregar."""
    n = norm(sentence)
    missing: list[str] = []
    for fact in facts:
        if not fact.get("assertion"):
            continue
        spoken = str(fact.get("spoken") or "").strip()
        words = fact_keywords(spoken)
        if spoken and words and not any(w in n for w in words):
            missing.append(spoken)
    for spoken in missing:
        sentence = sentence.rstrip(" .") + " e " + spoken + "."
        break  # uma frase curta; o resto fica no log, não vira ladainha
    return sentence, missing


def template_for(facts: list[dict[str, Any]] | None,
                 fallback: str = "") -> str:
    """Frase determinística quando o modelo é rejeitado ou falha."""
    facts = list(facts or [])
    pending = [str(f.get("pending_confirm")) for f in facts
               if f.get("pending_confirm")]
    if pending:
        return f'Diga "{pending[0]}" se for isso mesmo.'
    if any(f.get("cancelled") for f in facts):
        return "Nada foi executado, criador; o pedido ficou no meio."
    spoken = [str(f.get("spoken") or "").strip() for f in facts
              if f.get("assertion") and str(f.get("spoken") or "").strip()]
    if spoken:
        return spoken[0][:1].upper() + spoken[0][1:].rstrip(".") + "."
    return str(fallback or "").strip()
