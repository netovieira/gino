"""
core/restart.py — restarting the assistant or the PC, with a spoken gate.

WHY A SECOND CONFIRMATION MODULE
    core/confirm.py puts an irreversible action behind a button on the HUD. The
    user explicitly chose *voice-only* restarts (no HUD button), so the gate
    here is a phrase instead of a click:

      1. ``request_assistant(reason)`` / ``request_pc(reason)`` stores a pending
         request and returns a sentence telling the model to ask the owner to
         say the phrase out loud. Nothing runs.
      2. ``confirm(phrase)`` runs only when the phrase contains
         "confirmo reiniciar". A request the model makes on its own does not
         execute.
      3. ``cancel()`` throws the pending request away.

    The phrase is a gate, not a proof of identity: the voiceprint check belongs
    to the audio layer, which is why this module stays small and does not try to
    listen. What it guarantees is that *nothing irreversible happens in the same
    call that asks for it*.

WHY THE HELPER IS DETACHED
    A process cannot reliably re-exec itself while Qt, the audio stream and the
    Live session are still shutting down. So the assistant restart spawns a
    tiny shell that waits for the current PID to disappear, then runs
    ``run-jarvis.sh --tray`` in a new session. The app then asks ``main`` for a
    clean shutdown via ``shutdown_cb``. If the hook is not bound we refuse
    rather than spawn a helper that would wait forever.

RATE LIMIT
    One assistant restart per five minutes. A restart loop is the worst failure
    mode of self-improvement: the assistant edits itself, restarts, crashes,
    restarts, and the user cannot reach it to stop it. The PC restart is not
    rate-limited because it does not come back by itself.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

RUN_SCRIPT = "/home/anthero/jarvis/run-jarvis.sh"

# How long a pending request stays useful. Long enough for the model to finish
# its sentence and the user to answer; short enough that a forgotten "confirmo
# reiniciar" from ten minutes ago cannot fire.
CONFIRM_TIMEOUT_SECONDS = 120.0

# One assistant restart per five minutes.
RATE_LIMIT_SECONDS = 300.0

# The gate. Accent- and case-insensitive, but the words themselves are fixed.
CONFIRM_MARKER = "confirmo reiniciar"

_PHRASES = {
    "assistant": "confirmo reiniciar o assistente",
    "pc":        "confirmo reiniciar o computador",
}

_RATE_MESSAGE = "one assistant restart per 5 minutes"


@dataclass
class _PendingRequest:
    kind:     str            # "assistant" | "pc"
    reason:   str
    phrase:   str
    at:       float          # time.monotonic(), for expiry


def _normalize(text: object) -> str:
    """Lower-case, strip accents, collapse whitespace — for the phrase only."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pc_command_hint() -> str:
    """The command actions.computer_settings.restart_computer would run here."""
    if sys.platform.startswith("win"):
        return "shutdown /r /t 10"
    if sys.platform == "darwin":
        return 'osascript -e \'tell application "System Events" to restart\''
    return "systemctl reboot"


def _helper_command(pid: int) -> str:
    """Shell that outlives us: wait for `pid`, then start JARVIS again."""
    return (f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.2; done; "
            f"exec {shlex.quote(RUN_SCRIPT)} --tray")


