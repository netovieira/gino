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

# ── Anúncio de ação/ferramenta: PROIBIDO na boca ────────────────────────────
#
# O dono ouviu "Abrindo o Terminal." em voz alta sem ter pedido nada (turno 22,
# origem "boca") e a regra é literal: a boca nunca descreve a ação, a
# ferramenta ou o andamento. Ela pode dar conteúdo (um fato autorizado), pedir
# uma confirmação ou ficar calada — nunca narrar o que "está fazendo".
#
# O detector é deliberadamente léxico e conservador: a lista de gerúndios é
# explícita (não um regex de "-ndo", que pegaria "quando"/"entendendo"), e a
# checagem de fato autorizado abaixo deixa a boca RELATAR "Queen tocando no
# Spotify" quando esse é o fato que um trabalhador devolveu. Número, nome
# próprio ou ferramenta continuam cobertos pelas outras regras.
ACTION_GERUNDS = (
    "abrindo", "fechando", "pondo", "colocando", "tocando", "pausando",
    "ajustando", "aumentando", "diminuindo", "anotando", "lembrando",
    "procurando", "pesquisando", "conferindo", "verificando", "olhando",
    "checando", "executando", "rodando", "iniciando", "comecando",
    "baixando", "salvando", "enviando", "mandando", "escrevendo",
    "digitando", "ligando", "desligando", "reiniciando", "atualizando",
    "instalando", "esperando", "aguardando", "preparando", "organizando",
    "arrumando", "carregando", "processando", "sincronizando",
)
ACTION_INFINITIVES = (
    "abrir", "fechar", "por", "colocar", "tocar", "pausar", "ajustar",
    "aumentar", "diminuir", "anotar", "lembrar", "procurar", "pesquisar",
    "conferir", "verificar", "olhar", "checar", "executar", "rodar",
    "iniciar", "comecar", "baixar", "salvar", "enviar", "mandar",
    "escrever", "digitar", "ligar", "desligar", "reiniciar", "atualizar",
    "instalar", "esperar", "aguardar", "preparar", "organizar", "arrumar",
    "carregar", "processar",
)
# "vou abrir", "vamos tocar", "irei reiniciar" — intenção, não conteúdo.
_FUTURE_RE = re.compile(
    r"\b(?:vou|vamos|irei|iremos)\s+(?:ja\s+)?(?:"
    + "|".join(ACTION_INFINITIVES) + r")\b")
# Andamento puro: nenhuma dessas palavras entrega conteúdo ao dono.
_PROGRESS_RE = re.compile(
    r"\b(?:aguarde|aguenta|espera|instante|"
    r"ja volto|calma que ja|em andamento|processando)\b")
_GERUND_RE = re.compile(r"\b(?:" + "|".join(ACTION_GERUNDS) + r")\b")


def announcement(text: str) -> str:
    """A palavra/expressão que faz da frase um anúncio de ação, ou ''.

    "Abrindo o Terminal." -> 'abrindo'; "Vou pôr Queen" -> 'vou por';
    "aguarde, criador" -> 'aguarde'. Conteúdo ("Volume em 40%.",
    "Reiniciar é irreversível.") não casa.
    """
    n = norm(text)
    for pattern in (_FUTURE_RE, _PROGRESS_RE, _GERUND_RE):
        match = pattern.search(n)
        if match:
            return match.group(0)
    return ""


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

    # Lei do dono (04/11/2026): a boca NUNCA anuncia a ação/ferramenta nem o
    # andamento ("Abrindo o Terminal.", "vou abrir", "executando", "espera").
    # Vale nas duas fases. No complemento, um gerúndio que está no FATO
    # autorizado é relato do trabalhador ("Queen tocando no Spotify") e passa.
    hit_announce = announcement(text)
    if hit_announce:
        allowed_fact = False
        if phase == "complement":
            for fact in facts:
                if not fact.get("assertion"):
                    continue
                spoken = norm(str(fact.get("spoken") or ""))
                if hit_announce in spoken:
                    allowed_fact = True
                    break
        if not allowed_fact:
            return False, f"anúncio de ação/andamento: {hit_announce!r}"

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
    # FASE3-TASK3: consulta o verificador externo antes de aceitar a asserção.
    allow = any(bool(f.get("assertion")) for f in facts)
    if not allow:
        hit = has_assertion(text)
        if hit:
            return False, f"asserção sem fato autorizado: {hit!r}"

    # Verifica externamente os fatos que carregam recibo de tool call.
    verified = _verify_facts(facts)
    if verified is False:
        return False, "verificação externa do resultado falhou"

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
    """Frase determinística quando o modelo é rejeitado ou falha.

    O fallback do roteador é um template e obedece à mesma lei: se ele narra a
    ação ("Abrindo o WhatsApp.", "Vou pôr Queen", "Conferindo a hora."), a
    boca fica CALADA — devolve string vazia. Silêncio é melhor que anunciar;
    o cérebro responde logo em seguida e a boca nunca o espera.
    """
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
    safe = str(fallback or "").strip()
    if safe and announcement(safe):
        return ""
    return safe


# ── FASE3-TASK3: verificacao externa de fatos ─────────────────────────────

def _verify_facts(facts: list[dict[str, Any]]) -> bool | None:
    """Consulta o verificador externo para fatos com recibo de tool call.

    Retorna:
      True  — todos os fatos verificaveis passaram (ou nenhum era verificavel).
      False — pelo menos um fato falhou na verificacao externa.
      None  — nenhum fato carregava metadados de tool call (call_id + tool).

    A verificacao eh sincrona e deliberadamente rapida (< 50 ms para arquivos).
    """
    verifier = None
    try:
        from core.action_verifier import get_verifier
        verifier = get_verifier()
    except Exception:
        return None

    checked = 0
    for fact in facts:
        if not fact.get("assertion"):
            continue
        call_id = fact.get("call_id")
        tool = fact.get("tool")
        if not call_id or not tool:
            continue
        args = fact.get("tool_args") or {}
        result = fact.get("tool_result")
        ok = verifier.verify_sync(str(call_id), str(tool),
                                  args if isinstance(args, dict) else {},
                                  result)
        checked += 1
        if not ok:
            return False
    return checked > 0 or None
