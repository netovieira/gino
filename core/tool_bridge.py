"""
Local HTTP bridge between the persistent Qoder sidecar (MCP, Node) and JARVIS.

The sidecar never imports Python and never touches the Qt application. It talks
JSON over loopback to this module, which owns exactly three policy decisions:

  * authentication — every route except ``/health`` needs
    ``Authorization: Bearer <token>``; the token is injected by the app and is
    never written to disk;
  * risk tier — safe/low actions run immediately, medium/high actions ask the
    injected ``confirm(title, message) -> bool`` callback first;
  * execution — ``/call`` is forwarded to the application's own tool executor
    (``dispatch``) or, when none was injected, to ``ActionRegistry.run``. The
    call runs on a worker thread with a timeout so the FastAPI event loop is
    never blocked. The bridge deliberately does NOT decide how UI or audio work
    is threaded: that remains the app's job, which is why ``dispatch`` exists.

Audit records are append-only JSONL, one object per ``/call`` (and per explicit
``/confirm``). Argument keys matching api_key/token/password/secret/authorization
are redacted before they leave the process.

Nothing here imports main.py or Qt — it must stay importable in tests and by a
headless sidecar.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import tool_receipts as _receipts

# ── risk tiers ───────────────────────────────────────────────────────────────
#
# safe   — reads, status, search. No side effects worth asking about.
# low    — music, apps, files that can be reversed. Auto-run (undo is preferred
#          to a question for anything reversible).
# medium — something leaves the machine or changes shared state: sending a
#          message, uploading, purchases, controlling a phone/browser.
# high   — restart/shutdown, destructive file operations, a destructive shell
#          command, persona/secrets, and anything we do not recognise. A
#          confirmation (and, at the voice layer, the owner) is mandatory.
TIER_SAFE = "safe"
TIER_LOW = "low"
TIER_MEDIUM = "medium"
TIER_HIGH = "high"
TIERS = (TIER_SAFE, TIER_LOW, TIER_MEDIUM, TIER_HIGH)

# Every action/plugin name currently auto-discovered in actions/ and plugins/.
# Unknown names deliberately fall through to high instead of being trusted.
ACTION_TIERS: dict[str, str] = {
    # ── safe: reads / status / search ──────────────────────────────────────
    "web_search": TIER_SAFE,
    "weather_report": TIER_SAFE,
    "flight_finder": TIER_SAFE,
    "continuity": TIER_SAFE,
    "morning_briefing": TIER_SAFE,
    "brain_status": TIER_SAFE,
    "gemini_accounts_status": TIER_SAFE,
    "groq_listen_status": TIER_SAFE,
    "qoder_status": TIER_SAFE,
    "runtime_status": TIER_SAFE,
    "rastracking": TIER_SAFE,
    "system_monitor": TIER_SAFE,
    "background_monitor": TIER_SAFE,
    "screen_processor": TIER_SAFE,
    # ── low: music / apps / reversible files / opening a visible terminal ───
    "open_app": TIER_LOW,
    "music_control": TIER_LOW,
    "video_player": TIER_LOW,
    "youtube_video": TIER_LOW,
    "file_controller": TIER_LOW,
    "file_processor": TIER_LOW,
    "desktop_control": TIER_LOW,
    "organizer": TIER_LOW,
    "memory_index": TIER_LOW,
    "tasks": TIER_LOW,
    "task_mirror": TIER_LOW,
    "open_in": TIER_LOW,
    "media_control": TIER_LOW,
    "focus_mode": TIER_LOW,
    "humor_mode": TIER_LOW,
    "reminder": TIER_LOW,
    "game_updater": TIER_LOW,
    "computer_settings": TIER_LOW,
    "proactive": TIER_LOW,
    # Opening a terminal is an interface action, not execution: the owner
    # asked that it never require permission. terminal_run is also direct for
    # a plain command; risk_tier() escalates it when the command is destructive.
    "terminal_open": TIER_LOW,
    "terminal_run": TIER_LOW,
    # ── medium: sending / uploads / purchases / remote devices ─────────────
    "send_message": TIER_MEDIUM,
    "email_control": TIER_MEDIUM,
    "mail_control": TIER_MEDIUM,
    "whatsapp_control": TIER_MEDIUM,
    "linkedin_control": TIER_MEDIUM,
    "trello_control": TIER_MEDIUM,
    "cakto_sales": TIER_MEDIUM,
    "android_control": TIER_MEDIUM,
    "iphone_control": TIER_MEDIUM,
    "mac_control": TIER_MEDIUM,
    # Simple house actions (on/off, brightness, scene) are reversible and are
    # whitelisted in actions/home_control.auto_authorized(), so the bridge does
    # not ask for a spoken confirmation. The tier stays medium because it
    # changes shared state in the owner's home.
    "home_control": TIER_MEDIUM,
    "browser_control": TIER_MEDIUM,
    "computer_control": TIER_MEDIUM,
    "upload": TIER_MEDIUM,
    "upload_file": TIER_MEDIUM,
    "purchase": TIER_MEDIUM,
    "buy": TIER_MEDIUM,
    # ── high: reboot/shutdown, deletion, code execution, persona/secrets ───
    "root_exec": TIER_HIGH,
    "dev_agent": TIER_HIGH,
    "code_helper": TIER_HIGH,
    "qoder_agent": TIER_HIGH,
    "qoder_job": TIER_HIGH,
    "personas": TIER_HIGH,
    "phrase_blocklist": TIER_HIGH,
    # Parking a restart is not the restart: the owner's spoken phrase is the
    # only thing that executes it. The confirmation action is refused by the
    # tool itself, so the bridge does not need a second, unusable gate.
    "restart_assistant": TIER_LOW,
    "restart_pc": TIER_HIGH,
}

# Actions whose tier depends on what they were asked to do, not just on their
# name. A `computer_settings` volume change is harmless; a `computer_settings`
# restart is not. Same for deleting through file_controller.
_ESCALATING_ARGS: dict[str, frozenset[str]] = {
    "computer_settings": frozenset({"restart", "shutdown", "reboot", "poweroff"}),
    "file_controller": frozenset({"delete", "remove", "trash", "erase", "format",
                                  "wipe", "destroy", "rm", "rmdir"}),
    "desktop_control": frozenset({"clean", "purge"}),
    # Sending mail is irreversible. The tool itself refuses to send (it parks
    # the message until the owner's VOICE releases it); this tier makes the
    # bridge ask for its own confirmation on top, never instead.
    "mail_control": frozenset({"send", "enviar", "manda", "mandar"}),
}

_ARG_SCAN_KEYS = ("action", "operation", "op", "command", "mode", "task")

# ── terminal command risk ───────────────────────────────────────────────────
#
# The owner's rule: opening the terminal window must never ask permission.
# What still needs the gate is the *command* the model wants to type inside it.
# A plain command (ls, tail -f, npm test, git status) runs directly; anything
# here is treated as irreversible and keeps the mandatory confirmation. The
# list is deliberately conservative: a false positive costs one question, a
# false negative could wipe a disk.
_TERMINAL_DANGEROUS_RE = re.compile(
    r"""
    (?:
        # privilege / root
        (?<![\w-])(?:sudo|doas|pkexec)(?![\w-])
      | (?<![\w-])su(?![\w-])
        # deletion / disk writing
      | (?<![\w-])(?:rm|rmdir|unlink|shred|wipe|wipefs|mkfs(?:\.\w+)?|dd
                       |truncate|fdisk|parted|gdisk|sgdisk|mkswap|blkdiscard
                       |cryptsetup)(?![\w-])
      | (?<![\w-])find\b[^\n]*?(?:\s-delete\b|\s-exec(?:dir)?\s+(?:sudo\s+)?rm\b)
      | (?<![\w-])rsync\b[^\n]*?\s--delete(?:-\w+)?(?:\s|$)
      | (?<![\w-])chmod\s+-[a-zA-Z]*R
      | (?<![\w-])chown\s+-[a-zA-Z]*R
        # power / session
      | (?<![\w-])(?:shutdown|reboot|poweroff|halt)(?![\w-])
      | (?<![\w-])(?:systemctl|loginctl)\s+(?:poweroff|reboot|halt|suspend
                                              |hibernate|kill-session)
      | (?<![\w-])(?:init|telinit)\s+[06](?!\d)
        # process / package / container mass destruction
      | (?<![\w-])(?:killall|pkill)(?![\w-])
      | (?<![\w-])(?:apt|apt-get)\s+(?:remove|purge|autoremove|autopurge)(?![\w-])
      | (?<![\w-])(?:dnf|yum)\s+(?:remove|erase|autoremove)(?![\w-])
      | (?<![\w-])pacman\s+-[a-zA-Z]*R
      | (?<![\w-])zypper\s+(?:rm|remove)(?![\w-])
      | (?<![\w-])(?:snap|flatpak)\s+(?:remove|uninstall)(?![\w-])
      | (?<![\w-])(?:docker|podman)\s+(?:rm|rmi|prune)(?![\w-])
      | (?<![\w-])(?:docker|podman)\s+(?:system|volume|image|container)\s+
            (?:prune|rm)(?![\w-])
      | (?<![\w-])kubectl\s+delete(?![\w-])
      | (?<![\w-])helm\s+(?:uninstall|delete)(?![\w-])
        # database / irreversible VCS history
      | (?<![\w-])drop\s+(?:database|table|schema)(?![\w-])
      | (?<![\w-])truncate\s+table(?![\w-])
      | (?<![\w-])git\s+reset\s+--hard(?![\w-])
      | (?<![\w-])git\s+clean\b[^\n]*?\s-[a-zA-Z]*f
      | (?<![\w-])git\s+push\b[^\n]*?\s(?:--force|-f)(?![\w-])
        # remote code piped straight into a shell
      | (?<![\w-])(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh(?![\w-])
        # opaque inline code: the payload can do anything, so it needs the gate
      | (?<![\w-])(?:python[0-9.]*|node|perl|ruby|php|lua)\s+-[a-zA-Z]*[ce](?![\w-])
      | (?<![\w-])(?:ba|z|k)?sh\s+-[a-zA-Z]*c(?![\w-])
      | (?<![\w-])eval(?![\w-])
        # raw write to a block device
      | >\s*/dev/(?:sd|nvme|vd|hd|disk|mmcblk)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def terminal_command_is_dangerous(command: Any) -> bool:
    """True when a shell command would be irreversible / privileged / mass.

    Used only for the risk tier of ``terminal_run``. Opening the window
    (``terminal_open``) never reaches this check.
    """
    return bool(_TERMINAL_DANGEROUS_RE.search(str(command or "")))

_REDACT_RE = re.compile(r"(api[_-]?key|token|password|secret|authorization)", re.IGNORECASE)
_REDACTED = "[REDACTED]"

_DEFAULT_TIMEOUT = 120.0
_RESULT_SUMMARY_CHARS = 500

# ── pending (refused) actions ───────────────────────────────────────────────
#
# A medium/high action the owner did not authorise is a *pending* request. It
# used to live only in memory and only for ten minutes, so the 2026-10-31
# voice-note joke came back ~10 times over 30 h with a reworded caption each
# time. The ledger below is the fix; it is written to disk so a restart cannot
# resurrect it, and it is bounded on three sides:
#
#   * lifetime — 6 h from creation or from the last owner interaction (a spoken
#     consent). Model retries are not an owner interaction: they do not renew
#     it, otherwise a retry every hour would keep the request alive for ever.
#   * refusals — 3 refused attempts and the request is cancelled for good.
#   * identity — the key is a hash of the action plus its *stable* arguments.
#     A caption or a justification is cosmetic: re-wording it is not a new
#     request and does not reopen the prompt.
_PENDING_TTL_SECONDS = 6 * 3600.0
_PENDING_DENIAL_LIMIT = 3
# Dead marks (expired/cancelled) outlive the TTL so a restart cannot replay
# them; after a week the owner's intent is stale by any measure.
_PENDING_DEAD_RETENTION_SECONDS = 7 * 24 * 3600.0
_PENDING_MAX_ENTRIES = 200
# Not part of the payload identity. Deliberately short: ``caption`` is the
# optional media caption and ``reason`` is the human justification of an
# action (root_exec) — re-wording either does not change what would run.
# ``title`` (window to focus) and ``description`` (code_helper's request) are
# payload, so they stay in the key even though they read like labels.
_PENDING_COSMETIC_KEYS = frozenset({"caption", "reason"})
# For a file send the file is the payload and the text is only its caption.
_PENDING_FILE_KEYS = frozenset({"path", "file", "filename", "attachment", "media"})
_PENDING_FILE_CAPTION_KEYS = frozenset({"text", "message", "body", "msg",
                                        "message_text"})
_PENDING_ENV_TTL = "JARVIS_TOOL_BRIDGE_PENDING_TTL_SECONDS"
_PENDING_ENV_LIMIT = "JARVIS_TOOL_BRIDGE_PENDING_DENIAL_LIMIT"
_PENDING_ENV_PATH = "JARVIS_TOOL_BRIDGE_PENDING_PATH"


def redact_args(value: Any) -> Any:
    """Return a deep copy with secret-looking dict keys replaced by [REDACTED]."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _REDACT_RE.search(str(key)) else redact_args(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_args(item) for item in value]
    return value


def summarize(value: Any, limit: int = _RESULT_SUMMARY_CHARS) -> str:
    """A short, single-line-ish representation for logs and audit records."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _positive(value: Any, minimum: float) -> float:
    try:
        return max(float(minimum), float(value))
    except (TypeError, ValueError):
        return max(float(minimum), 1.0)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ── request bodies ───────────────────────────────────────────────────────────


class CallRequest(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)


class ConfirmRequest(BaseModel):
    reason: str
    title: Optional[str] = None


class MemoryReadRequest(BaseModel):
    path: str


class MemorySearchRequest(BaseModel):
    query: str
    limit: Optional[int] = None


class MemoryNoteRequest(BaseModel):
    text: str
    kind: Optional[str] = None


class VoiceEnrollRequest(BaseModel):
    name: str
    authorized: bool = False
    embedding: list[float] | None = None


class VoiceAuthorizeRequest(BaseModel):
    name: str


class RestartRequest(BaseModel):
    reason: str = ""


class LocalAskRequest(BaseModel):
    prompt: str


class TaskStartRequest(BaseModel):
    task: str
    mode: str = "answer"
    approval: bool | None = None


def _jsonable(value: Any) -> Any:
    try:
        return jsonable_encoder(value)
    except Exception:
        return str(value)


class ToolBridge:
    """FastAPI bridge + risk policy over the app's action/plugin executor.

    Parameters
    ----------
    registry:
        ``ActionRegistry`` (core/action_loader.py). Used as the fallback
        executor and for the ``/tools`` catalogue.
    dispatch:
        ``dispatch(name, args) -> str`` supplied by the application. This is the
        path that keeps UI/audio-thread safety in the app. When None, the bridge
        falls back to ``registry.run(name, args)``.
    token:
        Shared bearer token. Required, never persisted by this class.
    confirm:
        ``confirm(title, message) -> bool``. When None, medium/high calls are
        refused with HTTP 409 ``confirmation_required``.
    preauthorize:
        Optional ``preauthorize(name, args) -> bool``. Checked before the
        confirmation gate for medium/high calls; True means a separate, narrow
        grant already authorises this exact call (the autonomous WhatsApp
        reply in core/actiongate.py, or — through the application's hook — an
        active per-person grant in core/voice_grants.py, whose default answer
        is DENIAL). Exceptions count as False, so a broken
        hook never approves anything.
    authorized:
        Optional ``authorized() -> bool`` telling whether a spoken consent
        window is currently open. A refused call is parked (and persisted) so a
        model that retries it gets a refusal instead of a fresh prompt; only a
        fresh spoken consent lets the identical payload reach the confirmation
        gate again. Exceptions count as False. When omitted, a refused call is
        still recorded and cancelled after N refusals, but the bridge cannot
        tell a retry from a new owner request, so it re-prompts instead of
        suppressing until the retry is cancelled.
    pending_path:
        JSON file holding the refused/pending actions. ``None`` (default)
        derives ``tool_bridge_pending.json`` next to ``audit_path``; with no
        audit path the ledger is in-memory only, which keeps tests hermetic.
        No message body is ever stored, only the action, a redacted summary and
        a payload digest.
    pending_ttl / pending_denial_limit:
        Override ``_PENDING_TTL_SECONDS`` (6 h) and ``_PENDING_DENIAL_LIMIT``
        (3). When omitted, the env vars
        ``JARVIS_TOOL_BRIDGE_PENDING_TTL_SECONDS`` and
        ``JARVIS_TOOL_BRIDGE_PENDING_DENIAL_LIMIT`` are read, then the module
        defaults.
    clock:
        Wall-clock source (``time.time`` by default). Injected by tests so the
        TTL can be crossed without sleeping; the ledger must use wall clock,
        not ``time.monotonic()``, because it outlives the process.
    memory / voice / restart / local_ask:
        Optional delegated components. Each may be an object exposing methods
        or a dict of callables. When absent the endpoint answers HTTP 501.
    audit_path:
        Optional JSONL file. One record per /call and /confirm.
    """

    def __init__(self, registry, dispatch, token: str, host: str = "127.0.0.1",
                 port: int = 8011, confirm=None, logger: Callable[[str], None] = print,
                 audit_path=None, memory=None, voice=None, restart=None,
                 local_ask=None, start_task=None,
                 timeout: float = _DEFAULT_TIMEOUT, preauthorize=None,
                 authorized=None, pending_path=None, pending_ttl: Optional[float] = None,
                 pending_denial_limit: Optional[int] = None,
                 clock: Optional[Callable[[], float]] = None):
        if not isinstance(token, str) or not token:
            raise ValueError("ToolBridge requires a non-empty bearer token")
        self._registry = registry
        self._dispatch = dispatch
        self._token = token
        self.host = host
        self._requested_port = int(port)
        self._port = int(port)
        self._confirm = confirm
        self._preauthorize = preauthorize
        # True while a spoken consent window is open. Used only to lift the
        # pending marks: a fresh "pode" authorises the same action again.
        self._authorized = authorized
        self._log_fn = logger or (lambda _msg: None)
        self._audit_path = Path(audit_path) if audit_path else None
        self._memory = memory
        self._voice = voice
        self._restart = restart
        self._local_ask = local_ask
        self._start_task = start_task
        self._timeout = float(timeout)

        # ── pending ledger ────────────────────────────────────────────────
        self._audit_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._clock = clock or time.time
        self._pending_ttl = _positive(
            pending_ttl if pending_ttl is not None
            else _env_float(_PENDING_ENV_TTL, _PENDING_TTL_SECONDS), 60.0)
        self._pending_denial_limit = int(_positive(
            pending_denial_limit if pending_denial_limit is not None
            else _env_float(_PENDING_ENV_LIMIT, float(_PENDING_DENIAL_LIMIT)), 1.0))
        self._pending_path = self._resolve_pending_path(pending_path)
        self._denials: dict[str, dict[str, Any]] = {}
        self._denials_lock = threading.RLock()
        self._load_pending()

        # ── tool receipts (HMAC) ──────────────────────────────────────────
        self._receipts_key: Optional[bytes] = None
        try:
            self._receipts_key = _receipts.load_key()
        except Exception:
            self._receipts_key = None

        self._server = None
        self._thread: Optional[threading.Thread] = None

        self.app = self._build_app()

    # ── policy ─────────────────────────────────────────────────────────────

    def risk_tier(self, name: str, args: Optional[dict] = None) -> str:
        """Resolve the tier for an action name, escalating on dangerous args."""
        action_name = str(name or "")
        if action_name == "personas" and isinstance(args, dict):
            # Reading personas/projects/models is safe; changing the board is
            # high-risk and needs the owner's spoken confirmation.
            action = str(args.get("action") or "").strip().lower()
            if action in ("list", "show", "models", "for_project"):
                return TIER_SAFE
            return TIER_HIGH
        if action_name == "terminal_run" and isinstance(args, dict):
            # Opening the window is direct; the command typed inside it is
            # classified on its own. A destructive one still needs the gate.
            if terminal_command_is_dangerous(args.get("command")):
                return TIER_HIGH
            return TIER_LOW
        tier = ACTION_TIERS.get(action_name, TIER_HIGH)
        tokens = _ESCALATING_ARGS.get(action_name)
        if tokens and isinstance(args, dict):
            for key in _ARG_SCAN_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip().lower() in tokens:
                    return TIER_HIGH
        return tier

    # ── app construction ───────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="JARVIS Tool Bridge", docs_url=None, redoc_url=None,
                      openapi_url=None)

        async def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
            if not authorization:
                raise HTTPException(status_code=401, detail="Missing bearer token",
                                    headers={"WWW-Authenticate": "Bearer"})
            scheme, _, presented = authorization.partition(" ")
            if scheme.lower() != "bearer" or not presented:
                raise HTTPException(status_code=401, detail="Expected Authorization: Bearer <token>",
                                    headers={"WWW-Authenticate": "Bearer"})
            # Constant-time even on length mismatch.
            if not hmac.compare_digest(presented.encode("utf-8"),
                                       self._token.encode("utf-8")):
                raise HTTPException(status_code=401, detail="Invalid token",
                                    headers={"WWW-Authenticate": "Bearer"})

        auth = [Depends(require_auth)]

        @app.get("/health")
        async def health():
            return {
                "status": "ok",
                "service": "jarvis-tool-bridge",
                "running": self.is_running(),
                "port": self._port,
            }

        @app.get("/tools", dependencies=auth)
        async def tools():
            return {"tools": self._tool_catalog()}

        @app.post("/call", dependencies=auth)
        async def call(payload: CallRequest):
            return await self._handle_call(payload.name, payload.args)

        @app.post("/confirm", dependencies=auth)
        async def confirm(payload: ConfirmRequest):
            if self._confirm is None:
                return self._not_configured(
                    "confirmation", "No confirmation channel is bound to the bridge.")
            title = (payload.title or "Confirmation").strip() or "Confirmation"
            approved = await self._run_confirm(title, payload.reason)
            if isinstance(approved, JSONResponse):
                return approved
            self._audit(action="confirm", args={"reason": payload.reason,
                                                "title": title},
                        tier=TIER_HIGH, approved=approved,
                        result="approved" if approved else "denied",
                        status="ok" if approved else "denied")
            return {"status": "ok", "approved": approved}

        @app.post("/memory/read", dependencies=auth)
        async def memory_read(payload: MemoryReadRequest):
            fn = self._find_method(self._memory, ("read", "memory_read"))
            if fn is None:
                return self._not_configured("memory", "Memory bridge is not configured.")
            return await self._delegate(fn, path=payload.path)

        @app.post("/memory/search", dependencies=auth)
        async def memory_search(payload: MemorySearchRequest):
            fn = self._find_method(self._memory, ("search", "memory_search"))
            if fn is None:
                return self._not_configured("memory", "Memory bridge is not configured.")
            kwargs = {"query": payload.query}
            if payload.limit is not None:
                kwargs["limit"] = payload.limit
            return await self._delegate(fn, **kwargs)

        @app.post("/memory/note", dependencies=auth)
        async def memory_note(payload: MemoryNoteRequest):
            fn = self._find_method(self._memory, ("note", "memory_note", "append"))
            if fn is None:
                return self._not_configured("memory", "Memory bridge is not configured.")
            kwargs = {"text": payload.text}
            if payload.kind is not None:
                kwargs["kind"] = payload.kind
            return await self._delegate(fn, **kwargs)

        @app.post("/voice/list", dependencies=auth)
        async def voice_list():
            fn = self._find_method(self._voice, ("list", "list_voices"))
            if fn is None:
                return self._not_configured("voice", "Voice bridge is not configured.")
            return await self._delegate(fn)

        @app.post("/voice/enroll", dependencies=auth)
        async def voice_enroll(payload: VoiceEnrollRequest):
            fn = self._find_method(self._voice, ("enroll", "enroll_voice"))
            if fn is None:
                return self._not_configured("voice", "Voice bridge is not configured.")
            # Audio never travels through the bridge: the sample is captured by
            # the app's microphone. Without one, say so instead of a 500.
            if not payload.embedding:
                raise HTTPException(
                    status_code=400,
                    detail=("voice enrollment needs a voice sample from the "
                            "microphone; ask the owner to say 'registra essa voz' "
                            "in the app instead of calling this endpoint"))
            return await self._delegate(fn, name=payload.name,
                                        pcm16_or_embeddings=payload.embedding,
                                        authorized=payload.authorized)

        @app.post("/voice/authorize", dependencies=auth)
        async def voice_authorize(payload: VoiceAuthorizeRequest):
            fn = self._find_method(self._voice, ("authorize", "authorize_voice"))
            if fn is None:
                return self._not_configured("voice", "Voice bridge is not configured.")
            return await self._delegate(fn, name=payload.name)

        @app.post("/restart/assistant", dependencies=auth)
        async def restart_assistant(payload: RestartRequest):
            fn = self._find_method(self._restart, ("assistant", "restart_assistant", "restart"))
            if fn is None:
                return self._not_configured("restart", "Restart bridge is not configured.")
            return await self._delegate(fn, reason=payload.reason)

        @app.post("/restart/pc", dependencies=auth)
        async def restart_pc(payload: RestartRequest):
            fn = self._find_method(self._restart, ("pc", "restart_pc", "computer"))
            if fn is None:
                return self._not_configured("restart", "Restart bridge is not configured.")
            return await self._delegate(fn, reason=payload.reason)

        @app.post("/local_ask", dependencies=auth)
        async def local_ask(payload: LocalAskRequest):
            if self._local_ask is None:
                return self._not_configured("local_ask", "Local model bridge is not configured.")
            fn = self._local_ask
            if not callable(fn):
                fn = self._find_method(fn, ("ask", "local_ask", "complete"))
            if not callable(fn):
                return self._not_configured("local_ask", "Local model bridge is not configured.")
            return await self._delegate(fn, prompt=payload.prompt)

        @app.post("/task/start", dependencies=auth)
        async def task_start(payload: TaskStartRequest):
            if self._start_task is None:
                return self._not_configured("task", "Background task bridge is not configured.")
            fn = self._start_task
            if not callable(fn):
                fn = self._find_method(fn, ("start", "start_task", "task"))
            if not callable(fn):
                return self._not_configured("task", "Background task bridge is not configured.")
            return await self._delegate(fn, task=payload.task, mode=payload.mode,
                                        approval=payload.approval)

        return app

    def _tool_catalog(self) -> list[dict]:
        get_decls = getattr(self._registry, "get_tool_declarations", None)
        declarations = get_decls() if callable(get_decls) else []
        out = []
        for decl in declarations or []:
            item = dict(decl)
            item["tier"] = self.risk_tier(item.get("name", ""))
            out.append(item)
        return out

    # ── /call ──────────────────────────────────────────────────────────────

    async def _handle_call(self, name: str, args: dict):
        args = args if isinstance(args, dict) else {}
        tier = self.risk_tier(name, args)

        approved = True
        if tier in (TIER_MEDIUM, TIER_HIGH):
            if not self._preauthorized(name, args):
                blocked = self._pending_gate(name, args, tier)
                if blocked is not None:
                    return blocked
                if self._confirm is None:
                    message = (
                        f"Action '{name}' needs confirmation (risk: {tier}), but no "
                        f"confirmation channel is bound to the bridge."
                    )
                    self._audit_action(name, args, tier, approved=False,
                                       result=message, status="confirmation_required")
                    return JSONResponse(
                        status_code=409,
                        content={
                            "status": "confirmation_required",
                            "action": name,
                            "tier": tier,
                            "message": message,
                        },
                    )

                title = f"Confirm '{name}'"
                message = f"Risk: {tier}. Arguments: {summarize(redact_args(args), 300)}"
                approved = await self._run_confirm(title, message)
                if isinstance(approved, JSONResponse):
                    confirm_status = ("timeout" if approved.status_code == 504 else "error")
                    # A timeout is an unanswered question: it counts against the
                    # pending request exactly like a spoken "no", so the third
                    # silent refusal kills it instead of asking for ever. A
                    # broken confirmation channel is not the owner refusing.
                    cancelled = (self._remember_denial(name, args, tier, audit=False)
                                 if approved.status_code == 504 else None)
                    self._audit_action(name, args, tier, approved=False,
                                       result=approved.body.decode("utf-8", "replace")
                                       if isinstance(approved.body, bytes) else str(approved.body),
                                       status=confirm_status)
                    return cancelled if cancelled is not None else approved
                if not approved:
                    cancelled = self._remember_denial(name, args, tier)
                    if cancelled is not None:
                        return cancelled
                    message = (f"Action '{name}' was not authorized by the owner's "
                               f"voice yet. Ask the owner to say 'pode' or 'pode "
                               f"enviar' out loud; there is no popup to click.")
                    self._audit_action(name, args, tier, approved=False,
                                       result=message, status="denied")
                    return JSONResponse(
                        status_code=403,
                        content={"status": "denied", "action": name,
                                 "tier": tier, "message": message},
                    )
                self._forget_denial(name, args)

        executor = self._executor()
        if executor is None:
            message = "No tool executor is configured on the bridge."
            self._audit_action(name, args, tier, approved=approved,
                               result=message, status="unavailable")
            return JSONResponse(status_code=503,
                                content={"status": "unavailable",
                                         "error": message})

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._invoke, executor, name, args),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            message = (f"Action '{name}' timed out after "
                       f"{self._timeout:g}s. It may still be running in the app.")
            self._log(f"[ToolBridge] {message}")
            self._audit_action(name, args, tier, approved=approved,
                               result=message, status="timeout")
            return JSONResponse(status_code=504,
                                content={"status": "timeout", "action": name,
                                         "tier": tier, "error": message})
        except Exception as exc:  # dispatch/registry raised
            message = f"Action '{name}' failed: {exc}"
            self._log(f"[ToolBridge] {message}")
            self._audit_action(name, args, tier, approved=approved,
                               result=message, status="error")
            return JSONResponse(status_code=500,
                                content={"status": "error", "action": name,
                                         "tier": tier, "error": str(exc)})

        text = result if isinstance(result, str) else str(result)
        self._audit_action(name, args, tier, approved=approved, result=text, status="ok")
        self._issue_receipt(name, args, result)
        return {"status": "ok", "action": name, "tier": tier,
                "approved": True, "result": text}

    def _preauthorized(self, name: str, args: dict) -> bool:
        """True when a separate, narrow grant already authorises this exact call."""
        if self._preauthorize is None:
            return False
        try:
            granted = bool(self._preauthorize(name, args))
        except Exception as exc:
            self._log(f"[ToolBridge] preauthorization for '{name}' failed: {exc}")
            return False
        if granted:
            self._log(f"[ToolBridge] '{name}' authorized by a narrow grant")
        return granted

    # ── pending (refused) actions ──────────────────────────────────────────
    #
    # The bridge's confirmation channel is the owner's voice. When it refuses
    # an action, a model retrying the same call would ask the owner again (and
    # again, with a new caption each time) even hours later. The ledger below
    # remembers the request by *payload* — the action plus its stable arguments,
    # never the message body — and answers retries with a plain refusal.
    #
    # It is persisted next to the audit log, so a restart cannot resurrect an
    # action the owner already refused. Entries die three ways: a spoken
    # consent reopens the request (a fresh owner request), the TTL expires, or
    # the refusal count reaches the limit. Both deaths are written to the store
    # and logged as ``SYS: ação pendente ...`` so the owner can see why a
    # request stopped.

    _DENIAL_TARGET_KEYS = ("to", "contact", "recipient", "chat", "account",
                           "path", "target")

    @classmethod
    def _pending_payload(cls, name: str, args: dict) -> dict:
        """The stable, non-cosmetic part of a call — the payload identity.

        File sends are identified by the file, not by the caption: re-wording
        ``caption``/``text`` around the same ``path`` is the same request.
        """
        args = args if isinstance(args, dict) else {}
        payload = {key: value for key, value in args.items()
                   if key not in _PENDING_COSMETIC_KEYS}
        if any(key in args for key in _PENDING_FILE_KEYS):
            payload = {key: value for key, value in payload.items()
                       if key not in _PENDING_FILE_CAPTION_KEYS}
        return {"action": str(name or "").strip().lower(), "args": payload}

    @classmethod
    def _denial_key(cls, name: str, args: dict) -> str:
        payload = cls._pending_payload(name, args)
        try:
            body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                              default=str)
        except Exception:
            body = str(payload)
        digest = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]
        target = ""
        args = args if isinstance(args, dict) else {}
        for field in cls._DENIAL_TARGET_KEYS:
            value = args.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                target = str(value).strip().lower()[:40]
                break
        return f"{payload['action']}|{target}|{digest}"

    @classmethod
    def _pending_summary(cls, name: str, args: dict) -> str:
        """A log-safe identity: no body, no secret, just action/verb/target."""
        args = args if isinstance(args, dict) else {}
        parts = [str(name or "").strip() or "?"]
        verb = ""
        for field in ("action", "operation", "op"):
            value = args.get(field)
            if isinstance(value, str) and value.strip():
                verb = value.strip()[:24]
                break
        if verb:
            parts.append(verb)
        for field in cls._DENIAL_TARGET_KEYS:
            value = args.get(field)
            if isinstance(value, (str, int)) and str(value).strip():
                parts.append(f"→ {str(value).strip()[:40]}")
                break
        digest = cls._denial_key(name, args).rsplit("|", 1)[-1][:8]
        return f"{' '.join(parts)} [{digest}]"

    def _resolve_pending_path(self, pending_path) -> Optional[Path]:
        if pending_path is None:
            pending_path = os.environ.get(_PENDING_ENV_PATH) or None
        if pending_path in (None, False, ""):
            if self._audit_path is None:
                return None     # nothing to live next to: in-memory (tests)
            return self._audit_path.parent / "tool_bridge_pending.json"
        return Path(pending_path)

    # ── ledger persistence ─────────────────────────────────────────────────

    def _load_pending(self) -> None:
        path = self._pending_path
        if path is None:
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except Exception as exc:
            self._log(f"[ToolBridge] could not read pending actions: {exc}")
            return
        try:
            data = json.loads(raw)
        except Exception as exc:
            self._log(f"[ToolBridge] pending actions file is corrupt: {exc}")
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return
        now = self._clock()
        with self._denials_lock:
            for key, entry in entries.items():
                if not isinstance(entry, dict) or not isinstance(key, str):
                    continue
                entry.setdefault("status", "denied")
                entry.setdefault("attempts", 0)
                entry.setdefault("denials", 0)
                entry.setdefault("summary", "?")
                entry.setdefault("action", "?")
                entry.setdefault("tier", TIER_HIGH)
                touched = _safe_float(entry.get("touched"), now)
                entry["touched"] = touched
                if entry.get("status") == "denied":
                    entry["until"] = _safe_float(entry.get("until"),
                                                 touched + self._pending_ttl)
                self._denials[key] = entry
        # A restart must apply the same TTL: anything stale becomes dead here,
        # in the store, before a single call can replay it.
        self._sweep_pending(now)

    def _save_pending_locked(self) -> None:
        path = self._pending_path
        if path is None:
            return
        record = {"version": 1, "entries": self._denials}
        temp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                       default=str), encoding="utf-8")
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, path)
        except Exception as exc:      # the ledger must never break a call
            self._log(f"[ToolBridge] could not write pending actions: {exc}")

    def _sweep_pending(self, now: Optional[float] = None) -> None:
        """Expire stale requests and drop dead marks older than the retention."""
        now = self._clock() if now is None else now
        expired: list[dict] = []
        changed = False
        with self._denials_lock:
            for key, entry in list(self._denials.items()):
                status = entry.get("status")
                until = _safe_float(entry.get("until"), 0.0)
                if status == "denied" and now >= until:
                    entry["status"] = "expired"
                    changed = True
                    expired.append(dict(entry))
                elif (status in ("expired", "cancelled")
                      and now - until > _PENDING_DEAD_RETENTION_SECONDS):
                    self._denials.pop(key, None)
                    changed = True
            if len(self._denials) > _PENDING_MAX_ENTRIES:
                ordered = sorted(self._denials.items(),
                                 key=lambda item: _safe_float(item[1].get("touched"), 0.0))
                for key, _entry in ordered[: len(self._denials) - _PENDING_MAX_ENTRIES]:
                    self._denials.pop(key, None)
                    changed = True
            if changed:
                self._save_pending_locked()
        for entry in expired:
            message = f"SYS: ação pendente expirada: {entry.get('summary') or '?'}"
            self._log(message)
            self._audit(action=str(entry.get("action") or "?"), args={},
                        tier=str(entry.get("tier") or ""), approved=False,
                        result=message, status="expired")

    # ── ledger decisions ──────────────────────────────────────────────────

    def _fresh_consent(self) -> bool:
        if self._authorized is None:
            return False
        try:
            return bool(self._authorized())
        except Exception as exc:
            self._log(f"[ToolBridge] authorization check failed: {exc}")
            return False

    def _pending_cancel_response(self, name: str, tier: str, entry: dict,
                                 status: str, audit: bool = True) -> JSONResponse:
        limit = self._pending_denial_limit
        if status == "expired":
            hours = self._pending_ttl / 3600.0
            lifetime = f"{hours:g} h" if hours >= 1 else f"{self._pending_ttl / 60.0:g} min"
            message = (f"Action '{name}' was parked and the request expired "
                       f"after {lifetime} without the owner's authorisation. "
                       f"It will not be sent; ask the owner for a fresh "
                       f"request.")
            status_tag = "pending_expired"
        else:
            message = (f"Action '{name}' was cancelled after {limit} refusals. "
                       f"It will not be retried; ask the owner for a fresh "
                       f"request.")
            status_tag = "pending_cancelled"
        if audit:
            self._audit_action(name, {}, tier, approved=False, result=message,
                               status=status_tag)
        return JSONResponse(
            status_code=403,
            content={
                "status": "denied",
                "action": name,
                "tier": tier,
                "repeated": True,
                "awaiting_voice": False,
                "pending": status,
                "cancelled": status == "cancelled",
                "expired": status == "expired",
                "attempts": int(entry.get("attempts") or 0),
                "message": message,
            },
        )

    def _pending_gate(self, name: str, args: dict, tier: str):
        """Refuse a call whose parked request is dead or already refused.

        Returns a JSONResponse when the call must not reach the confirmation
        gate, else None. A fresh spoken consent reopens the request (that is
        the owner asking again); nothing else does.
        """
        self._sweep_pending()
        key = self._denial_key(name, args)
        with self._denials_lock:
            entry = self._denials.get(key)
            entry = dict(entry) if entry else None
        if entry is None:
            return None
        if self._fresh_consent():
            with self._denials_lock:
                self._denials.pop(key, None)
                self._save_pending_locked()
            self._log(f"[ToolBridge] pending request reopened by the owner's "
                      f"consent: {entry.get('summary')}")
            return None
        status = str(entry.get("status") or "denied")
        if status in ("expired", "cancelled"):
            return self._pending_cancel_response(name, tier, entry, status)
        if self._authorized is None:
            # No consent probe: a retry cannot be told apart from a new owner
            # request, so keep the old behaviour — ask again. Denials still
            # accumulate and cancel the request at the limit.
            return None
        # Already refused: do not reopen the prompt, do not reword the payload.
        cancelled = self._bump_pending_attempt(key, name, tier, entry)
        if cancelled is not None:
            return cancelled
        # Only while the request is still pending: a cancelled one must not be
        # invited back by the owner's answer to a question already withdrawn.
        self._rearm_voice_pending(entry)
        attempts = int(entry.get("attempts") or 0) + 1
        message = (
            f"Action '{name}' is still awaiting the owner's spoken "
            f"authorisation (repeat {attempts} suppressed). The "
            f"request was not reopened; do not ask again or retry "
            f"— wait for the owner to say 'pode'.")
        self._audit_action(name, args, tier, approved=False,
                           result=message, status="denied_repeat")
        return JSONResponse(
            status_code=403,
            content={
                "status": "denied",
                "action": name,
                "tier": tier,
                "repeated": True,
                "awaiting_voice": True,
                "attempts": attempts,
                "message": message,
            },
        )

    def _rearm_voice_pending(self, entry: dict) -> None:
        """Keep the owner's spoken answer possible, without re-asking the model.

        The bridge no longer re-prompts on a retry, but the question the owner
        never answered is still the same one: core/actiongate forgets a parked
        request after five minutes, so re-park it when it lapsed. Only when
        this bridge is wired to that module's own ``authorized`` probe; a test
        double must not touch module state. An answer window is never extended
        for ever — it is re-opened only after the previous one lapsed.
        """
        if self._authorized is None:
            return
        try:
            from core import actiongate
        except Exception:       # bridge must stay importable without the app
            return
        if self._authorized is not actiongate.authorized:
            return
        try:
            if not actiongate.has_pending():
                actiongate.mark_pending(
                    f"Confirmar '{entry.get('action') or '?'}'",
                    str(entry.get("summary") or ""))
        except Exception as exc:
            self._log(f"[ToolBridge] could not re-arm the voice pending: {exc}")

    def _bump_pending_attempt(self, key: str, name: str, tier: str,
                              entry: dict):
        """Count a refused attempt; cancel the request at the limit."""
        now = self._clock()
        with self._denials_lock:
            live = self._denials.get(key)
            if live is None:
                return None
            live["attempts"] = int(live.get("attempts") or 0) + 1
            live["last_attempt"] = now
            cancelled = int(live["attempts"]) >= self._pending_denial_limit
            if cancelled:
                live["status"] = "cancelled"
                live["until"] = now
            self._save_pending_locked()
            snapshot = dict(live)
        if not cancelled:
            return None
        message = (f"SYS: ação pendente cancelada por negativas "
                   f"({self._pending_denial_limit}x): "
                   f"{snapshot.get('summary') or '?'}")
        self._log(message)
        return self._pending_cancel_response(name, tier, snapshot, "cancelled")

    def _remember_denial(self, name: str, args: dict, tier: str,
                         audit: bool = True):
        """Park a request the owner refused (or did not answer in time).

        Returns a JSONResponse when this refusal cancelled the request, else
        None. The entry is created once and never renewed by model retries:
        ``touched`` only moves when the owner consents again.
        """
        now = self._clock()
        key = self._denial_key(name, args)
        with self._denials_lock:
            entry = self._denials.get(key)
            if entry is None:
                entry = {
                    "action": name,
                    "tier": tier,
                    "summary": self._pending_summary(name, args),
                    "created": now,
                    "touched": now,
                    "until": now + self._pending_ttl,
                    "attempts": 0,
                    "denials": 0,
                    "status": "denied",
                }
                self._denials[key] = entry
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["denials"] = int(entry.get("denials") or 0) + 1
            entry["last_attempt"] = now
            cancelled = int(entry["attempts"]) >= self._pending_denial_limit
            if cancelled:
                entry["status"] = "cancelled"
                entry["until"] = now
            self._save_pending_locked()
            snapshot = dict(entry)
        if not cancelled:
            return None
        message = (f"SYS: ação pendente cancelada por negativas "
                   f"({self._pending_denial_limit}x): "
                   f"{snapshot.get('summary') or '?'}")
        self._log(message)
        return self._pending_cancel_response(name, tier, snapshot, "cancelled",
                                             audit=audit)

    def _forget_denial(self, name: str, args: dict) -> None:
        if not self._denials:
            return
        key = self._denial_key(name, args)
        with self._denials_lock:
            if self._denials.pop(key, None) is not None:
                self._save_pending_locked()

    def forget_pending(self, name: Optional[str] = None,
                       args: Optional[dict] = None) -> None:
        """Drop pending marks.

        The bridge reopens a request by itself when the owner's voice consents
        while the retry arrives, which is the only in-band "new request"
        signal. This hook exists for an app that has a better one (an explicit
        owner command) and for tests; ``args`` alone selects one payload.
        """
        with self._denials_lock:
            if name is None:
                self._denials.clear()
            elif args is None:
                for key in [k for k, entry in self._denials.items()
                            if entry.get("action") == name]:
                    self._denials.pop(key, None)
            else:
                self._denials.pop(self._denial_key(name, args), None)
            self._save_pending_locked()

    def pending_actions(self) -> list[dict]:
        """A copy of the ledger, for diagnostics and the dashboard."""
        self._sweep_pending()
        with self._denials_lock:
            return [dict(entry) for entry in self._denials.values()]

    def _executor(self) -> Optional[Callable[..., Any]]:
        if self._dispatch is not None:
            return self._dispatch
        if self._registry is not None:
            run = getattr(self._registry, "run", None)
            if callable(run):
                return run
        return None

    @staticmethod
    def _invoke(executor: Callable[..., Any], name: str, args: dict) -> Any:
        return executor(name, args)

    async def _run_confirm(self, title: str, message: str):
        """Call the confirmation callback off the event loop. Returns bool or
        a JSONResponse when the callback itself timed out / failed."""
        try:
            approved = await asyncio.wait_for(
                asyncio.to_thread(self._confirm, title, message),
                timeout=self._timeout,
            )
            if inspect.isawaitable(approved):
                approved = await approved
            return bool(approved)
        except asyncio.TimeoutError:
            self._log(f"[ToolBridge] Confirmation for '{title}' timed out.")
            return JSONResponse(
                status_code=504,
                content={"status": "timeout",
                         "error": f"Confirmation for '{title}' timed out."},
            )
        except Exception as exc:
            self._log(f"[ToolBridge] Confirmation for '{title}' failed: {exc}")
            return JSONResponse(
                status_code=500,
                content={"status": "error", "error": f"Confirmation failed: {exc}"},
            )

    # ── delegation helpers ─────────────────────────────────────────────────

    @staticmethod
    def _find_method(component: Any, names: tuple[str, ...]) -> Optional[Callable]:
        if component is None:
            return None
        if isinstance(component, dict):
            for name in names:
                fn = component.get(name)
                if callable(fn):
                    return fn
            return None
        for name in names:
            fn = getattr(component, name, None)
            if callable(fn):
                return fn
        return None

    @staticmethod
    def _filter_kwargs(fn: Callable, kwargs: dict) -> dict:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return kwargs
        params = sig.parameters.values()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            return kwargs
        allowed = {p.name for p in params
                   if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                 inspect.Parameter.KEYWORD_ONLY)}
        return {key: value for key, value in kwargs.items() if key in allowed}

    async def _delegate(self, fn: Callable, **kwargs):
        try:
            value = await asyncio.to_thread(
                lambda: fn(**self._filter_kwargs(fn, kwargs)))
            if inspect.isawaitable(value):
                value = await value
        except Exception as exc:
            self._log(f"[ToolBridge] Delegated call failed: {exc}")
            return JSONResponse(status_code=500,
                                content={"status": "error", "error": str(exc)})
        return {"status": "ok", "result": _jsonable(value)}

    @staticmethod
    def _not_configured(component: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=501,
            content={"status": "unavailable", "component": component,
                     "error": message},
        )

    # ── audit ──────────────────────────────────────────────────────────────

    def _audit_action(self, action: str, args: dict, tier: str, approved: bool,
                      result: str, status: str) -> None:
        self._audit(action=action, args=args, tier=tier, approved=approved,
                    result=result, status=status)

    def _audit(self, *, action: str, args: Any, tier: str, approved: bool,
               result: Any, status: str) -> None:
        if self._audit_path is None:
            return
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "args": redact_args(args if isinstance(args, dict) else {"value": args}),
            "tier": tier,
            "approved": bool(approved),
            "result": summarize(result),
            "status": status,
        }
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str)
            with self._audit_lock:
                with self._audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # auditing must never break a call
            self._log(f"[ToolBridge] Could not write audit record: {exc}")

    # ── tool receipts (HMAC) ──────────────────────────────────────────────

    def _issue_receipt(self, tool: str, args: dict, result: Any) -> None:
        """Gera e persiste um recibo HMAC para o tool call.

        O recibo eh acrescentado ao mesmo JSONL de audit (append-only).
        Falhas sao silenciadas — recibos nao podem quebrar um call.
        A chave nunca eh logada; apenas o HMAC resultante vai ao disco.
        """
        if self._receipts_key is None or self._audit_path is None:
            return
        try:
            receipt = _receipts.make_receipt(
                tool, args if isinstance(args, dict) else {}, result,
                key=self._receipts_key,
            )
            line = json.dumps(receipt, ensure_ascii=False, default=str)
            with self._audit_lock:
                with self._audit_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            self._log(f"[ToolBridge] could not issue receipt: {exc}")

    # ── lifecycle ──────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and self._server is not None
                    and getattr(self._server, "started", False))

    def start(self) -> int:
        """Start uvicorn on a daemon thread and return the actually bound port.

        ``port=0`` lets the OS choose; the real port is read from the listening
        socket after startup, which makes tests (and two assistants on one host)
        safe.
        """
        with self._lifecycle_lock:
            if self.is_running():
                return self._port

            import uvicorn  # imported lazily so importing this module stays cheap

            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self._requested_port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)

            def _run() -> None:
                try:
                    server.run()
                except BaseException as exc:  # noqa: BLE001 - reported by start()
                    self._log(f"[ToolBridge] Server stopped: {exc}")

            thread = threading.Thread(target=_run, name="jarvis-tool-bridge",
                                      daemon=True)
            thread.start()

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if getattr(server, "started", False):
                    break
                if not thread.is_alive():
                    raise RuntimeError(
                        "ToolBridge server exited during startup; is the port in use?")
                time.sleep(0.02)
            else:
                server.should_exit = True
                thread.join(timeout=2.0)
                raise RuntimeError("ToolBridge server did not start within 10s")

            port = self._requested_port
            try:
                sockets = server.servers[0].sockets
                if sockets:
                    port = int(sockets[0].getsockname()[1])
            except Exception:
                pass

            self._server = server
            self._thread = thread
            self._port = port
            self._log(f"[ToolBridge] listening on {self.url()}")
            return port

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            if server is not None:
                server.should_exit = True
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    self._log("[ToolBridge] server thread did not stop in time")
            if server is not None:
                self._log("[ToolBridge] stopped")

    def url(self) -> str:
        return f"http://{self.host}:{self._port}"

    # ── logging ────────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        try:
            self._log_fn(message)
        except Exception:
            pass
