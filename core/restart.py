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
         execute. Since the owner's order of 10/11/2026 this gate covers the
         MACHINE only: the assistant's own process is restarted by
         ``self_restart()``, with no phrase (see SELF RESTART below).
      3. ``cancel()`` throws the pending request away.

    The phrase is a gate, not a proof of identity: the voiceprint check belongs
    to the audio layer, which is why this module stays small and does not try to
    listen. Since the owner's decision of 06/11/2026 the typed password is the
    equal second factor: main's password grant confirms a pending request here
    by calling ``confirm`` with the parked phrase — never a model tool call.
    What it guarantees is that *nothing irreversible happens in the same
    call that asks for it*.

WHY THE HELPER IS DETACHED — AND NOT ONLY DETACHED
    A process cannot reliably re-exec itself while Qt, the audio stream and the
    Live session are still shutting down. So the assistant restart spawns a
    tiny shell that waits for the current PID to disappear, then runs
    ``run-jarvis.sh --tray`` in a new session. The app then asks ``main`` for a
    clean shutdown via ``shutdown_cb``. If the hook is not bound we refuse
    rather than spawn a helper that would wait forever.

    A NEW SESSION IS NOT A NEW CGROUP (05/11/2026): the helper born with
    ``start_new_session=True`` stayed in the app's systemd scope and was swept
    with it when GNOME stopped the scope — every automatic relaunch today died
    before waiting. The helper must ESCAPE the cgroup (see
    ``_spawn_relaunch_helper``): preferred is a transient ``systemd-run --user``
    oneshot unit; fallback is setsid plus explicit migration to a delegated
    cgroup leaf; last resort is the old in-scope helper, logged as such.

WHY THE PENDING REQUEST IS ON DISK
    The manager used to keep the parked request only in memory. The model's
    tool call and the owner's spoken phrase are handled in the same process,
    but a request that outlived its timer silently disappeared — and ``status``
    then said "no restart awaiting" instead of admitting the request had
    expired. Now the request is written to ``memory/restart_pending.json`` (it
    survives a process restart too), and the status answer distinguishes
    "nothing was asked" from "it expired". When it expires, the file is *moved*
    to ``memory/restart_last_pending.json`` instead of being deleted, so a late
    "confirmo reiniciar" can renew the request with the reason it always had.

RATE LIMIT
    One assistant restart per five minutes. A restart loop is the worst failure
    mode of self-improvement: the assistant edits itself, restarts, crashes,
    restarts, and the user cannot reach it to stop it. The PC restart is not
    rate-limited because it does not come back by itself.

SELF RESTART IS A POWER, NOT A REQUEST (owner, 10/11/2026, audio)
    "Eu gostaria de te dar autonomia de se reiniciar... qualquer pessoa pode
    pedir para você fazer... reiniciar o computador não — mas reiniciar você
    mesmo, você pode ter essa autonomia a partir de agora."

    ``self_restart()`` is that power: restarting the assistant's OWN process
    needs no spoken phrase, from anyone — the owner, another enrolled voice at
    the machine, or the assistant's own initiative in a test. ``restart_pc``
    keeps the old protocol exactly as it was: only the owner's exact phrase
    releases the machine. There is no ``kind`` parameter in the new path and it
    never reaches ``_run_pc``, which is what keeps the machine behind the gate.

    Three limits the owner's order does not remove, encoded here as guards:

      * GUARD 1 — never kill a running executor/subagent. While
        ``core/qoder_jobs`` is busy the restart is postponed, never dropped.
      * GUARD 2 — never restart in the middle of an answer. While the app is
        still delivering (TTS queue or the outgoing chat note), the restart is
        postponed. The effect happens in ``flush_self_restart()``, which the
        app calls when the current answer finished leaving.
      * GUARD 3 — one restart per ceiling. The five-minute rate limit is the
        ceiling (it survives the boot it guards, through
        ``memory/restart_last.json``), and a single deferred slot means one
        delivery batch can only ever queue one restart.

    Every decision writes one line to ``memory/restart_trail.jsonl``: who
    asked, why, and which guard postponed it. The trail is redacted — no
    secret and no contact number ever lands there.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

RUN_SCRIPT = "/home/anthero/jarvis/run-jarvis.sh"