class RestartManager:
    """Two-step restart requests. One instance per app, bound in main."""

    def __init__(self, dry_run: bool = False,
                 logger: Optional[Callable[[str], None]] = print):
        self.dry_run = bool(dry_run)
        self._logger = logger or (lambda _msg: None)

        self._lock = threading.Lock()
        self._pending: Optional[_PendingRequest] = None
        self._last_assistant_at: Optional[float] = None

        self._shutdown_cb: Optional[Callable[[], None]] = None
        self._confirm_cb: Optional[Callable[[str], None]] = None
        self._pc_restart: Optional[Callable[[], object]] = None

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(self, shutdown_cb: Optional[Callable[[], None]] = None,
             confirm_cb: Optional[Callable[[str], None]] = None,
             pc_restart: Optional[Callable[[], object]] = None) -> None:
        """Wire the manager to the app.

        shutdown_cb: called with no arguments to end the app cleanly.
        confirm_cb:  called with the sentence to speak, asking for the phrase
                     (optional — the model can do the asking itself).
        pc_restart:  the callable that reboots the machine; defaults to
                     actions.computer_settings.restart_computer at execution
                     time, never at import time.
        """
        self._shutdown_cb = shutdown_cb if callable(shutdown_cb) else None
        self._confirm_cb = confirm_cb if callable(confirm_cb) else None
        self._pc_restart = pc_restart if callable(pc_restart) else None

    def _log(self, message: str) -> None:
        try:
            self._logger(f"[Restart] {_short(message, 200)}")
        except Exception:
            pass

    # ── state ───────────────────────────────────────────────────────────────

    def pending(self) -> Optional[dict]:
        """The pending request as plain data, or None. Callables never leave."""
        with self._lock:
            p = self._pending
            if p is None:
                return None
            age = time.monotonic() - p.at
            if age > CONFIRM_TIMEOUT_SECONDS:
                self._pending = None
                return None
            return {
                "kind":         p.kind,
                "reason":       p.reason,
                "phrase":       p.phrase,
                "age_seconds":  int(max(age, 0.0)),
                "expires_in":   int(max(CONFIRM_TIMEOUT_SECONDS - age, 0.0)),
            }

    # ── step one: ask ───────────────────────────────────────────────────────

    def _request(self, kind: str, reason: str) -> str:
        reason = _short(reason)
        now = time.monotonic()

        with self._lock:
            current = self._pending
            if current is not None and now - current.at <= CONFIRM_TIMEOUT_SECONDS:
                return (f"A confirmation is already pending for the "
                        f"{current.kind} restart. Finish it with action='confirm' "
                        f"or drop it with action='cancel' first. Nothing new was done.")
            self._pending = None

            if kind == "assistant" and self._last_assistant_at is not None:
                elapsed = now - self._last_assistant_at
                if elapsed < RATE_LIMIT_SECONDS:
                    remaining = int(RATE_LIMIT_SECONDS - elapsed) + 1
                    return (f"Assistant restart is rate-limited to {_RATE_MESSAGE}; "
                            f"try again in {remaining} seconds. Nothing was done.")

            request = _PendingRequest(kind=kind, reason=reason,
                                      phrase=_PHRASES[kind], at=now)
            self._pending = request

        self._log(f"{kind} restart requested"
                  + (f" — reason: {reason}" if reason else ""))

        if self._confirm_cb is not None and not self.dry_run:
            try:
                self._confirm_cb(
                    f"Ask the user to say '{request.phrase}' out loud to restart "
                    f"the {kind}.")
            except Exception as e:
                with self._lock:
                    if self._pending is request:
                        self._pending = None
                self._log(f"confirmation prompt failed — {e}")
                return (f"Could not ask for the spoken confirmation: {e}. "
                        f"Nothing was done.")

        return (
            f"[CONFIRMATION_PENDING] A spoken confirmation is needed to restart "
            f"the {kind}. Nothing has restarted. Ask the user to say exactly "
            f"'{request.phrase}' out loud, then call action='confirm' with that "
            f"phrase. Until the phrase is confirmed, do not claim it happened."
        )

    def request_assistant(self, reason: str = "") -> str:
        """Park an assistant restart; returns the sentence for the model."""
        return self._request("assistant", reason)

    def request_pc(self, reason: str = "") -> str:
        """Park a PC restart; returns the sentence for the model."""
        return self._request("pc", reason)

    # ── step two: confirm ───────────────────────────────────────────────────

    def confirm(self, phrase: str = "") -> str:
        """Execute the pending request only if `phrase` contains the marker."""
        with self._lock:
            p, self._pending = self._pending, None

        if p is None:
            return "There is no restart awaiting confirmation. Nothing was done."

        age = time.monotonic() - p.at
        if age > CONFIRM_TIMEOUT_SECONDS:
            self._log(f"{p.kind} confirmation expired")
            return ("The restart request expired before it was confirmed. "
                    "Nothing was done; ask for it again if still wanted.")

        if CONFIRM_MARKER not in _normalize(phrase):
            # Keep the request alive: the user may simply have been misheard.
            with self._lock:
                if self._pending is None:
                    self._pending = p
            return (f"Restart not confirmed: the user must say the exact phrase "
                    f"'{p.phrase}' out loud. Nothing was done.")

        if p.kind == "assistant":
            return self._run_assistant(p)
        return self._run_pc(p)

    def _rate_limited(self) -> Optional[str]:
        with self._lock:
            last = self._last_assistant_at
            now = time.monotonic()
        if last is None:
            return None
        elapsed = now - last
        if elapsed >= RATE_LIMIT_SECONDS:
            return None
        remaining = int(RATE_LIMIT_SECONDS - elapsed) + 1
        return (f"Assistant restart is rate-limited to {_RATE_MESSAGE}; "
                f"try again in {remaining} seconds. Nothing was done.")

    def _run_assistant(self, p: _PendingRequest) -> str:
        blocked = self._rate_limited()
        if blocked:
            self._log("assistant restart refused by rate limit")
            return blocked

        pid = os.getpid()
        script = _helper_command(pid)

        if self.dry_run:
            self._log(f"dry run — would run: {script}")
            with self._lock:
                # Keep the rate limit meaningful in dry run too, so the same
                # protocol can be tested without ever spawning a process.
                self._last_assistant_at = time.monotonic()
            return (f"[DRY RUN] Nothing was executed. Would run: "
                    f"/bin/sh -c {shlex.quote(script)} and then end this app.")

        if self._shutdown_cb is None:
            self._log("assistant restart refused — no shutdown hook bound")
            return ("Cannot restart the assistant: the app has not bound a "
                    "shutdown hook, so it could not close cleanly. Nothing was done.")

        if not os.path.isfile(RUN_SCRIPT):
            self._log(f"assistant restart refused — {RUN_SCRIPT} not found")
            return (f"Cannot restart the assistant: {RUN_SCRIPT} was not found. "
                    f"Nothing was done.")

        try:
            subprocess.Popen(
                ["/bin/sh", "-c", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as e:
            self._log(f"could not spawn restart helper — {e}")
            return (f"Could not schedule the assistant restart: {e}. "
                    f"Nothing was done.")

        with self._lock:
            self._last_assistant_at = time.monotonic()

        self._log("assistant restart scheduled"
                  + (f" — reason: {p.reason}" if p.reason else ""))
        try:
            self._shutdown_cb()
        except Exception as e:
            self._log(f"shutdown hook failed — {e}")
            return (f"The restart helper is waiting, but the shutdown hook "
                    f"failed: {e}. The app may need to be closed manually.")
        return ("Restarting the assistant now. The window closes and reopens "
                "in a few seconds.")

    def _run_pc(self, p: _PendingRequest) -> str:
        if self.dry_run:
            self._log("dry run — would reboot the PC")
            return (f"[DRY RUN] Nothing was executed. Would call the PC restart "
                    f"({_pc_command_hint()} on this platform).")

        fn = self._pc_restart
        if fn is None:
            try:
                from actions.computer_settings import restart_computer
            except Exception as e:
                self._log(f"PC restart unavailable — {e}")
                return f"PC restart is unavailable: {e}. Nothing was done."
            fn = restart_computer

        self._log("PC restart executing"
                  + (f" — reason: {p.reason}" if p.reason else ""))
        try:
            fn()
        except Exception as e:
            self._log(f"PC restart failed — {e}")
            return f"PC restart failed: {e}. Nothing was done."
        return "Restarting the computer now. It will shut down in a few seconds."

    # ── step zero: undo the request ─────────────────────────────────────────

    def cancel(self) -> str:
        """Drop the pending request. Safe to call when there is none."""
        with self._lock:
            had = self._pending is not None
            self._pending = None
        if not had:
            return "There is no restart waiting to be cancelled. Nothing was done."
        self._log("restart cancelled")
        return "Restart cancelled. Nothing was done."

    # The Qoder tool bridge looks for these names (see core/tool_bridge.py).
    def assistant(self, reason: str = "") -> str:
        return self.request_assistant(reason=reason)

    def pc(self, reason: str = "") -> str:
        return self.request_pc(reason=reason)

    def status(self) -> Optional[dict]:
        return self.pending()
