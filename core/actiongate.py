"""A short window in which the user has authorised a risky common action, opened by voice.

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
    device control). It never authorises sudo or root — core/rootgate.py owns
    that, with its own window. Opening one does not open the other.

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
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
import weakref

from core import rootgate

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
_pending: dict | None = None
# (account, chat) -> {"account": folded, "name": contact, "until": monotonic}
_replies: dict[tuple[str, str], dict] = {}
# Callbacks told when the owner grants a new window. The bridge uses this to
# drop its "already denied" marks: a fresh "pode" is a new authorisation, so
# the same call must be allowed to reach the normal gate again.
_grant_listeners: list[weakref.ref] = []


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


def open_window(seconds: float = WINDOW_SECONDS) -> float:
    """Start the window after a spoken consent. Returns how long it lasts."""
    global _until
    with _lock:
        _until = time.monotonic() + max(5.0, float(seconds))
    # A new consent renews everything that had been refused: the bridge
    # forgets the calls it had marked as "already denied".
    _notify_grant()
    return max(5.0, float(seconds))


def arm_send_window(seconds: float = CONSENT_TTL_SECONDS) -> float:
    """Remember a consent spoken BEFORE the bridge parked its request.

    The live failure of 02/11/2026: the owner answered "Pode enviar" at
    16:04:16 and 16:12:11, but the bridge only parked the whatsapp_control
    request at 16:04:33 and 16:12:27 — after the words. ``confirm()`` then
    found no pending request at the moment of the utterance and the consent
    was lost. This armed window is checked when the call finally arrives and
    authorises only a WhatsApp send, for a bounded time. It is not a standing
    grant and it never opens the sudo window.
    """
    global _send_until
    with _lock:
        _send_until = time.monotonic() + max(5.0, float(seconds))
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
    """True while the user's spoken authorisation is still in force.

    Includes the 15-minute window armed by a "pode enviar" that arrived before
    the bridge's request was parked (``arm_send_window``). The bridge uses
    this probe to reopen a previously refused request; ``confirm`` scopes that
    armed window to whatsapp_control, so an unrelated risky action still needs
    the ordinary 120 s pending window.
    """
    with _lock:
        now = time.monotonic()
        return now < _until or now < _send_until


def close() -> None:
    global _until, _send_until, _pending
    with _lock:
        _until = 0.0
        _send_until = 0.0
        _pending = None
        _replies.clear()


def confirm(title: str, message: str) -> bool:
    """The bridge's confirmation channel — voice-only, bounded by time.

    Returns True while the owner's spoken consent window is open, or while the
    armed send window is open and the call is a whatsapp_control action. The
    window is not spent here: the bridge asks before the action runs, and a
    handler that aborts to ask for a missing argument (whatsapp_control without
    a contact answers "para quem?") would otherwise burn the consent on a
    question. The deadline is the lock. Outside it the request is parked as
    pending (so the next short "pode" is understood as the answer) and this
    returns False, which the bridge turns into "was not confirmed". A model can
    never reach True here by itself: only the utterance matcher can open the
    window.
    """
    if _window_open():
        clear_pending()
        return True
    if _send_window_open() and "whatsapp_control" in str(title or "").lower():
        clear_pending()
        return True
    mark_pending(title, message)
    return False


# ── autonomous WhatsApp reply grant ─────────────────────────────────────────

def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


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
