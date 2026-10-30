"""
Consertar o que o reconhecedor entendeu errado — antes de alguém agir nisso.

Duas camadas, com papéis diferentes:

1. `fix_commands()` — tabela curada, local, sem modelo, instantânea. Cobre as
   palavras das quais os GATES dependem ("loga", "pausa", "youtube"), que o
   Whisper escreve de formas variadas quando o áudio é curto ("logra no
   YouTube" foi o caso real que originou isto). Onde a troca seria ambígua ela
   exige contexto por perto: "logra" só vira "loga" quando há "youtube",
   "google" ou "conta" na mesma frase, porque "logra êxito" é português
   legítimo e trocar isso mudaria o que o usuário pediu.

2. A correção por CONTEXTO da conversa não cabe numa tabela: é o modelo que já
   decide as ferramentas (`route_turn`, em core/brain.py) que devolve também a
   frase corrigida. Uma requisição, dois resultados — e o contexto é dele.

Regra comum às duas: corrigir não é reescrever. Mesmo idioma, mesmo sentido,
mesma pessoa, mesmo tempo verbal, mesmo nível de detalhe. Em dúvida, não mexer.
"""
from __future__ import annotations

import re

# (padrão, substituto, palavras de contexto obrigatórias)
# Contexto vazio = a troca é segura sozinha.
_RULES: tuple[tuple[re.Pattern, str, tuple[str, ...]], ...] = (
    # ── login ────────────────────────────────────────────────────────────────
    # The context words are listed with their own misspellings: these rules run
    # before the YouTube one, so "lógin no yotube" has to recognise "yotube" as
    # the context it is waiting for.
    (re.compile(r"\b(?:logra|lográ|logá|lógra|logre|logrei)\b", re.I),
     "loga", ("youtube", "yt", "google", "conta", "gmail", "navegador",
              "yotube", "yutube", "iutube", "ytube")),
    (re.compile(r"\b(?:lógin|lógui|logui|loging|logim|logi)\b", re.I),
     "login", ("youtube", "yt", "google", "conta", "gmail",
               "yotube", "yutube", "iutube", "ytube")),
    # ── nomes e grafias do YouTube ───────────────────────────────────────────
    (re.compile(r"\b(?:yotube|yutube|iutube|ytube|you\s*tube|youtub)\b", re.I),
     "YouTube", ()),
    (re.compile(r"\b(?:youtubr|youtbe)\b", re.I), "YouTube", ()),
    # ── transporte de música ─────────────────────────────────────────────────
    (re.compile(r"\b(?:pauza|paussa)\b", re.I), "pausa", ()),
    (re.compile(r"\b(?:musica|múzica)\b", re.I), "música", ()),
    # ── verbos que os gates reconhecem e o ouvido confunde ───────────────────
    (re.compile(r"\b(?:desl[ií]ga|disliga)\b", re.I), "desliga", ()),
    (re.compile(r"\b(?:continua|cont[íi]nua)\b", re.I), "continua", ()),
)

def fold(text: str) -> str:
    """Minúsculas sem acento — para comparar, nunca para devolver ao usuário."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# Stopword sets, not a language detector: enough to tell "the user is speaking
# Portuguese" from "the user is speaking English", which is the only decision
# this needs to make. Anything else returns "" and no instruction is added.
_PT_WORDS = {"que", "nao", "voce", "eu", "ele", "ela", "isso", "aqui", "tela",
             "musica", "para", "com", "uma", "tem", "esta", "fazer", "quero",
             "pode", "meu", "minha", "voce", "abre", "abrir", "ver", "veja",
             "por", "favor", "obrigado", "obrigada", "nao", "sim", "como"}
_EN_WORDS = {"the", "you", "what", "is", "are", "to", "of", "and", "this",
             "that", "with", "for", "can", "please", "there", "my", "your",
             "would", "could", "should", "have", "was", "were", "about"}


def detect_lang(text: str) -> str:
    """'pt' / 'en' / '' — a guess strong enough to steer the answer's language."""
    words = re.findall(r"[a-zà-ÿ]+", fold(text))
    if len(words) < 2:
        return ""
    pt = sum(1 for w in words if w in _PT_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    if pt >= 2 and pt > en:
        return "pt"
    if en >= 2 and en > pt:
        return "en"
    return ""


def language_directive(text: str) -> str:
    """A short line to append to the END of the system prompt for this turn.

    prompt.txt already says "answer in the language of the user's last message",
    but that paragraph sits in the middle of ~10k characters of English
    instructions, and a 7B drifts back to English — it answered "It seems like
    you're trying to interact with a folder" to a Portuguese speaker. Repeating
    the rule as the last thing the model reads is what small models obey.
    """
    lang = detect_lang(text)
    if lang == "pt":
        return ("\n\n[REPLY LANGUAGE — OVERRIDES EVERYTHING ABOVE] The user's "
                "message is in Brazilian Portuguese. Write EVERY sentence in "
                "Brazilian Portuguese, including acknowledgements and tool "
                "explanations. Do not answer in English.")
    if lang == "en":
        return ("\n\n[REPLY LANGUAGE] The user's message is in English. Reply "
                "in English, every sentence.")
    return ""


def fix_commands(text: str) -> str:
    """Aplica a tabela. Devolve o texto novo (ou o mesmo, se nada casou)."""
    out = str(text or "")
    if not out.strip():
        return out
    folded = fold(out)
    for pattern, replacement, needs in _RULES:
        if needs and not any(w in folded for w in needs):
            continue
        # Só substitui o que casou; o resto da frase fica byte a byte igual.
        new = pattern.sub(replacement, out)
        if new != out:
            out = new
            folded = fold(out)
    return out


def plausible(raw: str, fixed: str) -> bool:
    """A correção do modelo é aceitável?

    O modelo pode "consertar" demais: responder, resumir, traduzir. Duas provas
    baratas pegam os três casos. O tamanho, porque resposta e resumo mudam o
    comprimento muito mais que uma palavra ouvida errado. E a semelhança palavra
    a palavra, porque uma recusa educada ("Não posso fazer isso, senhor.") tem
    tamanho parecido com a pergunta e passaria sozinha pelo teste de tamanho.
    """
    import difflib

    a = str(raw or "").strip()
    b = str(fixed or "").strip()
    if not b:
        return False
    if not a:
        return True
    ratio = len(b) / max(1, len(a))
    if not (0.6 <= ratio <= 1.8):
        return False
    wa, wb = fold(a).split(), fold(b).split()
    if not wa or not wb:
        return False
    # Palavra a palavra: uma correção troca uma ou duas; uma reescrita troca a
    # maior parte. 0.45 deixa passar "clip"→"clipe" e barra "toca X"→"não posso".
    same = difflib.SequenceMatcher(None, wa, wb).ratio()
    return same >= 0.45
