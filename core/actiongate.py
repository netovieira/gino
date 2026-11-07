"""A short window in which the user has authorised a risky common action,
opened by his voice or by his typed password (the two full factors of
core/owner_auth.py — decision of the owner, 06/11/2026).

WHY THIS EXISTS
    The tool bridge refuses medium/high actions unless a confirm callback says
    yes — but this owner confirms with his voice, never by clicking. The bridge
    therefore uses ``confirm(title, message)`` below as its confirmation
    channel: it returns True only inside a window that a spoken consent phrase
    opened, and False otherwise (marking the request pending so the next short
    "pode" answers it).

THE RISK, STATED PLAINLY
    The microphone hears the room, and the room includes music and TV. So the
    window follows the same rules as core/rootgate.py: opened only by a human
    utterance matched before any model sees the text, short-lived, and bounded
    by time rather than by a use count. Unlike rootgate there is no privilege
    word to require — the equivalent of "the user named sudo" here is "a risky
    action is already pending": without a request on the table, nothing in this
    module opens a window at all, so a stray "pode" from the TV authorises
    nothing.

WHY TIME, NOT A USE COUNT
    The bridge asks confirm() before the action runs, so a first True cannot
    know whether the handler will execute anything: whatsapp_control without a
    contact answers "para quem?" instead of sending. Consuming the window on
    that True spent the owner's consent on a question, and the retry with the
    contact was refused. The deadline is the lock: a handler that aborts never
    shortens the window, and only the 120 s running out closes it.

SEPARATE FROM ROOT
    This window authorises ordinary risky actions (sending a message, remote
    device control). Sudo and root belong to core/rootgate.py, with its own
    window: neither window substitutes for the other. Both modules accept
    the same two factors — the owner's voice and his verified typed
    password (decision of the owner, 06/11/2026).

SEPARATE FROM STANDING PERSON-GRANTS
    Everything here is a WINDOW: it opens on a spoken consent and dies on a
    deadline. The contract's standing grants per person ("dê acesso total a
    VALMIRA", FLUXOS.md ramificação "*") live in core/voice_grants.py and
    are created only by the owner under a privileged verdict. A window here
    is not a grant there, and a grant there never opens a window here.

AUTONOMOUS WHATSAPP REPLIES
    A narrower grant, checked by the bridge's preauthorize hook rather than by
    confirm(): when a new individual WhatsApp message addresses the assistant
    by name, core/whatsapp_watch.py calls allow_whatsapp_reply(account, chat,
    name). That authorises exactly whatsapp_control with action=send to the
    same contact on the same account, for AUTO_REPLY_SECONDS. Any other
    action, contact or account still reaches the owner's voice window, and
    this grant never opens the sudo window.

CONSENT SPOKEN BEFORE THE REQUEST IS PARKED
    The owner answers "pode enviar" as soon as he hears the question, but the
    bridge only parks the request when the model's tool call arrives — often
    seconds later. On 02/11/2026 the words came at 16:04:16 and 16:12:11 and
    the refusals at 16:04:33 and 16:12:27, so a window that required a pending
    request at the exact moment of the utterance lost both. arm_send_window()
    keeps that consent alive for CONSENT_TTL_SECONDS (15 min) and confirm()
    honours it only for whatsapp_control. It is a bounded window, never a
    standing grant, and it does not open the sudo window.

THE FACTOR THAT OPENED THE WINDOW (lei do dono, 13/11/2026; fator pleno
por decisão do dono em 06/11/2026)
    The accepted factors are the owner's VOICE and his TYPED PASSWORD — the
    same verdict core/job_grant.py and core/owner_auth.py speak (FACTOR_VOICE
    / FACTOR_TEXT, decided in one place by owner_auth.authorized_factor). A
    password typed by the owner (app text or his direct WhatsApp chat) is
    FULL owner authorisation, so the 15-minute send window it armed covers
    the whole sending scope: whatsapp_control AND send_message — same scope,
    same TTL, no per-action exception. The send window armed by an
    anticipated spoken consent keeps the narrower WhatsApp scope it was
    created with on 02/11/2026: a stray "pode enviar" caught off the mic
    must not reach other platforms. No window of this module grants sudo by
    itself — core/rootgate.py owns that, and since 06/11/2026 BOTH factors
    open it there as well (main._owner_password_grant). Unknown voices,
    personas, subagents and third-party text open nothing.

BLOCKED EXECUTIONS THE OWNER HAS TO HEAR (UP-9, a partir de 29/11/2026)
    Passado o período de observação, o executor deixa de entregar como fato
    o resultado de uma ação com efeito colateral que voltou sem recibo HMAC.
    A recusa é registrada aqui: ``note_blocked`` guarda avisos de vida curta
    (``BLOCKED_TTL_SECONDS``) e entrega uma cópia a quem registrou
    ``add_blocked_listener`` — a fala do dono, o painel, o WhatsApp; quem
    injeta é o processo vivo, e sem ouvinte o rastro continua no activity
    log. Um aviso destes não abre janela nenhuma: é notícia, nunca grant.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
import weakref

from core import rootgate
from core.owner_auth import FACTOR_TEXT, FACTOR_VOICE  # veredito único de fatores

# How long a spoken consent for a pending bridge request lasts once accepted.
WINDOW_SECONDS = 120.0

# How long a consent that arrived BEFORE the bridge parked its request stays
# valid for the next WhatsApp send. The owner answers the question he hears
# while the model is still composing the tool call; the call lands seconds
# later. Fifteen minutes covers the composition and the retry, without being
# a standing grant.
CONSENT_TTL_SECONDS = 900.0

# How long a refused request stays on the table, waiting for the user's answer.
# Shorter than the window itself: the question "posso enviar?" should be
# answered now or the intent is stale.
PENDING_SECONDS = 300.0

# How long the assistant has to compose the autonomous reply after a WhatsApp
# message named it. 600 s matches the execution budget of a bridge call.
AUTO_REPLY_SECONDS = 600.0

_lock = threading.Lock()
_until = 0.0
_send_until = 0.0
# Qual fator abriu cada janela ("" = nada aberto). Só muda a decisão de
# escopo do confirm(): FACTOR_TEXT (senha do dono verificada por PBKDF2) é
# autorização plena de envio; FACTOR_VOICE mantém o escopo de 02/11/2026.
_window_factor = ""
_send_factor = ""
_pending: dict | None = None
# (account, chat) -> {"account": folded, "name": contact, "until": monotonic}
_replies: dict[tuple[str, str], dict] = {}
# Callbacks told when the owner grants a new window. The bridge uses this to
# drop its "already denied" marks: a fresh "pode" is a new authorisation, so
# the same call must be allowed to reach the normal gate again.
_grant_listeners: list[weakref.ref] = []


# ── UP-9: execuções bloqueadas por falta de prova material ─────────────────
#
# Depois do período de observação (29/11/2026) o executor recusa como fato o
# resultado de uma ação com efeito colateral que voltou sem recibo HMAC. A
# recusa precisa chegar ao dono — foi ele quem pediu a ação, e é ele quem
# decide o que fazer com um resultado que não tem prova. O registro abaixo é
# o portão dessa notícia: uma lista viva (com TTL, como tudo aqui) dos
# bloqueios recentes, mais os ouvintes que o processo vivo registra para
# falar/showar o aviso. Nada aqui autoriza execução nenhuma: é rastro e
# aviso, não janela.


# Quão recente um bloqueio precisa ser para ainda contar como "avisar".
BLOCKED_TTL_SECONDS = 1800.0

# Tetos do registro: uma sessão longa não pode acumular bloqueios para sempre.
_MAX_BLOCKED = 50

_blocked: list[dict] = []
_blocked_listeners: list[weakref.ref] = []


def add_blocked_listener(callback) -> None:
    """Chama ``callback(payload)`` quando uma execução é bloqueada (UP-9).

    Quem registra é o processo vivo (a fala do dono, o painel, o aviso por
    WhatsApp). A referência é fraca: um listener que morre não vazia memória
    nem bloqueia o executor. Um listener quebrado nunca derruba o registro —
    o rastro no portão independe da entrega.
    """
    if not callable(callback):
        return
    try:
        ref = weakref.WeakMethod(callback)
    except TypeError:
        ref = weakref.ref(callback)
    with _lock:
        if ref not in _blocked_listeners:
            _blocked_listeners.append(ref)


def note_blocked(payload: dict | None = None, **fields) -> dict:
    """Registra uma execução bloqueada e devolve o aviso entregue.

    O registro vive ``BLOCKED_TTL_SECONDS`` e é limitado a ``_MAX_BLOCKED``
    entradas. Os ouvintes recebem uma cópia do payload; exceção de ouvinte
    é engolida — o bloqueio já aconteceu e o log já guarda o rastro.
    """
    record = dict(payload or {})
    record.update(fields)
    record.setdefault("tool", "")
    record.setdefault("task_id", "")
    record.setdefault("reason", "")
    record.setdefault("kind", "execution_blocked")
    record["at"] = time.monotonic()
    record["until"] = record["at"] + BLOCKED_TTL_SECONDS
    with _lock:
        _prune_blocked_locked()
        _blocked.append(record)
        if len(_blocked) > _MAX_BLOCKED:
            del _blocked[:len(_blocked) - _MAX_BLOCKED]
        refs = list(_blocked_listeners)
    delivered = dict(record)
    for ref in refs:
        callback = ref()
        if callback is None:
            with _lock:
                if ref in _blocked_listeners:
                    _blocked_listeners.remove(ref)
            continue
        try:
            callback(dict(record))
        except Exception:
            pass  # a entrega falha; o registro e o log ficam de pé
    return delivered


def blocked_actions(now: float | None = None) -> list[dict]:
    """Os bloqueios ainda vivos (mais recente primeiro)."""
    moment = time.monotonic() if now is None else float(now)
    with _lock:
        return [dict(item) for item in reversed(_blocked)
                if moment < float(item.get("until", 0.0))]


def has_blocked(now: float | None = None) -> bool:
    """True enquanto há pelo menos um bloqueio vivo para avisar o dono."""
    return bool(blocked_actions(now=now))


def clear_blocked() -> None:
    """Esquece os bloqueios registrados (colheita do app, ou teste)."""
    with _lock:
        _blocked.clear()


def _prune_blocked_locked(now: float | None = None) -> None:
    """Descarta registros vencidos. Chamado com o lock tomado."""
    moment = time.monotonic() if now is None else float(now)
    keep = [item for item in _blocked if moment < float(item.get("until", 0.0))]
    if len(keep) != len(_blocked):
        _blocked[:] = keep


def add_grant_listener(callback) -> None:
    """Call ``callback()`` whenever a spoken consent opens the window."""
    if not callable(callback):
        return
    try:
        ref = weakref.WeakMethod(callback)
    except TypeError:
        ref = weakref.ref(callback)
    with _lock:
        _grant_listeners.append(ref)


def _notify_grant() -> None:
    with _lock:
        refs = list(_grant_listeners)
    for ref in refs:
        callback = ref()  # WeakMethod -> bound method; plain ref -> function
        if callback is None:
            with _lock:
                if ref in _grant_listeners:
                    _grant_listeners.remove(ref)
            continue
        try:
            callback()
        except Exception:
            pass  # a broken listener must never block the consent itself


def looks_like_consent(text: str) -> bool:
    """True when the utterance is the user answering a pending request.

    Delegates to rootgate's matcher in pending mode — a short phrase built
    around a consent word ("pode", "vai", "autorizo", "pode enviar") — because
    the semantics are identical: it is the answer to the assistant's question.
    """
    return rootgate.looks_like_authorization(text, pending=True)


def mark_pending(title: str, message: str = "") -> None:
    """A risky request was refused for lack of authorisation; park it."""
    global _pending
    with _lock:
        _pending = {"title": str(title or ""), "message": str(message or ""),
                    "until": time.monotonic() + PENDING_SECONDS}


def has_pending() -> bool:
    """True while a refused request is still waiting for the user's answer."""
    with _lock:
        if _pending is None:
            return False
        if time.monotonic() >= _pending["until"]:
            return False
        return True


