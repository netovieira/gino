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
    utterance matched before any model sees the text, short-lived, single-use.
    Unlike rootgate there is no privilege word to require — the equivalent of
    "the user named sudo" here is "a risky action is already pending": without
    a request on the table, nothing in this module opens a window at all, so a
    stray "pode" from the TV authorises nothing.

SEPARATE FROM ROOT
    This window authorises ordinary risky actions (sending a message, remote
    device control). It never authorises sudo or root — core/rootgate.py owns
    that, with its own window. Opening one does not open the other.
"""
from __future__ import annotations

import threading
import time

from core import rootgate

# How long a spoken consent for a pending bridge request lasts once accepted.
WINDOW_SECONDS = 120.0

# How long a refused request stays on the table, waiting for the user's answer.
# Shorter than the window itself: the question "posso enviar?" should be
# answered now or the intent is stale.
PENDING_SECONDS = 300.0

_lock = threading.Lock()
_until = 0.0
_consumed = False
_pending: dict | None = None


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
    global _until, _consumed
    with _lock:
        _until = time.monotonic() + max(5.0, float(seconds))
        _consumed = False
    return max(5.0, float(seconds))


def remaining() -> float:
    with _lock:
        return max(0.0, _until - time.monotonic())


def authorized() -> bool:
    """True while the user's spoken authorisation is still in force."""
    with _lock:
        return (not _consumed) and time.monotonic() < _until


def consume() -> None:
    """One risky action used the authorisation up."""
    global _consumed
    with _lock:
        _consumed = True


def close() -> None:
    global _until, _consumed, _pending
    with _lock:
        _until = 0.0
        _consumed = False
        _pending = None


def confirm(title: str, message: str) -> bool:
    """The bridge's confirmation channel — voice-only, single-use.

    Returns True exactly when the owner has opened a window by speaking a
    consent phrase, and consumes it. Otherwise it parks the request as pending
    (so the next short "pode" is understood as the answer) and returns False,
    which the bridge turns into "was not confirmed". A model can never reach
    True here by itself: only the utterance matcher can open the window.
    """
    if authorized():
        consume()
        clear_pending()
        return True
    mark_pending(title, message)
    return False
