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

AUTONOMOUS WHATSAPP REPLIES
    A narrower grant, checked by the bridge's preauthorize hook rather than by
    confirm(): when a new individual WhatsApp message addresses the assistant
    by name, core/whatsapp_watch.py calls allow_whatsapp_reply(account, chat,
    name). That authorises exactly whatsapp_control with action=send to the
    same contact on the same account, for AUTO_REPLY_SECONDS. Any other
    action, contact or account still reaches the owner's voice window, and
    this grant never opens the sudo window.
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

# How long a refused request stays on the table, waiting for the user's answer.
# Shorter than the window itself: the question "posso enviar?" should be
# answered now or the intent is stale.
PENDING_SECONDS = 300.0

# How long the assistant has to compose the autonomous reply after a WhatsApp
# message named it. 600 s matches the execution budget of a bridge call.
AUTO_REPLY_SECONDS = 600.0

_lock = threading.Lock()
_until = 0.0
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


def remaining() -> float:
    with _lock:
        return max(0.0, _until - time.monotonic())


def authorized() -> bool:
    """True while the user's spoken authorisation is still in force."""
    with _lock:
        return time.monotonic() < _until


def close() -> None:
    global _until, _pending
    with _lock:
        _until = 0.0
        _pending = None
        _replies.clear()


def confirm(title: str, message: str) -> bool:
    """The bridge's confirmation channel — voice-only, bounded by time.

    Returns True exactly while the owner's spoken consent window is open. The
    window is not spent here: the bridge asks before the action runs, and a
    handler that aborts to ask for a missing argument (whatsapp_control without
    a contact answers "para quem?") would otherwise burn the consent on a
    question. The 120 s deadline is the lock. Outside it the request is parked
    as pending (so the next short "pode" is understood as the answer) and this
    returns False, which the bridge turns into "was not confirmed". A model can
    never reach True here by itself: only the utterance matcher can open the
    window.
    """
    if authorized():
        clear_pending()
        return True
    mark_pending(title, message)
    return False


# ── autonomous WhatsApp reply grant ─────────────────────────────────────────

def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


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
