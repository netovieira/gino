"""Política de fala a partir de transcrição parcial.

Separar COMEÇAR A PROCESSAR de COMEÇAR A FALAR é o ponto do stream: cada
parcial vai ao orquestrador para pré-aquecer rota/contexto/modelo, mas só uma
hipótese ESTÁVEL e de NÍVEL BAIXO pode virar fala antes da transcrição final.

Nível baixo é:

* saudação ("oi", "bom dia", "e aí");
* reconhecimento ("entendi", "certo", "beleza");
* continuação de conversa ("pode continuar", "e depois", "voltei").

Nunca sai de parcial: pedido com ação (abrir, tocar, reiniciar, enviar),
conteúdo factual (hora, número, preço, notícia), pergunta de conteúdo ou
qualquer coisa ambígua que possa virar ordem. Nesses casos o silêncio curto é
melhor que uma frase errada; a frase final vem do fluxo normal.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

LEVEL_LOW = "low"
LEVEL_HIGH = "high"
LEVEL_NONE = "none"

FORBIDDEN_RE = re.compile(
    r"\b(abre|abrir|toca|tocar|poe|coloca|bota|pausa|para|baixa|abaixa|"
    r"aumenta|sobe|muda|troca|reinicia|reiniciar|desliga|desligar|apaga|"
    r"apagar|envia|enviar|manda|mandar|instala|desinstala|exclui|remove|"
    r"cria|agenda|lembra|agendar|faz|executa|roda|compra|paga|transfere)\b")
FACT_RE = re.compile(
    r"\b(que horas|horas sao|que dia|qual|quanto|quantos|quando|onde|quem|"
    r"por que|porque|como|status|temperatura|clima|noticia|noticias|agenda|"
    r"preco|valor|saldo|resultado|relatorio|processo|arquivo|log)\b")

GREETING_PREFIX_RE = re.compile(
    r"^(oi|ola|opa|e ai|bom dia|boa tarde|boa noite|tudo bem|tudo bom)\b")
GREETING_ONLY_RE = re.compile(
    r"^(oi|ola|opa|e ai|bom dia|boa tarde|boa noite|tudo bem|tudo bom)"
    r"( tudo bem| tudo bom)?( por ai| com voce)?( criador| gino)?$")
ACK_RE = re.compile(
    r"^(entendi|certo|beleza|ta bom|ta certo|ok|okay|claro|pode ser|"
    r"combinado|perfeito|isso|uhum|hum|aham)$")
CONTINUE_RE = re.compile(
    r"^(continua|pode continuar|continue|e depois|e ai|voltei|estou aqui|"
    r"um\s+instante|espera|calma|ja vou|so\s+um\s+instante|calma ai)$")


def norm(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    # Pontuação interna vira espaço: "e aí, tudo bem com você?" precisa ser
    # reconhecida como saudação, não como seis tokens por causa da vírgula.
    value = re.sub(r"[,!?;:]+", " ", value)
    return re.sub(r"\s+", " ", value).strip(" .")


@dataclass(frozen=True)
class PartialDecision:
    release: bool
    level: str
    reason: str
    phrase: str = ""
    route: str = ""


def classify(text: str) -> tuple[str, str]:
    """Classifica a hipótese: (nível, motivo)."""
    n = norm(text)
    if not n:
        return LEVEL_NONE, "vazio"
    if re.search(r"\d", n):
        return LEVEL_HIGH, "número/fato"
    if FORBIDDEN_RE.search(n):
        return LEVEL_HIGH, "ação"
    if FACT_RE.search(n):
        return LEVEL_HIGH, "conteúdo factual"
    if GREETING_ONLY_RE.search(n):
        return LEVEL_LOW, "saudação"
    if ACK_RE.search(n):
        return LEVEL_LOW, "reconhecimento"
    if CONTINUE_RE.search(n):
        return LEVEL_LOW, "continuação"
    # Conta palavras, não tokens separados por espaço: "e aí, tudo bem com
    # você?" tem 5 palavras e é saudação, mesmo com a vírgula colada.
    if (GREETING_PREFIX_RE.search(n)
            and len(re.findall(r"[^\W_]+", n, re.UNICODE)) <= 5):
        return LEVEL_LOW, "saudação"
    return LEVEL_HIGH, "conteúdo não trivial"


def phrase_for(text: str, *, assistant_name: str = "Gino") -> str:
    """Frase determinística de nível baixo; nunca gerada por modelo."""
    n = norm(text)
    if GREETING_PREFIX_RE.search(n):
        return f"Oi, criador. {assistant_name} aqui."
    if CONTINUE_RE.search(n):
        return "Certo."
    return "Entendi."


def decide(text: str, *, stable: bool, already_released: bool,
           min_chars: int, min_words: int,
           assistant_name: str = "Gino") -> PartialDecision:
    """Decide se a hipótese parcial pode ser falada agora."""
    n = norm(text)
    level, reason = classify(text)
    if already_released:
        return PartialDecision(False, level, "já liberado")
    if not stable:
        return PartialDecision(False, level, "hipótese ainda mudando")
    if level != LEVEL_LOW:
        return PartialDecision(False, level, reason)
    if len(n) < max(1, min_chars) or len(n.split()) < max(1, min_words):
        return PartialDecision(False, level, "curta demais")
    return PartialDecision(True, level, reason,
                           phrase=phrase_for(text, assistant_name=assistant_name))