def pending() -> dict | None:
    """The waiting request, or None if there is none / it expired."""
    with _lock:
        if _pending is None or time.monotonic() >= _pending["until"]:
            return None
        return dict(_pending)


def clear_pending() -> None:
    global _pending
    with _lock:
        _pending = None


def open_window(seconds: float = WINDOW_SECONDS,
                factor: str = FACTOR_VOICE) -> float:
    """Start the window after an authorisation. Returns how long it lasts.

    ``factor`` records WHO opened it (voz do dono / senha digitada — the two
    labels core/owner_auth.py defines). It never widens the window: the same
    bounded time. Like every other privileged class, both factors also open
    the sudo window where sudo is owned (core/rootgate.py) — this module's
    windows themselves stay separate from it.
    """
    global _until, _window_factor
    with _lock:
        _until = time.monotonic() + max(5.0, float(seconds))
        _window_factor = str(factor or FACTOR_VOICE)
    # A new consent renews everything that had been refused: the bridge
    # forgets the calls it had marked as "already denied".
    _notify_grant()
    return max(5.0, float(seconds))


def arm_send_window(seconds: float = CONSENT_TTL_SECONDS,
                    factor: str = FACTOR_VOICE) -> float:
    """Remember a consent spoken BEFORE the bridge parked its request.

    The live failure of 02/11/2026: the owner answered "Pode enviar" at
    16:04:16 and 16:12:11, but the bridge only parked the whatsapp_control
    request at 16:04:33 and 16:12:27 — after the words. ``confirm()`` then
    found no pending request at the moment of the utterance and the consent
    was lost. This armed window is checked when the call finally arrives and
    authorises a bounded send, for a bounded time. It is not a standing
    grant and it never opens the sudo window.

    ``factor`` decides the send scope (see this module's header): a window
    armed by the owner's verified TYPED PASSWORD covers the whole sending
    class — whatsapp_control and send_message — with the same TTL; a window
    armed by an anticipated spoken consent keeps the WhatsApp-only scope.
    """
    global _send_until, _send_factor
    with _lock:
        _send_until = time.monotonic() + max(5.0, float(seconds))
        _send_factor = str(factor or FACTOR_VOICE)
    _notify_grant()
    return max(5.0, float(seconds))