BASE_DIR = Path(__file__).resolve().parent.parent
# Local, git-ignored state next to the other memory/*.json files.
STATE_PATH = BASE_DIR / "memory" / "restart_pending.json"
LAST_PATH = BASE_DIR / "memory" / "restart_last.json"
# The last request that timed out. Kept so a late confirmation can be renewed
# with the original kind/reason instead of being re-parked as a blank request.
ARCHIVE_PATH = BASE_DIR / "memory" / "restart_last_pending.json"
# Trilha de toda decisão de reinício do próprio processo (limite 4 do dono).
TRAIL_PATH = BASE_DIR / "memory" / "restart_trail.jsonl"
TRAIL_MAX_LINES = 200
# Guarda 3: o teto anti-loop é o RATE_LIMIT_SECONDS de sempre (5 minutos,
# persistido em restart_last.json) — um reinício-auto por janela, nunca em
# loop. O abaixo só limita QUANTO TEMPO um reinício adiado fica esperando.
SELF_DEFER_TTL_SECONDS = 900.0
_TRAIL_MAX_CHARS = 120
_TRAIL_NUMBER_RE = re.compile(r"\+?(?:\d[\s().-]){7,}\d")
# "senha=xyz", "token: xyz", "api_key = xyz": a trilha guarda o fato de que
# alguém escreveu uma chave, nunca o valor dela.
_TRAIL_SECRET_RE = re.compile(
    r"(?i)\b(senha|password|passwd|token|secret|api[_-]?key|apikey|chave|"
    r"otp|pin)\b\s*[:=]\s*[^\s,;]+")
_TRAIL_REDACTED = "[retido]"

# Quem pediu o reinício do processo. A trilha só conhece estes três rótulos
# mais "origem nao declarada": nada aqui pode virar nome, número ou segredo.
REQUESTER_OWNER = "dono"
REQUESTER_VOICE = "outra voz"
REQUESTER_SELF = "iniciativa propria"
REQUESTER_UNKNOWN = "origem nao declarada"
REQUESTER_LABELS = (REQUESTER_OWNER, REQUESTER_VOICE, REQUESTER_SELF,
                    REQUESTER_UNKNOWN)
_REQUESTER_ALIASES = {
    "dono": REQUESTER_OWNER, "owner": REQUESTER_OWNER,
    "outra voz": REQUESTER_VOICE, "outra": REQUESTER_VOICE,
    "voz cadastrada": REQUESTER_VOICE, "voz nao dona": REQUESTER_VOICE,
    "voice": REQUESTER_VOICE, "guest": REQUESTER_VOICE,
    "person": REQUESTER_VOICE, "anyone": REQUESTER_VOICE,
    "iniciativa propria": REQUESTER_SELF, "self": REQUESTER_SELF,
    "modelo": REQUESTER_SELF, "model": REQUESTER_SELF,
    "teste": REQUESTER_SELF, "autonomous": REQUESTER_SELF,
    "own initiative": REQUESTER_SELF,
}

# Wall clock of this process. A request saved by a PREVIOUS process is stale by
# definition — the assistant already restarted, so asking again after boot is
# how the old loop was born ("reinício continua pendente" forever).
_PROCESS_STARTED_WALL = time.time()
_BOOT_GRACE_S = 2.0

# How long a pending request stays useful. Long enough for the owner to come
# back after a failed voiceprint or a busy turn; short enough that a forgotten
# "confirmo reiniciar" cannot fire much later.
#
# 30 minutes, not the original 15, by the owner's decision on 02/11/2026: a
# request parked at 11:23 and confirmed at 11:51 had expired, the phrase fell
# through to the "start a restart" gate and parked a new request in silence —
# a confirmation loop that never restarted anything. Widening the window keeps
# a legitimate "I stepped away" confirmation alive; the rate limit stays as is.
CONFIRM_TIMEOUT_SECONDS = 1800.0

# One assistant restart per five minutes.
RATE_LIMIT_SECONDS = 300.0

# The gate. Accent- and case-insensitive, but the words themselves are fixed.
CONFIRM_MARKER = "confirmo reiniciar"

_PHRASES = {
    "assistant": "confirmo reiniciar o assistente",
    "pc":        "confirmo reiniciar o computador",
}

_RATE_MESSAGE = "one assistant restart per 5 minutes"

# Origem do reinício: o manager NUNCA inventa que o dono ordenou. Um reason
# vazio vira "origem não informada"; o rótulo antigo que main.py gravava por
# detecção automática de intenção não é ordem literal do dono e é registrado
# como o que de fato foi: uma detecção do app.
UNKNOWN_ORIGIN = "origem não informada"
AUTO_DETECTION_REASON = (
    "detecção automática de intenção — fala sobre reiniciar, não ordem literal")
# Rótulos que o caminho automático usava antes desta correção (main.py ainda
# passa "ordem direta do dono" até a fatia que mexe nele). Nunca são aceitos
# como prova de ordem: viram AUTO_DETECTION_REASON.
_LEGACY_ORDER_LABELS = (
    "ordem direta do dono",
    "ordered directly by the owner",
    "direct order from the owner",
    "ordem direta",
)


def recorded_origin(reason: str = "", previous: str = "") -> str:
    """Texto gravado como origem: real, sem atribuir ordem ao dono.

    ``reason`` é o que o chamador informou; ``previous`` é o reason do pedido
    que já estava estacionado. Vazio nos dois vira UNKNOWN_ORIGIN. Um rótulo
    legado de ordem direta é tratado como detecção automática — ele nasceu de
    um regex de intenção, não de uma ordem literal verificada.
    """
    received = _short(reason) or _short(previous)
    if not received:
        return UNKNOWN_ORIGIN
    if _normalize(received) in {_normalize(label)
                                for label in _LEGACY_ORDER_LABELS}:
        return AUTO_DETECTION_REASON
    return received


