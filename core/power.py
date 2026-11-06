"""Power actions with an owner-confirmed gate (spoken phrase or typed password).

Kinds:
    shutdown  systemctl poweroff / shutdown -h now
    suspend   systemctl suspend
    lock      loginctl lock-session (or the desktop's locker)
    unlock    loginctl unlock-session

Lock is reversible and runs immediately after the owner check. Shutdown,
suspend and unlock park a pending request and only execute when the owner says
the exact phrase. Voice is an identity hint, not strong authentication: the OS
may still ask for the account password (especially for unlock).
"""
from __future__ import annotations

import platform
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional

PENDING_TIMEOUT_SECONDS = 60.0
RATE_LIMIT_SECONDS = 300.0
PHRASES = {
    "shutdown": "confirmo desligar",
    "suspend": "confirmo suspender",
    "unlock": "confirmo desbloquear",
}
LABELS = {
    "shutdown": "desligar o computador",
    "suspend": "suspender o computador",
    "lock": "bloquear a tela",
    "unlock": "desbloquear a tela",
}


def _normalize(text: object) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()


@dataclass
class _Pending:
    kind: str
    reason: str
    at: float


class PowerManager:
    def __init__(
        self,
        dry_run: bool = False,
        logger: Callable[[str], None] = print,
        platform_name: str | None = None,
        runner: Optional[Callable[[list[str]], object]] = None,
        unlock_fn: Optional[Callable[[], str]] = None,
    ) -> None:
        self.dry_run = bool(dry_run)
        self._log_fn = logger or (lambda _m: None)
        self._platform = platform_name or platform.system()
        self._runner = runner
        self._unlock_fn = unlock_fn
        self._lock = threading.RLock()
        self._pending: Optional[_Pending] = None
        self._last_at: dict[str, float] = {}

    # ── state ───────────────────────────────────────────────────────────────
    def pending(self) -> Optional[dict]:
        with self._lock:
            p = self._pending
            if p is None:
                return None
            age = time.monotonic() - p.at
            if age > PENDING_TIMEOUT_SECONDS:
                self._pending = None
                return None
            return {"kind": p.kind, "reason": p.reason,
                    "phrase": PHRASES.get(p.kind, ""),
                    "age_seconds": int(age),
                    "expires_in": int(max(0, PENDING_TIMEOUT_SECONDS - age))}

    def _log(self, message: str) -> None:
        try:
            self._log_fn(f"[Power] {message}")
        except Exception:
            pass

    def _rate_limited(self, kind: str) -> bool:
        with self._lock:
            last = self._last_at.get(kind)
            now = time.monotonic()
        return last is not None and now - last < RATE_LIMIT_SECONDS

    # ── step one: ask ───────────────────────────────────────────────────────
    def request(self, kind: str, reason: str = "") -> str:
        kind = str(kind or "").strip().lower()
        if kind not in PHRASES:
            return f"Ação de energia desconhecida: {kind}."
        with self._lock:
            current = self._pending
            if current is not None and time.monotonic() - current.at <= PENDING_TIMEOUT_SECONDS:
                return (f"Já existe um pedido de {LABELS.get(current.kind, current.kind)} "
                        "aguardando confirmação.")
            self._pending = None
            if self._rate_limited(kind):
                return (f"Ação de {LABELS[kind]} está limitada a uma vez a cada "
                        f"{int(RATE_LIMIT_SECONDS/60)} minutos. Nada foi feito.")
            self._pending = _Pending(kind=kind, reason=str(reason or "")[:200],
                                     at=time.monotonic())
        self._log(f"{kind} requested")
        return (f"[CONFIRMATION_PENDING] Diga exatamente '{PHRASES[kind]}' para "
                f"{LABELS[kind]}. Nada foi feito ainda.")

    # ── step two: execute ───────────────────────────────────────────────────
    def confirm(self, phrase: str = "") -> str:
        with self._lock:
            p, self._pending = self._pending, None
        if p is None:
            return "Não há ação de energia aguardando confirmação. Nada foi feito."
        if time.monotonic() - p.at > PENDING_TIMEOUT_SECONDS:
            return "O pedido de energia expirou. Nada foi feito."
        if PHRASES[p.kind] not in _normalize(phrase):
            with self._lock:
                if self._pending is None:
                    self._pending = p
            return (f"Não confirmei. Diga exatamente '{PHRASES[p.kind]}' ou 'cancela'.")
        try:
            out = self.execute(p.kind)
        except Exception as e:
            self._log(f"{p.kind} failed: {e}")
            return f"Não consegui {LABELS[p.kind]}: {str(e)[:160]}. Nada foi feito."
        with self._lock:
            self._last_at[p.kind] = time.monotonic()
        self._log(f"{p.kind} executed")
        return out

    def execute_owner(self, kind: str, reason: str = "") -> str:
        """Run a power action the OWNER just ordered out loud.

        Same owner rule as ``RestartManager.confirm_owner``: the direct order
        is the authorisation, and the identity proof is the audio layer's
        privileged voiceprint (this method is never reached by a model tool
        call). Rate limit and platform support still apply; a parked request
        of another kind is dropped so it cannot fire later.
        """
        kind = str(kind or "").strip().lower()
        if kind not in PHRASES:
            return f"Ação de energia desconhecida: {kind}."
        with self._lock:
            self._pending = None
        if kind == "lock":
            return self.lock()
        if self._rate_limited(kind):
            self._log(f"{kind} refused by rate limit (direct owner order)")
            return (f"Ação de {LABELS[kind]} está limitada a uma vez a cada "
                    f"{int(RATE_LIMIT_SECONDS/60)} minutos. Nada foi feito.")
        try:
            out = self.execute(kind)
        except Exception as e:
            self._log(f"{kind} failed: {e}")
            return f"Não consegui {LABELS[kind]}: {str(e)[:160]}. Nada foi feito."
        with self._lock:
            self._last_at[kind] = time.monotonic()
        self._log(f"{kind} executed (direct owner order)"
                  + (f" — {str(reason)[:80]}" if reason else ""))
        return out

    def cancel(self) -> str:
        with self._lock:
            had = self._pending is not None
            self._pending = None
        return "Pedido de energia cancelado." if had else "Não há pedido de energia pendente."

    # ── lock is immediate/reversible ────────────────────────────────────────
    def lock(self) -> str:
        try:
            self.execute("lock")
            return "Tela bloqueada."
        except Exception as e:
            self._log(f"lock failed: {e}")
            return f"Não consegui bloquear a tela: {str(e)[:160]}."

    # ── commands ────────────────────────────────────────────────────────────
    def _run(self, cmd: list[str], timeout: float = 15.0) -> None:
        if self.dry_run:
            self._log(f"dry run — would run: {cmd}")
            return
        if self._runner is not None:
            r = self._runner(cmd)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = int(getattr(r, "returncode", 0) or 0)
        if rc != 0:
            detail = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip()
            raise RuntimeError(detail[:200] or f"{cmd[0]} falhou (rc={rc})")

    def _run_first(self, choices: list[list[str]]) -> None:
        last: Optional[Exception] = None
        for cmd in choices:
            try:
                self._run(cmd)
                return
            except Exception as e:
                last = e
        raise last or RuntimeError("nenhum comando de energia funcionou")

    def execute(self, kind: str) -> str:
        kind = str(kind or "").strip().lower()
        if self._platform == "Linux":
            if kind == "shutdown":
                self._run(["systemctl", "poweroff"])
                return "Desligando o computador agora."
            if kind == "suspend":
                self._run(["systemctl", "suspend"])
                return "Suspendendo o computador agora."
            if kind == "lock":
                self._run_first([
                    ["loginctl", "lock-session"],
                    ["xdg-screensaver", "lock"],
                    ["gnome-screensaver-command", "-l"],
                    ["dm-tool", "lock"],
                ])
                return "Tela bloqueada."
            if kind == "unlock":
                if self._unlock_fn is not None:
                    return self._unlock_fn()
                self._run(["loginctl", "unlock-session"])
                return "Tela desbloqueada."
        elif self._platform == "Windows":
            if kind == "shutdown":
                self._run(["shutdown", "/s", "/t", "5"])
                return "Desligando o computador agora."
            if kind == "suspend":
                self._run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
                return "Suspendendo o computador agora."
            if kind == "lock":
                self._run(["rundll32.exe", "user32.dll,LockWorkStation"])
                return "Tela bloqueada."
            if kind == "unlock":
                raise RuntimeError("desbloqueio por voz não é suportado no Windows")
        elif self._platform == "Darwin":
            if kind == "shutdown":
                self._run(["osascript", "-e", 'tell application "System Events" to shut down'])
                return "Desligando o computador agora."
            if kind == "suspend":
                self._run(["pmset", "sleepnow"])
                return "Suspendendo o computador agora."
            if kind == "lock":
                self._run(["pmset", "displaysleepnow"])
                return "Tela bloqueada."
            if kind == "unlock":
                raise RuntimeError("desbloqueio por voz não é suportado no macOS")
        raise RuntimeError(f"ação de energia não suportada: {kind}")