def _window_open() -> bool:
    with _lock:
        return time.monotonic() < _until


def _send_window_open() -> bool:
    with _lock:
        return time.monotonic() < _send_until


def remaining() -> float:
    with _lock:
        now = time.monotonic()
        return max(0.0, _until - now, _send_until - now)


def authorized() -> bool:
    """True while an owner authorisation window is still in force.

    Includes the 15-minute window armed by a "pode enviar" that arrived before
    the bridge's request was parked (``arm_send_window``). The bridge uses
    this probe to reopen a previously refused request; ``confirm`` scopes that
    armed window to the send class (see ``_send_window_allows``), so an
    unrelated risky action still needs the ordinary 120 s pending window.
    """
    with _lock:
        now = time.monotonic()
        return now < _until or now < _send_until


def close() -> None:
    global _until, _send_until, _window_factor, _send_factor, _pending
    with _lock:
        _until = 0.0
        _send_until = 0.0
        _window_factor = ""
        _send_factor = ""
        _pending = None
        _replies.clear()
        # UP-9: fechar o portão também fecha o registro de bloqueios — uma
        # sessão nova não herda avisos de execuções da sessão anterior.
        _blocked.clear()


def window_factor() -> str:
    """Qual fator abriu a janela de ação hoje vigente ('' se nenhuma)."""
    with _lock:
        return _window_factor if time.monotonic() < _until else ""