def normalize_requester(value: object) -> str:
    """Quem pediu, reduzido a um dos rótulos da trilha.

    Chamador nenhum é credível para si mesmo: "dono" só vale quando quem
    chama é o portão de voz que acabou de conferir a voz dele. O modelo e a
    ponte passam "iniciativa propria"; nada aqui copia texto livre.
    """
    return _REQUESTER_ALIASES.get(_normalize(value), REQUESTER_UNKNOWN)


def _redact_for_trail(text: object) -> str:
    """Motivo/guarda como vão entrar na trilha: curto e sem segredo."""
    raw = _short(str(text or ""), _TRAIL_MAX_CHARS)
    if not raw:
        return ""
    try:
        from core.guide_panel import redact
        raw = str(redact(raw))
    except Exception:
        pass
    raw = _TRAIL_SECRET_RE.sub(lambda m: f"{m.group(1)}={_TRAIL_REDACTED}", raw)
    raw = _TRAIL_NUMBER_RE.sub(_TRAIL_REDACTED, raw)
    return " ".join(raw.split())


def _jobs_busy() -> bool:
    """Guarda 1, no código: existe executor/subagente vivo agora?"""
    try:
        from core import qoder_jobs
        return bool(qoder_jobs.is_busy())
    except Exception:
        return False


@dataclass
class _PendingRequest:
    kind:     str            # "assistant" | "pc"
    reason:   str
    phrase:   str
    at:       float          # time.monotonic(), for expiry
    requester: str = ""      # quem pediu o reinício do próprio processo


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


def _helper_unit_name(pid: int) -> str:
    """Nome único da unidade transitória; também é o marcador de proveniência.

    core/boot_audit lê esse prefixo no cgroup do processo novo para dizer que
    o boot veio do relançamento automático, não de um clique no ícone.
    """
    return f"jarvis-relaunch-{int(pid)}-{int(time.time())}"


def systemd_run_helper_argv(script: str, unit: str) -> list[str]:
    """Escape 1 (escolhido): unidade transitória sob o gerenciador do usuário.

    ``systemd-run --user`` nasce fora do scope do app (o GNOME não varre a
    unidade quando mata o scope) e ``--collect`` a descarta ao terminar.
    Testado nesta máquina em 05/11/2026 com o app vivo intacto: o helper
    sobreviveu ao stop do scope transitório que o criou.
    """
    return ["systemd-run", "--user", "--no-block", "--quiet", "--collect",
            "--property=Type=oneshot", f"--unit={unit}",
            "/bin/sh", "-c", script]


def _cgroup_escape_leaf(pid: int) -> Path:
    """Folha delegada própria sob user@<uid>.service para o escape 2.

    Medido nesta máquina: escrever o PID no cgroup.procs de
    ``user.slice/user-<uid>.slice`` dá EACCES e no de ``user@<uid>.service``
    dá EBUSY (restrição de processos internos); o que funciona sem privilégio
    é criar uma folha própria ali embaixo e mover o PID para ela.
    """
    uid = os.getuid()
    return Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice"
                f"/user@{uid}.service/gino-relaunch-{int(pid)}"
                f"-{int(time.time())}")


def _move_pid_out_of_app_cgroup(pid: int) -> bool:
    """Escape 2: migração explícita do PID para a folha delegada."""
    leaf = _cgroup_escape_leaf(pid)
    try:
        leaf.mkdir(parents=True, exist_ok=False)
    except OSError:
        return False
    try:
        (leaf / "cgroup.procs").write_text(f"{int(pid)}\n", encoding="utf-8")
        return True
    except OSError:
        try:
            leaf.rmdir()
        except OSError:
            pass
        return False


