"""Sanitização da fala: ferramenta não vira fala — intenção humana.

O texto que vai para o TTS nasce de um modelo que às vezes narra o próprio
trabalho: nome de ferramenta ("ferramenta — Bash grep …"), caminho de
arquivo, linha de comando, JSON de argumento e até a frase inteira em inglês.
O dono ouve isso como lixo. Esta camada é pura (regex + léxico pequeno, sem
modelo local, sem GPU) e roda só no texto destinado ao TTS.

Regra de ouro: **não corta mudo**. O trecho ilegível é trocado por uma frase
de intenção humana ("deixa eu ver no log", "vou testar isso", "deixa eu
verificar isso"), e o que era legível na mesma frase continua na fala. Frase
limpa passa byte a byte, sem reformatação.

Tipos detectados (nomes usados na flag ``sanitized=`` do Activity Log):

    tool     nome de ferramenta (Bash, Grep, Read, qoder_job, mcp__…)
    path     caminho de arquivo ou pasta (/home/…, /projects/…, ./repo/…,
             ``pasta/arquivo``) ou arquivo com extensão
    phone    telefone/identificador com DDI/DDD (+55 17 9…, 5517…,
             ``5517…@s.whatsapp.net``) — nunca vira número falado
    command  linha de comando ($ …, grep …, pytest …)
    json     JSON/XML/etiqueta solta ({…}, […], <…>)
    lang     frase em outro idioma quando a fala é português

Quem grava o flag é o main.py, no sufixo ``[audio={...}]`` (core/activity.py);
este módulo não escreve em disco e não chama rede.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Sanitized", "sanitize", "strip_speaker", "KINDS"]

# Ordem fixa na flag `sanitized=`: deixa o log comparável entre linhas.
KINDS = ("tool", "path", "phone", "command", "json", "lang")

# ── ferramentas ────────────────────────────────────────────────────────────

_TOOL_NAMES = (
    r"Bash|Grep|Glob|Read|Edit|Write|Agent|WebFetch|WebSearch|TodoWrite|"
    r"Task|NotebookEdit|Qoder|qoder|qoder_job|qoder_live|task_notification|"
    r"whatsapp_control|screen_process|save_memory|memory_search|"
    r"jarvis_call|tool_calls?|mcp__[A-Za-z0-9_]+")

# O artigo que vinha antes do nome entra na troca: "o Bash" → "o terminal".
_TOOL_RE = re.compile(
    r"(?:(?:a|o|as|os|um|uma)\s+)?(?<![\w])(?:" + _TOOL_NAMES + r")(?![\w])")

# "usar a ferramenta qoder_job" → "usar a tarefa": o rótulo some junto.
_TOOL_LABEL_RE = re.compile(
    r"\b(?:a|o)\s+(?:ferramenta|tool)\s+(?=" + _TOOL_NAMES + r")")

_TOOL_WORDS = {
    "Bash": "o terminal",
    "Grep": "a busca",
    "Glob": "a busca",
    "Read": "a leitura",
    "Edit": "a edição",
    "Write": "a edição",
    "Agent": "o agente auxiliar",
    "Task": "a tarefa",
    "WebFetch": "a consulta",
    "WebSearch": "a pesquisa",
    "Qoder": "o assistente",
    "qoder": "o assistente",
    "qoder_job": "a tarefa",
    "qoder_live": "o assistente",
    "task_notification": "o aviso",
    "whatsapp_control": "o WhatsApp",
    "screen_process": "a captura de tela",
    "save_memory": "a anotação",
    "memory_search": "a memória",
}

# ── caminhos ───────────────────────────────────────────────────────────────

_EXT = (r"py|pyc|js|mjs|ts|tsx|jsx|json|jsonl|md|txt|log|yml|yaml|toml|ini|"
        r"cfg|conf|sh|bat|html|css|sql|db|csv|env")

# Preposição que vinha antes do caminho entra na troca: "em /projects/x" →
# "na pasta", "de /projects/x" → "da pasta". Só uma palavra, como no original.
_PATH_PREPS = r"em|no|na|de|do|da|para|pelo|pela"

# Pastas de projeto que aparecem sem barra inicial ("projects/therotv").
# A lista é curta de propósito: "e/ou", "24/7" e "km/h" não são caminho.
_REL_DIRS = (r"projects|repos|workspace|src|core|tests|skills|logs|memory|"
             r"docs|actions|plugins|tools|scripts|dashboard|config")

_PATH_RE = re.compile(
    r"(?<![\w/])(?:(?:" + _PATH_PREPS + r")\s+)?(?:"
    # Qualquer caminho absoluto comum: /projects/therotv, /home/…, /opt/…
    r"(?:~|\.{1,2})?/(?:[\w.@%+~-]+/)+[\w.@%+~-]*"
    r"|~/(?:[\w.@%+~-]+)(?:/[\w.@%+~-]*)*"
    r"|(?:\.{1,2}/)[\w.@%+~/-]{2,}"
    # Caminho relativo de projeto: projects/therotv, core/narration
    r"|(?:" + _REL_DIRS + r")/[\w.@%+~-]+(?:/[\w.@%+~-]+)*"
    r"|(?:[\w.-]+/)+[\w.-]+\.(?:" + _EXT + r")\b"
    r"|[\w-]+\.(?:" + _EXT + r")\b"
    r")")

# ── telefone / identificador ───────────────────────────────────────────────

# Telefone com DDI/DDD, JID do WhatsApp e sequência longa de dígitos (número
# colado no log/argumento). Valor monetário com pontos ("380.789,00"), data
# ("02/11/2026"), hora ("16:04") e linha de log ("3190") NÃO casam: exigimos
# +DDI, ou formato (DDD) 9…-…, ou ao menos 11 dígitos seguidos.
_PHONE_RE = re.compile(
    r"(?<![\w/])(?:(?P<prep>em|no|na|para|pra|pro|ao|à|a|de|do|da|com)\s+)?"
    r"(?P<num>(?:"
    r"\+\s*\d{1,3}(?:[\s.\-()]*[\d*]){8,16}"
    r"|\d{11,}"
    r"|\(?\d{2}\)?[\s.\-]+(?:9[\s.\-]?)?\d{4}[\s.\-]?\d{4}"
    r")(?:@(?:s\.whatsapp\.net|c\.us|g\.us))?)(?![\w])")

# A fala nomeia o contato quando ele já está na frase; o número vira sempre
# "destinatário". A preposição capturada evita "chega em o destinatário".
_PHONE_PREPS = {
    "em": "ao destinatário",
    "no": "no destinatário",
    "na": "no destinatário",
    "para": "para o destinatário",
    "pra": "para o destinatário",
    "pro": "para o destinatário",
    "ao": "ao destinatário",
    "à": "ao destinatário",
    "a": "ao destinatário",
    "de": "do destinatário",
    "do": "do destinatário",
    "da": "do destinatário",
    "com": "com o destinatário",
}

# Troca com a preposição já consumida: mantém a frase em português natural.
_PATH_DIR_WORDS = {
    "em": "na pasta", "no": "na pasta", "na": "na pasta",
    "de": "da pasta", "do": "da pasta", "da": "da pasta",
    "para": "para a pasta", "pelo": "pela pasta", "pela": "pela pasta",
}
_PATH_FILE_WORDS = {
    "em": "no arquivo", "no": "no arquivo", "na": "no arquivo",
    "de": "do arquivo", "do": "do arquivo", "da": "do arquivo",
    "para": "para o arquivo", "pelo": "pelo arquivo", "pela": "pelo arquivo",
}

# ── comandos ───────────────────────────────────────────────────────────────

_COMMANDS = (r"grep|rg|pytest|pip3?|npm|yarn|docker|curl|wget|bash|zsh|"
             r"chmod|chown|ls|cat|sed|awk|find|make|git|python3?|mypy|ruff|"
             r"uv|poetry")

_DOLLAR_RE = re.compile(
    r"\$\s+(?=(?:" + _COMMANDS + r")\b)[^\n]{1,160}")
_CMD_TOKEN_RE = re.compile(
    r"(?<![\w/.-])(?:" + _COMMANDS + r")(?![\w/.-])")

_CMD_WORDS = {
    "pytest": "os testes",
    "grep": "a busca",
    "rg": "a busca",
    "ls": "a lista",
    "git": "o repositório",
    "pip": "os pacotes",
    "pip3": "os pacotes",
    "npm": "os pacotes",
    "yarn": "os pacotes",
    "python": "o programa",
    "python3": "o programa",
    "bash": "o terminal",
    "zsh": "o terminal",
    "docker": "os contêineres",
    "curl": "a consulta",
    "wget": "o download",
    "find": "a busca",
    "cat": "o conteúdo",
    "sed": "o ajuste",
    "awk": "o filtro",
    "make": "a montagem",
    "mypy": "a checagem",
    "ruff": "a checagem",
    "chmod": "a permissão",
    "chown": "a permissão",
}

# ── JSON / XML / etiqueta ──────────────────────────────────────────────────

_JSON_RE = re.compile(r"\{[^{}\n]{2,400}\}|\[[^\[\]\n]{1,300}\]")
_TAG_RE = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s+[^<>\n]{0,120})?/?>")

# ── idioma ─────────────────────────────────────────────────────────────────

# Léxico mínimo e sem ambiguidade com o português: "a", "as", "no", "do",
# "da", "os", "e", "data" e "status" ficam fora de propósito, porque são
# palavras legítimas em pt-BR e derrubariam frase limpa.
_EN_WORDS = frozenset("""
the is are was were be been being to of in on for with and or but not this
that these those it its you your we our they their he she his her i can will
would should could have has had does did failed failure error errors file
files tool tools task tasks running done started start warning please sorry
unable cannot found missing try again now here there when what which how why
all any some more less into by if then so yes ready output success build test
tests server response request result message okay
""".split())

_PT_WORDS = frozenset("""
não uma dos das que por mais isso isto você está estão são foi ser mas como
quando onde aqui agora então também muito bem tudo nada sim obrigado pronto
deixa vou ver teste testes arquivo arquivos falhou erro resultado tarefa
tarefas ferramenta executar abrir fechar resposta consegui consigo estou tem
tenho fazer feito rodando terminou concluí olha veja mostra log pasta
""".split())

_ACCENT_RE = re.compile(r"[ãõçáéíóúâêôàü]", re.I)

# Rótulos de log que às vezes entram na fala ("ferramenta — …"). Só somem
# quando o trecho também tinha vazamento; a palavra sozinha é fala legítima.
_LABEL_RE = re.compile(
    r"^\s*(?:ferramenta|tool|comando|command|saída|output|log)\s*[—–:-]\s*",
    re.I)

# ── nome do falante ────────────────────────────────────────────────────────

# A assinatura (`*Gino:*`, `Gino:`, `Gino —`, `[Gino]`) é coisa de mensagem
# ESCRITA. Na fala ela nunca entra: a voz já diz quem está falando.
_MARK = r"[*_~`\s]*"


def strip_speaker(text: str, names=()) -> str:
    """Tira o nome do falante do começo da fala; o resto volta intacto.

    ``names`` são os nomes que assinam mensagem escrita (Gino, Aline,
    Arnaldo...). Sem nome conhecido na frente, o texto volta byte a byte —
    a função nunca reformata fala limpa.
    """
    raw = str(text or "")
    body = raw.strip()
    known = [str(n or "").strip() for n in (names or ())]
    known = [n for n in known if n]
    if not body or not known:
        return raw
    alts = "|".join(re.escape(n) for n in sorted(known, key=len, reverse=True))
    pattern = re.compile(
        r"^(?:"
        r"\[" + _MARK + r"(?:" + alts + r")" + _MARK + r"\]"
        r"|" + _MARK + r"(?:" + alts + r")" + _MARK + r"(?::|[-–—])"
        r")" + _MARK,
        re.IGNORECASE)
    match = pattern.match(body)
    if not match:
        return raw
    rest = body[match.end():].lstrip(" \t\r\n")
    return rest or raw      # só o nome sozinho não é assinatura de nada


# ── frase de intenção por tipo dominante ───────────────────────────────────

_PHRASES = {
    "command": "Vou testar isso.",
    "path": "Deixa eu ver no log.",
    "phone": "Aviso o destinatário.",
    "tool": "Deixa eu verificar isso.",
    "json": "Pronto.",
    "lang": "Deixa eu verificar isso.",
}

_STOP = frozenset("""
deixa vou ver verificar testar isso isto no na o a os as um uma em para que
e de do da com
""".split())


@dataclass(frozen=True)
class Sanitized:
    """Resultado da sanitização de uma fala."""

    text: str                     # o que vai para o TTS
    kinds: tuple[str, ...]        # tipos detectados, na ordem de KINDS
    changes: tuple[tuple[str, str], ...] = ()   # (trecho original, troca)


def sanitize(text: str, lang: str = "pt") -> Sanitized:
    """Devolve a fala sanitizada e os tipos de vazamento encontrados.

    ``lang`` é o idioma-alvo da fala (o do dono). Com ``lang != "pt"`` a
    detecção de idioma estrangeiro fica desligada; os outros tipos continuam.
    Texto sem vazamento volta idêntico — a comparação é byte a byte.
    """
    raw = str(text or "")
    if not raw.strip():
        return Sanitized(raw, (), ())

    pieces: list[str] = []
    kinds: set[str] = set()
    changes: list[tuple[str, str]] = []
    previous_generated = ""

    # Ponto só separa frase quando vem espaço depois: `main.py`,
    # `activity.log` e `3.14` continuam inteiros.
    for sentence in re.split(r"(?<=[.!?…])\s+", raw):
        if not sentence.strip():
            pieces.append(sentence)
            continue
        new, found, subs = _sanitize_sentence(sentence, lang)
        if not found:
            pieces.append(sentence.strip())
            previous_generated = ""
            continue
        kinds.update(found)
        changes.extend(subs)
        # A mesma frase de intenção repetida emendada soa a robô.
        if new == previous_generated and new in _PHRASES.values():
            continue
        pieces.append(new.strip())
        previous_generated = new

    if not kinds:
        return Sanitized(raw, (), ())
    joined = " ".join(piece for piece in pieces if piece)
    joined = re.sub(r"\s+", " ", joined).strip()
    ordered = tuple(k for k in KINDS if k in kinds)
    return Sanitized(joined, ordered, tuple(changes))


def _sanitize_sentence(sentence: str, lang: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """Sanitiza uma frase; devolve (fala, tipos, trocas)."""
    found: list[str] = []
    subs: list[tuple[str, str]] = []
    work = sentence

    def _swap(name: str, pattern: re.Pattern, make) -> None:
        nonlocal work

        def repl(m: re.Match) -> str:
            original = m.group(0)
            change = make(m)
            if change == original:
                return original
            if name not in found:
                found.append(name)
            subs.append((original, change))
            return change

        work = pattern.sub(repl, work)

    # Marcação sai primeiro: é ela que fazia "<tool>Read</tool>" parecer
    # inglês antes de qualquer outra checagem.
    _swap("json", _JSON_RE, lambda m: "")
    _swap("json", _TAG_RE, lambda m: "")

    # Uma frase inteira em inglês quando o dono fala português não tem
    # "parte legível" para preservar: vira uma frase de intenção.
    if lang == "pt" and _foreign(work):
        phrase = _PHRASES["lang"]
        return phrase, found + ["lang"], subs + [(work.strip(), phrase)]

    _swap("command", _DOLLAR_RE, _dollar_repl)
    _swap("command", _CMD_TOKEN_RE, lambda m: _CMD_WORDS.get(m.group(0), "o comando"))
    _swap("phone", _PHONE_RE, _phone_repl)
    _swap("path", _PATH_RE, _path_repl)
    _swap("tool", _TOOL_LABEL_RE, lambda m: "")
    _swap("tool", _TOOL_RE, _tool_repl)

    if not found:
        return sentence, [], []

    if _LABEL_RE.match(work):
        work = _LABEL_RE.sub("", work, count=1)

    work = _tidy(work)
    # Só vale a troca inline quando sobrou prosa de verdade *fora* dos
    # vazamentos; um resto que é só a casca ("ferramenta —") vira frase.
    if not _readable(_without_leaks(sentence)):
        phrase = _phrase_for(found, sentence)
        return phrase, found, subs + [(sentence.strip(), phrase)]
    return work, found, subs


def _phrase_for(found: list[str], sentence: str) -> str:
    """Frase de intenção pela pista do texto, não só pelo tipo."""
    low = sentence.lower()
    if "command" in found or "lang" in found:
        if any(word in low for word in ("grep", "rg ", "journal", " log")):
            return "Deixa eu ver no log."
        if any(word in low for word in ("test", "pytest", "check", "ruff",
                                        "mypy", "build")):
            return "Vou testar isso."
        if "command" in found:
            return "Vou rodar isso."
    return _PHRASES[_dominant(found)]


def _without_leaks(sentence: str) -> str:
    """A frase sem os trechos ilegíveis — para medir o que é prosa."""
    work = _JSON_RE.sub(" ", sentence)
    work = _TAG_RE.sub(" ", work)
    work = _DOLLAR_RE.sub(" ", work)
    work = _CMD_TOKEN_RE.sub(" ", work)
    work = _PHONE_RE.sub(" ", work)
    work = _PATH_RE.sub(" ", work)
    work = _TOOL_LABEL_RE.sub(" ", work)
    work = _TOOL_RE.sub(" ", work)
    return _LABEL_RE.sub(" ", work)


def _dollar_repl(m: re.Match) -> str:
    line = m.group(0).lower()
    if any(word in line for word in ("test", "pytest", "mypy", "ruff", "check")):
        return "vou testar isso"
    if any(word in line for word in ("grep", "rg ", " journal", " log")):
        return "deixa eu ver no log"
    return "vou rodar isso"


def _path_repl(m: re.Match) -> str:
    raw = m.group(0)
    if "/" not in raw:
        # Nome de arquivo solto ("o arquivo main.py"): o substantivo já está
        # na frase, então o nome some em vez de virar "o arquivo" duas vezes.
        return ""
    low = raw.lower()
    if "log" in low:
        return "no log"
    # A preposição pode ter entrado no casamento ("em /projects/x"): a troca
    # já sai com a contração certa. Sem preposição, devolve o substantivo.
    parts = low.split()
    lead = parts[0] if len(parts) > 1 else ""
    if re.search(r"\.(?:" + _EXT + r")\b", low):
        return _PATH_FILE_WORDS.get(lead, "o arquivo")
    return _PATH_DIR_WORDS.get(lead, "a pasta")


def _phone_repl(m: re.Match) -> str:
    prep = (m.groupdict().get("prep") or "").lower()
    # "o contato 5517…" já tem o substantivo: repetir "o destinatário" soaria
    # "o contato o destinatário". Nesse caso o número simplesmente some.
    if not prep and re.search(
            r"(?:contato|número|numero|conta|identificador)\s+$",
            m.string[:m.start()], re.IGNORECASE):
        return ""
    change = _PHONE_PREPS.get(prep, "o destinatário")
    # No começo da frase, "o destinatário" precisa de maiúscula.
    if m.start() == 0 and not m.string[:m.start()].strip():
        change = change[:1].upper() + change[1:]
    return change


def _tool_repl(m: re.Match) -> str:
    name = m.group(0).split()[-1]
    return _TOOL_WORDS.get(name, "a ferramenta interna")


def _dominant(found: list[str]) -> str:
    for name in KINDS:
        if name in found:
            return name
    return "tool"


def _tidy(text: str) -> str:
    out = re.sub(r"\s+", " ", text).strip()
    out = re.sub(r"\s+([,.;:!?…])", r"\1", out)
    out = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", out)
    out = re.sub(r"^[\s\-—–:;,.]+", "", out)
    out = re.sub(r"[\s\-—–:;,]+$", "", out).strip()
    if out and out[-1] not in ".!?…":
        out += "."
    return out


def _readable(text: str) -> bool:
    """Sobrou fala legível, ou só a casca da substituição?"""
    words = [w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ]{2,}", text)]
    content = [w for w in words if w not in _STOP]
    return len(content) >= 2


def _foreign(sentence: str) -> bool:
    """Heurística de inglês quando a fala é português: léxico + acentos."""
    if _ACCENT_RE.search(sentence):
        return False
    words = re.findall(r"[a-z']+", sentence.lower())
    if not words:
        return False
    en = sum(1 for word in words if word in _EN_WORDS)
    pt = sum(1 for word in words if word in _PT_WORDS)
    if en >= 2 and en > pt:
        return True
    return en >= 1 and len(words) <= 3 and pt == 0