def send_window_factor() -> str:
    """Qual fator abriu a janela de envio vigente ('' se nenhuma)."""
    with _lock:
        return _send_factor if time.monotonic() < _send_until else ""


# Escopo da janela de envio: o CLASS-DE-AÇÕES "mandar mensagem" — nunca uma
# exceção por ferramenta. whatsapp_control é o envio conectado (regra de
# 02/11/2026); send_message é o mesmo envio dirigido por janela de app.
# E-mail NÃO entra aqui: mail_control tem portão próprio, liberado pelos
# DOIS fatores do dono (voz ou senha digitada verificada) — decisão dele
# de 06/11/2026.
_SEND_TITLES = ("whatsapp_control", "send_message")


def _send_window_allows(title: str) -> bool:
    """True when the armed send window covers this confirmation title."""
    with _lock:
        open_now = time.monotonic() < _send_until
        factor = _send_factor if open_now else ""
    if not open_now:
        return False
    low = str(title or "").lower()
    if "whatsapp_control" in low:
        return True
    # Fator senha-digitada (dono verificado por PBKDF2): mesmo escopo e mesmo
    # TTL das demais ações privilegiadas da janela — cobre o envio por
    # send_message também. Fator voz antecipada: escopo de WhatsApp de origem.
    if factor == FACTOR_TEXT and "send_message" in low:
        return True
    return False