class RestartManager:
    """Two-step restart requests. One instance per app, bound in main."""

    def __init__(self, dry_run: bool = False,
                 logger: Optional[Callable[[str], None]] = print,
                 state_path: Optional[Path] = None):
        self.dry_run = bool(dry_run)
        self._logger = logger or (lambda _msg: None)

        self.state_path = Path(state_path) if state_path is not None else STATE_PATH
        # Dry runs are the test path: they must not touch the real state file.
        # An explicit path is an isolated state file and is honoured anyway.
        self._persist = state_path is not None or not self.dry_run
        # Keep the rate limit next to the request file, never in a shared path.
        self.last_path = (self.state_path.with_name("restart_last.json")
                          if state_path is not None else LAST_PATH)
        self.archive_path = (self.state_path.with_name(
                                "restart_last_pending.json")
                             if state_path is not None else ARCHIVE_PATH)
        # Trilha do poder próprio: no mesmo lugar do resto do estado, e em
        # arquivo separado quando o chamador isolou o estado (testes).
        self.trail_path = (self.state_path.with_name("restart_trail.jsonl")
                           if state_path is not None else TRAIL_PATH)

        self._lock = threading.RLock()
        self._pending: Optional[_PendingRequest] = None
        self._expired: Optional[dict] = None
        self._last_assistant_at: Optional[float] = None
        self._last_reason: str = ""
        # Reinício do próprio processo adiado por guarda (limites 1 e 2).
        # Um único slot: um lote de trabalho nunca acumula mais de um.
        self._self_deferred: Optional[_PendingRequest] = None
        self._load_last()

        self._shutdown_cb: Optional[Callable[[], None]] = None
        self._confirm_cb: Optional[Callable[[str], None]] = None
        self._expire_cb: Optional[Callable[[str], None]] = None
        self._pc_restart: Optional[Callable[[], object]] = None
        self._delivering_cb: Optional[Callable[[], bool]] = None

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(self, shutdown_cb: Optional[Callable[[], None]] = None,
             confirm_cb: Optional[Callable[[str], None]] = None,
             pc_restart: Optional[Callable[[], object]] = None,
             expire_cb: Optional[Callable[[str], None]] = None,
             delivering_cb: Optional[Callable[[], bool]] = None) -> None:
        """Wire the manager to the app.

        shutdown_cb: called with no arguments to end the app cleanly.
        confirm_cb:  called with the sentence to speak, asking for the phrase
                     (optional — the model can do the asking itself).
        expire_cb:   called with the sentence announcing that a parked request
                     expired without execution. Must be short (it runs under
                     the manager lock) and must never call back into the
                     manager — the app wires it to its spoken-notice channel.
        pc_restart:  the callable that reboots the machine; defaults to
                     actions.computer_settings.restart_computer at execution
                     time, never at import time.
        delivering_cb: True while the current answer is still leaving (TTS
                     queue or the outgoing chat note). Guarda 2 do reinício do
                     próprio processo: o reinício espera a entrega acabar.
        """
        self._shutdown_cb = shutdown_cb if callable(shutdown_cb) else None
        self._confirm_cb = confirm_cb if callable(confirm_cb) else None
        self._expire_cb = expire_cb if callable(expire_cb) else None
        self._pc_restart = pc_restart if callable(pc_restart) else None
        self._delivering_cb = delivering_cb if callable(delivering_cb) else None

    def _log(self, message: str) -> None:
        try:
            self._logger(f"[Restart] {_short(message, 200)}")
        except Exception:
            pass

    # ── persisted state ─────────────────────────────────────────────────────

    def _load_file(self) -> Optional[_PendingRequest]:
        """Read the parked request from disk, if any (caller holds the lock)."""
        if not self._persist:
            return None
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "")
        if kind not in _PHRASES:
            return None
        try:
            created_at = float(raw.get("created_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if created_at <= 0:
            return None
        if created_at < _PROCESS_STARTED_WALL - _BOOT_GRACE_S:
            # Saved before this process existed: the restart it was waiting for
            # already happened. Keeping it would ask the owner to confirm the
            # same restart on every boot.
            self._log("pedido de reinício de um processo anterior descartado")
            self._clear_file()
            return None
        # The file stores wall-clock time; the in-memory logic uses monotonic.
        at = time.monotonic() - max(time.time() - created_at, 0.0)
        return _PendingRequest(kind=kind,
                               reason=_short(raw.get("reason") or ""),
                               phrase=_PHRASES[kind], at=at)

    def _save_file(self, p: _PendingRequest) -> None:
        """Atomically persist the request (caller holds the lock)."""
        if not self._persist:
            return
        payload = {
            "kind":       p.kind,
            "reason":     p.reason,
            "phrase":     p.phrase,
            "created_at": time.time() - max(time.monotonic() - p.at, 0.0),
        }
        self._write_json(self.state_path, payload)

    def _write_json(self, path: Path, payload: dict) -> None:
        """Atomic 0600 write; a failure is logged, never raised."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            self._log(f"could not persist {path.name} — {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _clear_file(self) -> None:
        """Remove the persisted request, if any (caller holds the lock)."""
        if not self._persist:
            return
        try:
            self.state_path.unlink(missing_ok=True)
        except OSError as e:
            self._log(f"could not clear pending request — {e}")

    def _load_last(self) -> None:
        """Restore the last-restart time so the rate limit survives a boot."""
        if not self._persist:
            return
        try:
            raw = json.loads(self.last_path.read_text(encoding="utf-8"))
            at_wall = float(raw.get("at") or 0.0)
            pid = int(raw.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return
        if at_wall <= 0 or pid == os.getpid():
            # Same process: the in-memory limit already covers it, and a test
            # that builds two managers must not inherit the first one's limit.
            return
        self._last_reason = _short(raw.get("reason") or "")
        age = max(time.time() - at_wall, 0.0)
        self._last_assistant_at = time.monotonic() - age

    def _save_last(self, reason: str = "") -> None:
        """Persist when the assistant last restarted (boot rate limit)."""
        if not self._persist:
            return
        self._last_reason = _short(reason)
        payload = {"at": time.time(), "pid": os.getpid(),
                   "reason": self._last_reason}
        try:
            self.last_path.parent.mkdir(parents=True, exist_ok=True)
            self.last_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            self._log(f"could not persist restart time — {e}")

    def _archive_expired(self, p: _PendingRequest) -> None:
        """Keep the dead request as last-known, so renewal keeps its reason."""
        if not self._persist:
            return
        age = time.monotonic() - p.at
        self._write_json(self.archive_path, {
            "kind":                p.kind,
            "reason":              p.reason,
            "phrase":              p.phrase,
            "created_at":          time.time() - max(age, 0.0),
            "expired_at":          time.time(),
            "expired_seconds_ago": int(max(age, 0.0)),
        })

    def _read_archive(self) -> Optional[dict]:
        """The last expired request on disk as plain data, or None (locked)."""
        if not self._persist:
            return None
        try:
            raw = json.loads(self.archive_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "")
        if kind not in _PHRASES:
            return None
        return {"kind": kind,
                "reason": _short(raw.get("reason") or ""),
                "phrase": _PHRASES[kind]}

    def _forget_archive(self) -> None:
        """Drop the expired record once a fresh request carries it (locked)."""
        if not self._persist:
            return
        try:
            self.archive_path.unlink(missing_ok=True)
        except OSError as e:
            self._log(f"could not clear expired archive — {e}")

    def _mark_expired(self, p: _PendingRequest) -> None:
        """Remember that a request expired, so status stops lying (locked).

        The expiry is NOT silent anymore (05/11/2026): a parked request that
        died without execution gets one spoken notice to the owner through
        ``expire_cb``. Before this the request parked at 11:44 expired at
        13:21 and the owner never heard a word about it.
        """
        age = time.monotonic() - p.at
        self._expired = {
            "kind":               p.kind,
            "reason":             p.reason,
            "phrase":             p.phrase,
            "expired_seconds_ago": int(max(age, 0.0)),
        }
        # Move, don't delete: the late-phrase path renews from this record.
        self._archive_expired(p)
        self._pending = None
        self._clear_file()
        if self._expire_cb is not None:
            try:
                # Short call under the lock; never re-enters the manager.
                self._expire_cb(self.expired_notice(p))
            except Exception as e:
                self._log(f"aviso de expiração falhou — {e}")

    def expired_notice(self, p: _PendingRequest) -> str:
        """Frase curta e falável avisando que o pedido estacionado expirou."""
        wall = time.time() - max(time.monotonic() - p.at, 0.0)
        hhmm = time.strftime("%H:%M", time.localtime(wall))
        rotulo = "do assistente" if p.kind == "assistant" else "do computador"
        return (f"O pedido de reinício {rotulo} das {hhmm} expirou sem "
                f"execução, criador.")

    def last_expired(self) -> Optional[dict]:
        """The most recent request that timed out, or None. Plain data."""
        with self._lock:
            return dict(self._expired) if self._expired else None

    def last_known(self) -> Optional[dict]:
        """The last request that died (expired in RAM, on disk, or before boot).

        Used by the late-confirmation path to know whether the owner was
        answering an assistant restart or a PC one before renewing it.
        """
        with self._lock:
            known = self._expired or self._read_archive()
            if known is None and self._last_reason:
                known = {"kind": "assistant", "reason": self._last_reason}
            return dict(known) if known else None

    def renew_expired(self, reason: str = "", kind: str = "") -> dict:
        """Recreate an expired request, keeping its kind and reason.

        The anomaly this exists for (02/11/2026): a request parked at 11:23 was
        confirmed at 11:51, the TTL had run out, ``pending()`` returned None and
        the phrase was read by the voice gate as a NEW silent request — the
        owner waited for a restart that never came. This only rebuilds the
        request through the normal rate-limited path; the caller must say out
        loud that the old one expired, and the phrase has to be repeated.

        ``kind`` overrides the last-known kind when the owner named one out
        loud ("...o assistente"), so a stale PC request is never renewed as a
        reboot the owner did not ask for.

        Returns plain data:
        ``{"renewed", "kind", "reason", "phrase", "detail"}``.
        """
        known = self.last_known()
        if known is None:
            return {"renewed": False, "kind": "", "reason": "", "phrase": "",
                    "detail": "no earlier request on record"}
        kind = str(kind or known.get("kind") or "assistant")
        if kind not in _PHRASES:
            kind = "assistant"
        kept = (_short(reason) or _short(known.get("reason") or "")
                or self._last_reason)
        out = self._request(kind, kept)
        p = self.pending()
        if p is None:
            return {"renewed": False, "kind": kind, "reason": kept,
                    "phrase": "", "detail": out}
        with self._lock:
            self._forget_archive()
        self._log("confirmacao tardia — pedido expirado, renovado")
        return {"renewed": True, "kind": p["kind"], "reason": p["reason"],
                "phrase": p["phrase"], "detail": out}

    # ── state ───────────────────────────────────────────────────────────────

    def pending(self) -> Optional[dict]:
        """The pending request as plain data, or None. Callables never leave."""
        with self._lock:
            p = self._pending
            if p is None:
                p = self._load_file()
                self._pending = p
            if p is None:
                return None
            age = time.monotonic() - p.at
            if age > CONFIRM_TIMEOUT_SECONDS:
                self._mark_expired(p)
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
            if current is None:
                current = self._load_file()
            if current is not None and now - current.at <= CONFIRM_TIMEOUT_SECONDS:
                self._pending = current
                return (f"A confirmation is already pending for the "
                        f"{current.kind} restart. The owner must speak the "
                        f"phrase, or it can be dropped with action='cancel'. "
                        f"Nothing new was done.")
            if current is not None:
                self._mark_expired(current)
            self._pending = None
            self._expired = None

            if kind == "assistant" and self._last_assistant_at is not None:
                elapsed = now - self._last_assistant_at
                if elapsed < RATE_LIMIT_SECONDS:
                    remaining = int(RATE_LIMIT_SECONDS - elapsed) + 1
                    return (f"Assistant restart is rate-limited to {_RATE_MESSAGE}; "
                            f"try again in {remaining} seconds. Nothing was done.")

            request = _PendingRequest(kind=kind, reason=reason,
                                      phrase=_PHRASES[kind], at=now)
            self._pending = request
            self._save_file(request)

        self._log(f"{kind} restart requested"
                  + (f" — reason: {reason}" if reason else ""))

        if self._confirm_cb is not None and not self.dry_run:
            try:
                self._confirm_cb(
                    f"Ask the user to say '{request.phrase}' out loud, or to "
                    f"type his password in the app or his own WhatsApp, to "
                    f"restart the {kind}.")
            except Exception as e:
                with self._lock:
                    if self._pending is request:
                        self._pending = None
                        self._clear_file()
                self._log(f"confirmation prompt failed — {e}")
                return (f"Could not ask for the spoken confirmation: {e}. "
                        f"Nothing was done.")

        return (
            f"[CONFIRMATION_PENDING] A spoken confirmation is needed to restart "
            f"the {kind}. Nothing has restarted. Tell the owner to say exactly "
            f"'{request.phrase}' out loud, or to type his password in the app "
            f"or his own WhatsApp. The confirmation is owner-factor-only (the "
            f"owner's voice or his verified typed password — decision of "
            f"06/11/2026): no tool call can confirm it for him. Until then, "
            f"do not claim it happened."
        )

    # ── poder próprio: reiniciar o PRÓPRIO processo ─────────────────────────

    def _trail(self, decision: str, requester: str, reason: str,
               guard: str = "") -> None:
        """Uma linha por decisão de reinício — nunca levanta o turno.

        ``decision``: executado | adiado | recusado | descartado. Quem pediu é
        um dos rótulos fixos; o motivo passa por redação. Sem segredo e sem
        número de contato, mesmo que o texto recebido tivesse um.
        """
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pid": os.getpid(),
            "alvo": "processo",
            "decisao": decision,
            "quem": requester or REQUESTER_UNKNOWN,
            "motivo": _redact_for_trail(reason),
            "guarda": _redact_for_trail(guard),
        }
        try:
            self._log(f"reinício do processo {decision} — quem: "
                      f"{record['quem']}"
                      + (f" — guarda: {record['guarda']}"
                         if record["guarda"] else ""))
            if not self._persist:
                return
            self.trail_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trail_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._trim_trail()
        except Exception as e:
            self._log(f"trilha de reinício falhou — {e}")

    def _trim_trail(self) -> None:
        """Mantém as últimas TRAIL_MAX_LINES linhas; falha é silenciosa."""
        try:
            lines = self.trail_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) <= TRAIL_MAX_LINES:
            return
        tmp = self.trail_path.with_suffix(".trim")
        try:
            tmp.write_text("\n".join(lines[-TRAIL_MAX_LINES:]) + "\n",
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.trail_path)
        except OSError as e:
            self._log(f"trilha não coube — {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _self_guard(self) -> str:
        """Motivo que impede o reinício AGORA; "" quando nada impede.

        Guarda 1 (executor/subagente vivo) é verificada no código, sem
        depender de quem chama. Guarda 2 (resposta ainda saindo) vem do app
        por callback, porque só o app sabe se a fala dele terminou de sair.
        """
        if _jobs_busy():
            return "tarefa de fundo em execução"
        cb = self._delivering_cb
        if cb is not None:
            try:
                if cb():
                    return "resposta ainda em entrega"
            except Exception as e:
                self._log(f"guarda de entrega indisponível — {e}")
        return ""

    def self_restart(self, reason: str = "",
                     requester: str = REQUESTER_SELF) -> str:
        """Reinicia o processo do assistente — poder próprio, sem frase.

        Não existe parâmetro de alvo aqui de propósito: este método só sabe
        reiniciar o processo e nunca chama ``_run_pc``. A máquina continua no
        protocol antigo (``request_pc`` + a frase exata do dono).

        Ordem dos chequeiros: teto (recusa), depois guardas (adia), depois
        executa. Adiar nunca derruba o pedido — ele sai em
        ``flush_self_restart`` quando a entrega/executor liberar.
        """
        who = normalize_requester(requester)
        why = recorded_origin(reason)
        ceiling = self._rate_limited()
        if ceiling:
            self._trail("recusado", who, why, guard="teto de reinício")
            return ceiling
        guard = self._self_guard()
        if guard:
            with self._lock:
                # Um reinício por lote: o slot já cheio não acumula o segundo.
                if self._self_deferred is None:
                    self._self_deferred = _PendingRequest(
                        kind="assistant", reason=why, phrase="",
                        at=time.monotonic(), requester=who)
            self._trail("adiado", who, why, guard=guard)
            return (f"Reinício do processo ADIADO: {guard}. Acontece sozinho "
                    f"quando isso terminar — nada foi reiniciado ainda.")
        return self._run_self(_PendingRequest(kind="assistant", reason=why,
                                              phrase="", at=time.monotonic(),
                                              requester=who))

    def _run_self(self, request: _PendingRequest) -> str:
        """Executa o reinício do processo e grava a trilha do que houve."""
        with self._lock:
            self._self_deferred = None
        out = self._run_assistant(request)
        low = str(out).lower()
        decided = ("recusado" if "nothing was done" in low
                   or "cannot restart" in low or "could not schedule" in low
                   else "executado")
        self._trail(decided, request.requester, request.reason)
        return out

    def flush_self_restart(self) -> str:
        """Ponto de efeito do reinício adiado: chame quando a resposta saiu.

        O app chama isto quando a entrega terminou e nenhum executor está
        vivo (a fila de saída esvaziou). Ainda assim o método confere as
        guardas de novo — se algo voltou a ocupá-las, o pedido continua
        adiado em vez de cortar o trabalho no meio.
        """
        with self._lock:
            request = self._self_deferred
            if request is None:
                return ""
            age = time.monotonic() - request.at
        if age > SELF_DEFER_TTL_SECONDS:
            with self._lock:
                if self._self_deferred is request:
                    self._self_deferred = None
            self._trail("descartado", request.requester, request.reason,
                        guard="adiamento venceu")
            return ("The deferred assistant restart aged out. Nothing was "
                    "restarted.")
        ceiling = self._rate_limited()
        if ceiling:
            with self._lock:
                if self._self_deferred is request:
                    self._self_deferred = None
            self._trail("recusado", request.requester, request.reason,
                        guard="teto de reinício")
            return ceiling
        guard = self._self_guard()
        if guard:
            return ""
        return self._run_self(request)

    def deferred_self_restart(self) -> Optional[dict]:
        """O reinício do processo que está aguardando a guarda liberar."""
        with self._lock:
            request = self._self_deferred
            if request is None:
                return None
            age = int(max(time.monotonic() - request.at, 0.0))
            return {"kind": request.kind, "reason": request.reason,
                    "requester": request.requester, "age_seconds": age,
                    "guard": self._self_guard()}

    def confirm_owner(self, kind: str = "assistant",
                      reason: str = "") -> str:
        """Run a restart the OWNER just ordered out loud.

        The owner's rule (02/11/2026 ~19h): when he himself asks for the
        restart, his order is the authorisation — he does not repeat
        "confirmo reiniciar". The identity proof belongs to the audio layer,
        which calls this only after the privileged voiceprint matched the
        owner; a model tool call never reaches this method
        (actions/restart_assistant.py keeps using request_*). The five-minute
        assistant rate limit still applies, and a request parked earlier is
        superseded — the owner's latest order wins.
        """
        kind = str(kind or "").strip().lower()
        if kind not in _PHRASES:
            return "Unknown restart kind. Nothing was done."
        with self._lock:
            previous = self._pending
            self._pending = None
            self._clear_file()
        reason = recorded_origin(
            reason, getattr(previous, "reason", "") or "")
        request = _PendingRequest(kind=kind, reason=reason,
                                  phrase=_PHRASES[kind], at=time.monotonic())
        self._log(f"{kind} restart requested — origem: {reason}")
        if kind == "assistant":
            return self._run_assistant(request)
        return self._run_pc(request)

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
            p = self._pending
            if p is None:
                p = self._load_file()
            self._pending = None
            if p is None:
                return ("There is no restart awaiting confirmation. "
                        "Nothing was done.")

            age = time.monotonic() - p.at
            if age > CONFIRM_TIMEOUT_SECONDS:
                self._mark_expired(p)
                self._log(f"{p.kind} confirmation expired")
                return ("The restart request expired before it was confirmed. "
                        "Nothing was done; ask for it again if still wanted.")
            self._clear_file()

        if CONFIRM_MARKER not in _normalize(phrase):
            # Keep the request alive: the user may simply have been misheard.
            with self._lock:
                if self._pending is None:
                    self._pending = p
                    self._save_file(p)
            return (f"Restart not confirmed: the owner must say the exact "
                    f"phrase '{p.phrase}' out loud or give his typed password "
                    f"(his two doors). Nothing was done.")

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

    def _spawn_relaunch_helper(self, script: str) -> Optional[str]:
        """Lança o helper FORA do cgroup deste processo; descreve o mecanismo.

        ``start_new_session`` muda sessão/grupo, não cgroup: nascido dentro do
        scope do app o helper é varrido junto quando o systemd o para
        (provado em 05/11/2026 — nenhum relançamento automático do dia
        sobreviveu). Ordem testada nesta máquina:
          1. ``systemd-run --user`` — unidade transitória oneshot, fora do
             scope; o ambiente do app vivo tem DISPLAY, XDG_RUNTIME_DIR e
             DBUS_SESSION_BUS_ADDRESS, então o bus do usuário alcança.
          2. setsid + migração explícita para folha delegada (2 funciona sem
             privilégio só na folha; ver ``_cgroup_escape_leaf``).
          3. Último recurso: o helper antigo dentro do scope do app — pode
             morrer junto, documentado no log; melhor que não agendar nada.
        Retorna a descrição do mecanismo usado, ou None se não lançou.
        """
        pid = os.getpid()
        if shutil.which("systemd-run"):
            unit = _helper_unit_name(pid)
            try:
                # rc != 0 cobre bus ausente, unit duplicada e binário quebrado.
                done = subprocess.run(
                    systemd_run_helper_argv(script, unit),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10)
                if done.returncode == 0:
                    return f"systemd-run --user {unit}"
                self._log("systemd-run --user falhou (rc="
                          f"{done.returncode}) — tentando fuga de cgroup")
            except Exception as e:
                self._log(f"systemd-run --user indisponível ({e}) — "
                          "tentando fuga de cgroup")
        # Fallback 2: nasce detached por sessão e é MOVIDO para fora do
        # cgroup do app antes que o scope seja encerrado.
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", script],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                start_new_session=True,
            )
        except Exception as e:
            self._log(f"could not spawn restart helper — {e}")
            return None
        if _move_pid_out_of_app_cgroup(proc.pid):
            return f"setsid + cgroup leaf (pid {proc.pid})"
        # Fallback 3: último recurso documentado — segue vivo no scope do app.
        self._log("aviso: helper ficou DENTRO do cgroup do app — pode "
                  "morrer junto com o scope (último recurso)")
        return f"in-scope helper (pid {proc.pid})"

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
                    f"/bin/sh -c {shlex.quote(script)} detached outside this "
                    f"cgroup (systemd-run --user; fallback setsid + leaf) and "
                    f"then end this app.")

        if self._shutdown_cb is None:
            self._log("assistant restart refused — no shutdown hook bound")
            return ("Cannot restart the assistant: the app has not bound a "
                    "shutdown hook, so it could not close cleanly. Nothing was done.")

        if not os.path.isfile(RUN_SCRIPT):
            self._log(f"assistant restart refused — {RUN_SCRIPT} not found")
            return (f"Cannot restart the assistant: {RUN_SCRIPT} was not found. "
                    f"Nothing was done.")

        mechanism = self._spawn_relaunch_helper(script)
        if mechanism is None:
            return ("Could not schedule the assistant restart. "
                    "Nothing was done.")

        with self._lock:
            self._last_assistant_at = time.monotonic()
        # The limit must survive the restart it is guarding: a resumed chat
        # that still "remembers" a pending restart cannot trigger another one
        # immediately on the new boot.
        self._save_last(p.reason)

        self._log(f"assistant restart scheduled via {mechanism}"
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
            had = self._pending is not None or self._load_file() is not None
            self._pending = None
            self._expired = None
            self._clear_file()
        if not had:
            return "There is no restart waiting to be cancelled. Nothing was done."
        self._log("restart cancelled")
        return "Restart cancelled. Nothing was done."

    # The Qoder tool bridge looks for these names (see core/tool_bridge.py).
    def assistant(self, reason: str = "") -> str:
        """Ponte/agente pedindo o reinício do próprio processo: é poder
        próprio (dono, 10/11/2026), não um pedido a confirmar. A origem
        registrada é a de sempre em qualquer chamada de ferramenta:
        iniciativa do assistente — uma chamada de ferramenta nunca prova que
        foi o dono quem falou."""
        return self.self_restart(reason=reason, requester=REQUESTER_SELF)

    def pc(self, reason: str = "") -> str:
        """A máquina continua no portão antigo: estaciona e espera a frase."""
        return self.request_pc(reason=reason)

    def status(self) -> Optional[dict]:
        return self.pending()
