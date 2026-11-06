"""A short window in which the user has authorised root, opened by his
voice or by his verified typed password.

WHY THIS EXISTS
    The user wanted to say "pode usar sudo" and have it happen (see
    autorizar-sudo.sh for why sudo is passwordless), and the authorisation
    therefore lives HERE: a window that opens only when the user says so —
    by his voice or, since his decision of 06/11/2026 ("eu posso liberar
    QUALQUER coisa com a senha ou com a minha voz"), also by his typed
    password — expires on its own, and closes again after use. Both factors
    are full owner factors, decided in one place (core/owner_auth.
    authorized_factor); the password grant is applied by
    main._owner_password_grant, which opens this window the same way the
    spoken privilege phrase does.

THE RISK, STATED PLAINLY
    With passwordless sudo, the ONLY gate is this window plus whatever the
    router decided to run. The microphone hears the room, and the room
    includes music and TV — a lyric has already become a turn once. So the
    window is:

      • opened by an explicit privilege phrase ("pode usar sudo", "autorizo o
        sudo", "pode usar root"), matched as its own intent before any model
        sees the text and confirmed by the OWNER's voiceprint — or opened by
        the owner's password typed in his own app/WhatsApp text and verified
        against the PBKDF2 hash. The model cannot open it either way;
      • short-lived (two minutes by default), so an accidental phrase does not
        leave root hanging around for the rest of the day;
      • single-use by default: a successful privileged command consumes it,
        which is what a spoken "yes" should mean.

    Anything that must never run even inside the window is still refused by
    actions/root_exec.py's own blocklist.
"""
from __future__ import annotations

import threading
import time
import unicodedata

# How long a spoken authorisation lasts. Long enough to answer one question and
# carry on; short enough that a stray phrase from the TV is not a standing grant.
WINDOW_SECONDS = 120.0

_lock = threading.Lock()
_until = 0.0
_consumed = False

# A privilege word plus a consent word, when there is no pending request. With a
# request already on the table — Alice just asked "preciso da sua autorização" —
# a plain "pode executar" / "tem minha autorização" is enough: that is the
# answer to her question, and requiring "sudo" in it would be unnatural.
_PRIV = ("sudo", "root", "administrador", "admin")
_CONSENT = ("pode", "podes", "autorizo", "autoriza", "autorizado", "autorizacao",
            "autorização", "libera", "liberado", "confirmo", "sim", "vai",
            "manda", "usa", "use", "executa", "execute", "roda", "rode", "ok",
            "beleza")

# A bare "sim" is only accepted while a request is pending, and only in a short
# utterance — a long sentence that happens to contain "pode" is a normal turn.
_MAX_PENDING_WORDS = 8


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.split())


def looks_like_authorization(text: str, pending: bool = False) -> bool:
    """True when the utterance itself is the user granting root.

    `pending=True` when Alice has just asked "preciso da sua autorização" — then
    the answer is a short consent phrase ("pode executar", "tem minha
    autorização", "pode") and no privilege word is required. Without a pending
    request the phrase must name the privilege ("pode usar sudo"), so an
    ordinary sentence containing "pode" never opens the window by accident.
    """
    folded = _fold(text).replace(",", " ").replace(".", " ").replace("!", " ")
    words = folded.split()
    tokens = set(words)
    has_consent = any(c in tokens for c in _CONSENT)
    if pending:
        return has_consent and 0 < len(words) <= _MAX_PENDING_WORDS
    has_priv = any(p in tokens for p in _PRIV)
    return has_priv and has_consent


def open_window(seconds: float = WINDOW_SECONDS) -> float:
    """Start the window. Returns how many seconds it lasts."""
    global _until, _consumed
    with _lock:
        _until = time.monotonic() + max(5.0, float(seconds))
        _consumed = False
    return max(5.0, float(seconds))


def remaining() -> float:
    with _lock:
        return max(0.0, _until - time.monotonic())


def authorized() -> bool:
    """True while the user's authorisation (voice or typed password) is in
    force."""
    with _lock:
        return (not _consumed) and time.monotonic() < _until


def consume() -> None:
    """One privileged command used the authorisation up."""
    global _consumed
    with _lock:
        _consumed = True


def close() -> None:
    global _until, _consumed
    with _lock:
        _until = 0.0
        _consumed = False