def confirm(title: str, message: str) -> bool:
    """The bridge's confirmation channel — owner factor, bounded by time.

    Returns True while the owner's authorisation window is open (voice or
    typed password — the two factors core/owner_auth.py defines), or while the
    armed send window is open and the call is a send in that window's scope
    (see _send_window_allows). The window is not spent here: the bridge asks
    before the action runs, and a handler that aborts to ask for a missing
    argument (whatsapp_control without a contact answers "para quem?") would
    otherwise burn the consent on a question. The deadline is the lock.
    Outside it the request is parked as pending (so the next short "pode" is
    understood as the answer) and this returns False, which the bridge turns
    into "was not confirmed". A model can never reach True here by itself:
    only the utterance matcher (owner's voice) or a verified typed password
    can open the windows.
    """
    if _window_open():
        clear_pending()
        return True
    if _send_window_allows(title):
        clear_pending()
        return True
    mark_pending(title, message)
    return False


# ── UV-4: origem de toda ação de dispositivo ────────────────────────────────

def _fold(value: object) -> str:
    """Comparação sem caixa nem acentos (origem de UV-4 e grants de WhatsApp).

    Servia só ao grant de resposta autônoma do WhatsApp; desde UV-4 também é
    o normalizador do rótulo de origem — um dobrador, duas famílias de regra.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


#
# A queixa do dono (PLANO Fase 7 TASK4) é "mexeu sem pedido". Rastrear é o
# primeiro passo: toda ação despachada carrega um rótulo de origem entre os
# quatro do contrato — fala do dono, fala de visitante, automática, ou vinda
# do filtro de e-mail. O segundo passo é a política: ação com origem
# EXPLÍCITA ``automatica`` não toca dispositivo sensível (tier medium/high do
# tool_bridge — exatamente as ações que hoje já exigem a voz do dono). Uma
# origem não declarada ('') não ganha bloqueio novo: ela é registrada como
# ``nao_declarada`` no log, que é justamente a lacuna que a métrica de 24 h
# precisa mostrar para ser fechada.

ORIGIN_OWNER_SPEECH = "fala_do_dono"
ORIGIN_VISITOR_SPEECH = "fala_visitante"
ORIGIN_AUTOMATIC = "automatica"
ORIGIN_EMAIL_FILTER = "filtro_email"
ORIGIN_UNDECLARED = "nao_declarada"

ACTION_ORIGINS = frozenset({ORIGIN_OWNER_SPEECH, ORIGIN_VISITOR_SPEECH,
                            ORIGIN_AUTOMATIC, ORIGIN_EMAIL_FILTER})

# Rótulos que o app já usa nos turnos e chamadas (``source=``) viram origem
# canônica aqui, num lugar só — quem despacha não inventa nome.
_ORIGIN_ALIASES: dict[str, str] = {
    "owner": ORIGIN_OWNER_SPEECH,
    "dono": ORIGIN_OWNER_SPEECH,
    "voice": ORIGIN_OWNER_SPEECH,
    "voz": ORIGIN_OWNER_SPEECH,
    "typed": ORIGIN_OWNER_SPEECH,
    "texto": ORIGIN_OWNER_SPEECH,
    "whatsapp": ORIGIN_OWNER_SPEECH,   # só o chat direto chega a rotear
    "visitor": ORIGIN_VISITOR_SPEECH,
    "visitante": ORIGIN_VISITOR_SPEECH,
    "guest": ORIGIN_VISITOR_SPEECH,
    "convidado": ORIGIN_VISITOR_SPEECH,
    "email": ORIGIN_EMAIL_FILTER,
    "mail": ORIGIN_EMAIL_FILTER,
    "email_flow": ORIGIN_EMAIL_FILTER,
    "email_filter": ORIGIN_EMAIL_FILTER,
    "auto": ORIGIN_AUTOMATIC,
    "automatic": ORIGIN_AUTOMATIC,
    "system": ORIGIN_AUTOMATIC,
    "sistema": ORIGIN_AUTOMATIC,
    "proactive": ORIGIN_AUTOMATIC,
    "proativa": ORIGIN_AUTOMATIC,
    "scheduled": ORIGIN_AUTOMATIC,
    "timer": ORIGIN_AUTOMATIC,
    "queue": ORIGIN_AUTOMATIC,
    "task": ORIGIN_AUTOMATIC,
    "monitor": ORIGIN_AUTOMATIC,
}

# Espelho dos tiers medium/high de core/tool_bridge.py, usado SOMENTE quando
# o bridge não é importável (ambiente enxuto sem fastapi). Manter em passo com
# ACTION_TIERS; a fonte da verdade é o tier do bridge.
_SENSITIVE_ACTIONS_FALLBACK = frozenset({
    "send_message", "email_control", "mail_control", "whatsapp_control",
    # linkedin_control fora do caminho de acoes: rota desligada pelo dono
    # (07/11/2026) — LinkedIn virou regra do fluxo de corpo de e-mail
    # (core/email_flows.py). Fica aqui só como cinto de seguranca: se alguem
    # reabilitar o plugin, ele ainda pede confirmacao.
    "linkedin_control", "trello_control", "cakto_sales", "android_control",
    "iphone_control", "mac_control", "home_control", "browser_control",
    "computer_control", "upload", "upload_file", "purchase", "buy",
    "root_exec", "dev_agent", "code_helper", "qoder_agent", "qoder_job",
    "personas", "phrase_blocklist", "restart_pc",
})


def normalize_origin(value: object, default: str = ORIGIN_UNDECLARED) -> str:
    """Rótulo livre de origem -> um dos nomes canônicos de UV-4.

    Vazio/ausente devolve ``default`` (por padrão ``nao_declarada``: ausência
    é registrada, não presumida). Rótulo fora do vocabulário mas DECLARADO é
    conservador: vira ``automatica`` — origem que não se conhece não pode se
    vender como pedido de ninguém.
    """
    folded = _fold(value)
    if not folded:
        return str(default or ORIGIN_UNDECLARED)
    # O marcador de lacuna é idempotente: lido de um log e re-normalizado,
    # continua 'nao_declarada' — nunca vira origem automática.
    if folded == ORIGIN_UNDECLARED:
        return ORIGIN_UNDECLARED
    if folded in ACTION_ORIGINS:
        return folded
    canonical = _ORIGIN_ALIASES.get(folded)
    if canonical:
        return canonical
    return ORIGIN_AUTOMATIC


def is_sensitive_device(action: object, args: dict | None = None) -> bool:
    """True quando a ação é de dispositivo sensível (tier medium/high).

    A régua é a MESMA do portão de confirmação do bridge: o que já exige a
    voz do dono é o que uma execução automática nunca pode tocar sem pedido.
    Sem o bridge importável, vale o espelho estático (conservador: nome
    desconhecido não é sensível por aqui, mas o bridge — o dono do tier —
    também cai em high para desconhecidos; no ambiente enxuto a função é
    só o fallback do bloqueio por origem).
    """
    name = str(action or "").strip()
    if not name:
        return False
    try:
        from core import tool_bridge as _bridge
        tier = _bridge.action_risk_tier(name, args if isinstance(args, dict) else None)
        return tier in (_bridge.TIER_MEDIUM, _bridge.TIER_HIGH)
    except Exception:
        return name in _SENSITIVE_ACTIONS_FALLBACK


def origin_block_reason(action: object, origin: object,
                        args: dict | None = None) -> str:
    """Motivo do bloqueio UV-4, ou '' quando a ação pode ser despachada.

    Bloqueia SOMENTE origem explícita que canonicamente é ``automatica`` em
    dispositivo sensível. Origem vazia (não declarada) não bloqueia — é
    lacuna de rastreamento, não acusação; o log registra ``nao_declarada``.
    """
    if not str(origin or "").strip():
        return ""
    if normalize_origin(origin) != ORIGIN_AUTOMATIC:
        return ""
    if not is_sensitive_device(action, args):
        return ""
    return (f"origem {ORIGIN_AUTOMATIC} nao toca dispositivo sensivel "
            f"'{str(action or '').strip()}' (UV-4: nada de acao automatica "
            f"sem pedido do dono)")


# ── autonomous WhatsApp reply grant ─────────────────────────────────────────

# A short consent said WITHOUT a pending request must not match an ordinary
# sentence that happens to contain "pode". Only a send-shaped phrase ("pode
# enviar", "pode mandar") or a bare consent word qualifies.
_SEND_CONSENT_RE = re.compile(
    r"\b(?:pode|podes)\s+(?:enviar|mandar|envia|manda)\b")
_BARE_CONSENT = frozenset({"pode", "podes", "autorizo", "autorizado",
                           "manda", "envia", "vai"})
_CONSENT_FILLERS = frozenset({"gino", "jarvis", "eu", "falei", "disse", "ja",
                              "entao", "sim", "ok", "okay", "claro", "beleza",
                              "por", "favor", "agora", "isso", "mesmo", "ai",
                              "para", "pra", "de", "o", "a", "meu", "minha"})
_MAX_SHORT_CONSENT_WORDS = 8


def looks_like_short_consent(text: str) -> bool:
    """True when the utterance itself is a short "pode" / "pode enviar".

    This is the matcher for a consent spoken before the bridge has parked a
    request (the owner answers the question he heard; the tool call arrives
    later). It is deliberately stricter than ``looks_like_consent``: a normal
    sentence such as "você pode me dizer as horas" must never arm a send
    window. Punctuation is flattened so "Pode enviar, por favor." still
    matches.
    """
    folded = _fold(text)
    for mark in (",", ".", "!", "?", ";"):
        folded = folded.replace(mark, " ")
    words = folded.split()
    if not words or len(words) > _MAX_SHORT_CONSENT_WORDS:
        return False
    if _SEND_CONSENT_RE.search(folded):
        return True
    return (any(w in _BARE_CONSENT for w in words)
            and all(w in _BARE_CONSENT or w in _CONSENT_FILLERS for w in words))


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _reply_key(account: str, chat: str) -> tuple[str, str]:
    return (_fold(account), _digits(chat) or _fold(chat))


def allow_whatsapp_reply(account: str, chat: str, name: str = "",
                         seconds: float = AUTO_REPLY_SECONDS) -> float:
    """Park a grant for the assistant's reply to one WhatsApp contact.

    The watcher calls this after a new individual message named the assistant;
    whatsapp_reply_allowed() turns it into a preauthorization for exactly one
    kind of bridge call. Returns how long the grant lasts.
    """
    ttl = max(5.0, float(seconds))
    with _lock:
        _replies[_reply_key(account, chat)] = {
            "account": _fold(account),
            "name": str(name or ""),
            "until": time.monotonic() + ttl,
        }
    return ttl


def revoke_whatsapp_reply(account: str, chat: str) -> None:
    """Drop the grant when the reply could not even be requested."""
    with _lock:
        _replies.pop(_reply_key(account, chat), None)


def whatsapp_reply_allowed(tool: str, args: dict | None = None) -> bool:
    """The bridge's preauthorize hook for the autonomous WhatsApp reply.

    True only for ``whatsapp_control`` with ``action=send`` addressed to the
    exact contact (by name or number) that triggered the grant, on the same
    account. Everything else returns False and still needs the owner's voice.
    An omitted account is accepted because the tool itself refuses to guess
    when several accounts are paired; if it resolves, it resolves to the grant.
    """
    if _fold(tool) != "whatsapp_control":
        return False
    args = args if isinstance(args, dict) else {}
    if _fold(args.get("action")) != "send":
        return False
    to = str(args.get("to") or "").strip()
    if not to:
        return False
    account = _fold(args.get("account"))
    target = _fold(to)
    target_digits = _digits(to)
    now = time.monotonic()
    with _lock:
        for key, grant in list(_replies.items()):
            if now >= grant["until"]:
                _replies.pop(key, None)
                continue
            if account and account != grant["account"]:
                continue
            if target and target == _fold(grant["name"]):
                return True
            chat_digits = key[1]
            if target_digits and chat_digits:
                if target_digits == chat_digits:
                    return True
                # Same phone written with and without the country code.
                if (len(target_digits) >= 10 and len(chat_digits) >= 10
                        and (target_digits.endswith(chat_digits)
                             or chat_digits.endswith(target_digits))):
                    return True
    return False
