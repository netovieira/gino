"""Policy for pre-action narration ("what I am doing" before a slow tool).

The owner's rules (2026-10-31 / 2026-11-01):
  * a local, instant action with no external effect never gets a status line;
  * a social or simple question never gets one either — answer it directly;
  * an action whose effect leaves this machine (a message, a call, a
    publication, an upload, a purchase, a physical device) is announced at
    once: the owner must hear it before the effect happens;
  * a slow action with no external effect is announced only after
    ``NARRATION_DELAY_S`` (5 s by default), so fast answers stay silent;
  * when it does fire, the line must be tied to the real subject of the
    turn and must sound like a person thinking out loud — a small concrete
    image, first person, ordinary conversation;
  * no tool name, file name, path or jargon may ever be spoken, and no
    generic filler ("estou pensando", "um instante", …) either;
  * the same sentence is not repeated in consecutive narrated turns.

This module is the single decision point.  ``plan`` classifies the tool,
picks a murmur from the matching family and attaches the time threshold;
``decide`` applies the threshold, except for external-effect actions which
are always immediate.  Callers schedule a timer with ``plan.delay_s`` and
re-check with ``decide`` when it fires, so a fast answer can cancel the
line before it is spoken.

The text layer is deliberately separate from the policy: families and their
variations live in ``_FAMILY_LINES`` / ``_EXTERNAL_LINES`` and every line is
screened by ``is_generic`` and ``is_technical`` before it can be returned.

No Qt, no ``main.py`` and no model imports, so this stays unit-testable.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_DELAY_S = 5.0
DELAY_ENV = "JARVIS_NARRATION_DELAY_S"
_MIN_DELAY_S = 0.0
_MAX_DELAY_S = 120.0

# Instant actions whose effect is local and reversible.  Mirrors the safe/low
# tiers in core.tool_bridge: opening an app, volume/brightness, status, memory.
_LOCAL_ACTIONS = frozenset({
    "open_app", "media_control", "music_control", "video_player",
    "youtube_video", "computer_settings", "desktop_control", "system_monitor",
    "runtime_status", "brain_status", "web_search", "weather_report",
    "memory_index", "tasks", "reminder", "open_in", "file_controller",
    "file_processor", "screen_processor", "continuity", "morning_briefing",
    "qoder_status", "flight_finder", "rastracking", "proactive",
    "background_monitor", "groq_listen", "gemini_accounts_status", "focus_mode",
    "humor_mode", "phrase_blocklist", "personas", "qoder_job",
})

# Actions that leave the machine, touch a physical device or can take long.
_EXTERNAL_ACTIONS = frozenset({
    "send_message", "whatsapp_control", "email_control", "upload",
    "upload_file", "purchase", "buy", "android_control", "iphone_control",
    "mac_control", "browser_control", "computer_control", "trello_control",
    "cakto_sales", "qoder_agent", "dev_agent", "Agent", "persona_ask",
})

# Subset whose effect leaves this machine (message, call, publish, upload,
# purchase, physical device).  These must be announced before they happen,
# with no delay: by the time the threshold expires the message is already sent.
_IMMEDIATE_ACTIONS = frozenset({
    "send_message", "whatsapp_control", "email_control", "upload",
    "upload_file", "purchase", "buy", "android_control", "iphone_control",
    "mac_control",
})

_SEND_ACTIONS = frozenset({
    "send", "enviar", "manda", "mandar", "send_file", "enviar_arquivo",
    "mandar_arquivo", "media",
    # Nota de voz (ordem do dono, 07/11/2026): WhatsApp em áudio é o mesmo
    # "sai desta máquina" que o texto é — a narração não pode tratar a nota de
    # voz como ação local. Mesma família de apelidos do catálogo em
    # plugins/whatsapp_control.py (ACTION_ALIASES), aqui em lista explícita
    # porque a narração não importa plugins.
    "send_voice", "voice", "voice_note", "send_audio", "audio", "ptt",
    "nota_de_voz", "enviar_nota_de_voz", "mandar_nota_de_voz",
    "enviar_audio", "mandar_audio", "falar",
})

_EMAIL_SEND_ACTIONS = frozenset({"send", "enviar", "mandar", "escrever"})

# Actions that leave the machine and are phrased as "this goes out now".
_EXTERNAL_OUT_ACTIONS = frozenset({
    "send_message", "email_control", "upload", "upload_file", "purchase",
    "buy", "android_control", "iphone_control", "mac_control",
})

# Slow background jobs; the family depends on what the job is about.
_SLOW_JOB_ACTIONS = frozenset({"qoder_agent", "dev_agent", "Agent"})

# No template from these fragments may survive in executable code.  The list
# itself lives here so the policy can reject a cue from an older sidecar.
_GENERIC_MARKERS = (
    "estou pensando",
    "deixa eu pensar",
    "deixa eu analisar",
    "pensando em",
    "pensando numa",
    "um instante",
    "só um instante",
    "so um instante",
    "processando",
    "analisando a situação",
    "verificando o que",
    "procurando uma solução",
    "não vai dar certo",
    "nao vai dar certo",
)

# "deixa eu ver" is only filler when nothing concrete follows it.  The owner
# explicitly wants "deixa eu ver onde isso encalha", so the bare lead is
# rejected while a lead carrying an image is allowed.
_FILLER_LEAD_RE = re.compile(
    r"^(?:deixa\s+eu|deixa-me|vou|vamos|estou|t[oô]|s[oó])\s+"
    r"(?:ver|pensar|analisar|checar|conferir|olhar|procurar|resolver|entender)\b"
    r"[\s,.:;!?…-]*",
    re.IGNORECASE,
)
_FILLER_REST = frozenset({
    "", "isso", "isto", "aqui", "ai", "aí", "agora", "um", "uma", "pouco",
    "mais", "com", "calma", "direito", "direitinho", "melhor", "de", "novo",
    "já", "ja", "só", "so", "nada", "tudo", "coisa", "situação", "situacao",
    "bem", "rápido", "rapido", "logo", "certo", "então", "entao", "né", "ne",
    "e", "é",
})

# Vocabulary lock: nothing from this list may ever be spoken.  It mixes the
# generic jargon the owner named with the real action/tool names of this repo.
_TECH_TOKENS = frozenset({
    # project actions and tools
    "qoder", "qoder_agent", "tool_bridge", "actiongate", "pytest", "ruff",
    "mpris", "dconf", "gsettings", "persona", "personas", "whatsapp_control",
    "send_message", "email_control", "upload", "upload_file", "purchase",
    "dev_agent", "computer_control", "browser_control", "trello_control",
    "cakto_sales", "jarvis",
    # generic technical vocabulary
    "json", "api", "apis", "deploy", "deploys", "build", "builds", "log",
    "logs", "timeout", "thread", "threads", "script", "scripts", "agente",
    "agentes", "agent", "teste", "testes", "test", "porta", "portas", "main",
    "py", "python", "git", "commit", "branch", "merge", "repo", "docker",
    "bash", "shell", "curl", "sql", "http", "https", "url", "uri", "sdk",
    "ide", "bug", "debug", "stack", "trace", "traceback", "cache", "server",
    "servidor", "kernel", "socket", "webhook", "token", "tokens", "prompt",
    "llm", "model", "checkpoint", "pipeline", "backend", "frontend",
    "runtime", "node", "npm", "yarn", "venv", "import", "class", "print",
})

# Verb/word stems that are technical in every inflection.
_TECH_PREFIX_RE = re.compile(
    r"^(?:index|compil|execut|refator|deploy|build|debug|serializ|instanc|"
    r"test|renome|migr)",
    re.IGNORECASE,
)

_TECH_WORD_RE = re.compile(r"[a-zà-ÿ0-9_./-]+", re.IGNORECASE)
_TECH_SPLIT_RE = re.compile(r"[./_-]+")

_SOCIAL_RE = re.compile(
    r"(?:\b(?:oi|ol[aá]|bom dia|boa tarde|boa noite|tudo bem|tudo certo|"
    r"como (?:voc[êe]|vc) (?:est[aá]|vai|t[aá])|como vai|obrigad[oa]|valeu|"
    r"agrade[cç]o|de nada|at[eé] (?:logo|mais)|tchau|quem [eé] voc[êe]|"
    r"seu nome|o que voc[êe] faz|voc[êe] (?:est[aá]|t[aá]) bem|"
    r"est[aá] bem)\b)",
    re.IGNORECASE,
)

# A social opening that also carries a real command is not a social question.
_ACTION_INTENT_RE = re.compile(
    r"\b(?:abre|abrir|toca|tocar|coloca|p[oõ]e|envia|enviar|manda|mandar|"
    r"liga|ligar|chama|chamar|compra|comprar|publica|publicar|sobe|subir|"
    r"instala|instalar|reinicia|reiniciar|desliga|desligar|agenda|agendar|"
    r"lembra|lembrar|pesquisa|pesquisar|busca|buscar|verifica|verificar|"
    r"consulta|consultar|cria|criar|edita|editar|apaga|apagar|remove|remover|"
    r"atualiza|atualizar)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NarrationPlan:
    """A specific line and the delay that must pass before saying it.

    ``immediate`` marks an action whose effect leaves this machine; it is
    spoken before the work starts and ignores the threshold entirely.
    """

    text: str
    delay_s: float
    immediate: bool = False


@dataclass(frozen=True)
class NarrationDecision:
    narrate: bool
    text: str
    delay_s: float


def narration_delay() -> float:
    """Seconds before a slow action may be narrated (env override, clamped)."""
    raw = os.environ.get(DELAY_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_DELAY_S
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DELAY_S
    return max(_MIN_DELAY_S, min(_MAX_DELAY_S, value))


def _tokens(text: str) -> set:
    """Word tokens plus their path/underscore parts, lowercased."""
    out: set = set()
    for raw in _TECH_WORD_RE.findall(str(text or "").lower()):
        out.add(raw)
        for part in _TECH_SPLIT_RE.split(raw):
            if part:
                out.add(part)
    return out


def is_technical(text: str) -> bool:
    """True when a line would leak a tool, file, path or piece of jargon."""
    for token in _tokens(text):
        if token in _TECH_TOKENS or _TECH_PREFIX_RE.match(token):
            return True
    return False


def is_generic(text: str) -> bool:
    """True for filler that could be said for any question."""
    flat = " ".join(str(text or "").lower().split())
    if any(marker in flat for marker in _GENERIC_MARKERS):
        return True
    lead = _FILLER_LEAD_RE.match(flat)
    if lead is None:
        return False
    rest = set(re.findall(r"[a-zà-ÿ]+", flat[lead.end():]))
    return rest <= _FILLER_REST


def is_social_question(text: str) -> bool:
    """A greeting/thanks/how-are-you turn with no command attached."""
    flat = " ".join(str(text or "").strip().split())
    if not flat or not _SOCIAL_RE.search(flat):
        return False
    return not _ACTION_INTENT_RE.search(flat)


def _clean(value, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _first(args: dict, *keys) -> str:
    for key in keys:
        value = args.get(key)
        if value is not None and str(value).strip():
            return _clean(value)
    return ""


def _resolve_tool(tool_name: str, tool_input: dict) -> tuple[str, dict]:
    """(action name, action args) behind the MCP wrapper, when there is one."""
    raw_name = str(tool_name or "")
    short = raw_name.rsplit("__", 1)[-1]
    data = tool_input if isinstance(tool_input, dict) else {}
    if short in ("jarvis_call", "jarvis_tools"):
        args = data.get("args")
        return str(data.get("name") or ""), args if isinstance(args, dict) else {}
    return short, data


# Which murmur family a slow job belongs to, guessed from what it is about.
_FAMILY_RES = (
    ("consertar", re.compile(
        r"\b(?:consert|corrig|arrum|quebr|falh|apert|ajeit)", re.IGNORECASE)),
    ("procurar", re.compile(
        r"\b(?:procur|busc|ach|onde|encontr|cad[eê])", re.IGNORECASE)),
    ("escrever", re.compile(
        r"\b(?:escrev|cria|implement|adicion|mud|mont|desenvolv|constru|"
        r"texto|resposta|rascunh)", re.IGNORECASE)),
    ("investigar", re.compile(
        r"\b(?:revis|verific|confer|olh|analis|entend|descobr)", re.IGNORECASE)),
)

# Human murmur families: (line that names the subject, line that does not).
# Every line is a concrete image, first person, no tool or file names, and
# avoids the generic markers above.  Five variations per family so the
# no-repeat rule has room to move.
_FAMILY_LINES = {
    "investigar": (
        ("deixa eu ver onde {s} encalha",
         "deixa eu ver onde isso encalha"),
        ("{s} tem dois fios e eu só achei um",
         "isso tem dois fios e eu só achei um"),
        ("já sei por onde começa {s}; falta o meio",
         "já sei por onde começa, falta o meio"),
        ("{s} mente no primeiro olhar",
         "isso é daqueles que mentem no primeiro olhar"),
        ("tem ponta solta com {s} e eu tô atrás dela",
         "tem ponta solta aqui e eu tô atrás dela"),
    ),
    "consertar": (
        ("{s} tinha uma peça torta; agora eu endireito",
         "achei a peça torta; agora endireito"),
        ("{s} tem um parafuso solto e eu vou apertar",
         "tem um parafuso solto aqui dentro"),
        ("tô remendando {s}, falta alinhar",
         "tô no meio do remendo, falta alinhar"),
        ("{s} engana; o estrago é no meio",
         "o estrago é pequeno, só está escondido"),
        ("já sei o que está torto; {s} só disfarça",
         "já sei o que está torto, falta apertar"),
    ),
    "procurar": (
        ("{s} tem uma peça faltando e eu já sei qual",
         "tô procurando a peça que falta"),
        ("tô atrás da peça que não aparece; {s} esconde bem",
         "isso não está onde devia, mas eu já tenho uma pista"),
        ("{s} some justo na parte que importa",
         "tem uma parte escondida e eu tô chegando perto"),
        ("quase achei; {s} tem um pedaço que não combina",
         "tô juntando os pedaços, um já apareceu"),
        ("{s} está pela metade na minha frente e a outra parte fugiu",
         "quase achei o que faltava"),
    ),
    "escrever": (
        ("tô escrevendo isso aos poucos; {s} ainda está pela metade",
         "tô escrevendo aos poucos, ainda está pela metade"),
        ("{s} já tem começo e fim; falta o meio",
         "já tenho começo e fim, falta o meio"),
        ("vou mudar {s} com cuidado pra não quebrar o resto",
         "vou mudar com cuidado pra não quebrar o resto"),
        ("tô rascunhando {s} e apagando mais do que escrevo",
         "tô rascunhando e apagando mais do que escrevo"),
        ("{s} pede a palavra certa e não aceita qualquer uma",
         "isso pede a palavra certa, não aceita qualquer uma"),
    ),
    "esperar": (
        ("{s} está demorando mais do que eu queria",
         "está demorando mais do que eu queria"),
        ("{s} ainda não deu notícia, mas está andando",
         "ainda não deu notícia, mas está andando"),
        ("tô de olho; {s} ainda está no meio do caminho",
         "tô de olho, ainda está no meio do caminho"),
        ("{s} ainda não acabou, mas já está no fim",
         "falta pouco, eu acho"),
        ("segurei a respiração; {s} está quase no ponto",
         "está quase no ponto, só não chegou"),
    ),
}

# External-effect lines: (line with the person/place it reaches, line without).
# {n} is the thing that goes out (message, file, order, …).
_EXTERNAL_LINES = (
    ("já estou mandando {n} pra {to}",
     "já estou mandando {n} agora"),
    ("{to} recebe {n} daqui a pouco",
     "isso sai daqui agora"),
    ("segurei até agora; {n} vai pra {to} neste momento",
     "segurei até agora; {n} vai neste momento"),
    ("tô entregando {n} pra {to} agora",
     "tô entregando {n} agora"),
    ("deixa comigo: {n} chega em {to} já",
     "deixa comigo: isso sai agora"),
)

FAMILIES = ("externo", "investigar", "consertar", "procurar", "escrever",
            "esperar")

# Determiner + concrete noun is the only shape accepted as a subject, which
# keeps stray verbs ("revisar", "consertar") out of the spoken line.
_DETERMINER_RE = re.compile(
    r"\b(o|a|os|as|um|uma|meu|minha|nosso|nossa|esse|essa|este|esta|aquele|"
    r"aquela)\s+([a-zà-ÿ]{3,})\b",
    re.IGNORECASE,
)
_VAGUE_NOUNS = frozenset({
    "que", "coisa", "coisas", "isso", "isto", "situação", "situacao", "caso",
    "jeito", "parte", "lado", "momento", "gente", "hora", "vez", "tipo",
})


def _usable(text: str) -> bool:
    text = " ".join(str(text or "").split())
    if not text:
        return False
    return not is_generic(text) and not is_technical(text)


def _subject(*sources) -> str:
    """The most concrete determiner+noun phrase in the source text, or ""."""
    best = ""
    for source in sources:
        for match in _DETERMINER_RE.finditer(str(source or "")):
            noun = match.group(2).lower()
            phrase = f"{match.group(1).lower()} {noun}"
            if noun in _VAGUE_NOUNS or is_technical(phrase):
                continue
            if len(noun) > len(best):
                best = phrase
    return best


def _target(args: dict) -> str:
    name = _first(args, "to", "contact", "receiver", "chat")
    name = name.split("@")[0].strip()
    return name if name and not is_technical(name) else ""


def _effect_noun(action: str, args: dict) -> str:
    if action in ("upload", "upload_file"):
        return "o arquivo"
    if action in ("purchase", "buy"):
        return "o pedido"
    if action in ("android_control", "iphone_control", "mac_control"):
        return "o que você pediu"
    if action == "email_control":
        return "o e-mail"
    return "a mensagem"


def _family_lines(family: str, subject: str = "") -> tuple[str, ...]:
    """Clean lines for a family, subject-linked when a safe subject exists."""
    pairs = _FAMILY_LINES.get(family)
    if not pairs:
        return ()
    if subject and is_technical(subject):
        subject = ""
    if subject:
        lines = [with_subject.format(s=subject) for with_subject, _ in pairs]
        lines = [line for line in lines if _usable(line)]
        if lines:
            return tuple(dict.fromkeys(lines))
    lines = [plain for _, plain in pairs]
    lines = [line for line in lines if _usable(line)]
    return tuple(dict.fromkeys(lines))


def _external_lines(effect: str, target: str) -> tuple[str, ...]:
    """Clean lines for an action that leaves this machine."""
    if target and is_technical(target):
        target = ""
    lines = []
    for with_target, plain in _EXTERNAL_LINES:
        if target:
            lines.append(with_target.format(n=effect, to=target))
        else:
            lines.append(plain.format(n=effect))
    lines = [line for line in lines if _usable(line)]
    return tuple(dict.fromkeys(lines))


def variants(family: str, subject: str = "", target: str = "",
             effect: str = "a mensagem") -> tuple[str, ...]:
    """Public read-only view of the spoken options for a family (tests)."""
    if family == "externo":
        return _external_lines(effect, target)
    return _family_lines(family, subject)


def _slow_family(blob: str) -> str:
    for family, pattern in _FAMILY_RES:
        if pattern.search(blob):
            return family
    return "esperar"


def _cue(action: str, args: dict, user_text: str,
         persona_name: str) -> Optional[tuple[str, str]]:
    """(family, subject-or-target) for this action, or None when silent."""
    if action == "whatsapp_control":
        what = str(args.get("action") or "").strip().lower()
        if what not in _SEND_ACTIONS:
            return None                     # status/open/search are local
        return "externo", _target(args)

    if action == "email_control":
        if str(args.get("action") or "").strip().lower() not in _EMAIL_SEND_ACTIONS:
            return None
        return "externo", _target(args)

    if action in _EXTERNAL_OUT_ACTIONS:
        return "externo", _target(args)

    if action == "browser_control":
        return "procurar", _subject(user_text, _first(args, "query", "task"))

    if action == "cakto_sales":
        return "investigar", _subject(user_text)

    if action == "trello_control":
        return "escrever", _subject(user_text)

    if action == "computer_control":
        return "consertar", _subject(
            user_text, _first(args, "task", "command", "text"))

    if action == "persona_ask":
        name = _clean(persona_name) or _first(args, "persona", limit=40)
        if name and is_technical(name):
            name = ""
        return "esperar", name

    if action in _SLOW_JOB_ACTIONS:
        blob = " ".join([
            str(user_text or ""),
            _first(args, "task", "title", "description", "prompt"),
        ])
        family = _slow_family(blob)
        return family, _subject(user_text, _first(
            args, "task", "title", "description"))

    return "investigar", _subject(user_text)


def _stable_index(key: str, size: int) -> int:
    if size <= 1:
        return 0
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % size


class NarrationPolicy:
    """Single point that decides whether, what and when to narrate."""

    def __init__(self, delay_s: Optional[float] = None) -> None:
        self.delay_s = narration_delay() if delay_s is None else max(
            _MIN_DELAY_S, min(_MAX_DELAY_S, float(delay_s)))
        self._last_spoken = ""
        self._last_turn = None
        self._last_variant = None

    def _compose(self, family: str, subject: str, action: str,
                 args: dict) -> str:
        """One screened line for the family; same subject keeps the same line."""
        if family == "externo":
            effect = _effect_noun(action, args)
            options = _external_lines(effect, subject)
            key = (family, subject, action, effect)
        else:
            options = _family_lines(family, subject)
            key = (family, subject, action)
        if not options:
            return ""
        index = _stable_index("|".join(key), len(options))
        last = self._last_variant
        if (last is not None and last[0] == family and last[1] == index
                and last[2] != key and len(options) > 1):
            index = (index + 1) % len(options)
        self._last_variant = (family, index, key)
        return options[index]

    def plan(self, tool_name: str, tool_input: dict,
             user_text: str = "", persona_name: str = "") -> Optional[NarrationPlan]:
        """A subject-linked murmur for an eligible action, or None."""
        if is_social_question(user_text):
            return None
        action, args = _resolve_tool(tool_name, tool_input)
        if not action or action in _LOCAL_ACTIONS:
            return None
        if action not in _EXTERNAL_ACTIONS:
            return None
        cue = _cue(action, args, user_text, persona_name)
        if cue is None:
            return None
        family, subject = cue
        text = self._compose(family, subject, action, args)
        if not text or is_generic(text) or is_technical(text):
            return None
        immediate = action in _IMMEDIATE_ACTIONS
        return NarrationPlan(text=text,
                             delay_s=0.0 if immediate else self.delay_s,
                             immediate=immediate)

    def note_spoken(self, text: str, turn=None) -> None:
        """Remember the last line actually spoken, for the no-repeat rule."""
        self._last_spoken = " ".join(str(text or "").lower().split())
        self._last_turn = turn

    def would_repeat(self, text: str, turn=None) -> bool:
        """True when ``text`` was already said in the previous narrated turn."""
        key = " ".join(str(text or "").lower().split())
        if not key or key != self._last_spoken:
            return False
        return turn is None or self._last_turn is None or turn != self._last_turn

    def decide(self, plan: Optional[NarrationPlan], elapsed_s: float,
               *, spoken: bool = False, turn=None) -> NarrationDecision:
        """Speak the planned cue only after the delay and only if still useful.

        External-effect actions are exempt from the threshold: they are said
        before the message/call/purchase happens, however fast the tool is.
        """
        if (plan is None or spoken or is_generic(plan.text)
                or is_technical(plan.text)):
            return NarrationDecision(False, "", self.delay_s)
        if not plan.immediate and float(elapsed_s) + 1e-9 < plan.delay_s:
            return NarrationDecision(False, "", plan.delay_s)
        if self.would_repeat(plan.text, turn):
            return NarrationDecision(False, "", plan.delay_s)
        return NarrationDecision(True, plan.text, plan.delay_s)
