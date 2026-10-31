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
import hmac
import inspect
import json
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

# ── risk tiers ───────────────────────────────────────────────────────────────
#
# safe   — reads, status, search. No side effects worth asking about.
# low    — music, apps, files that can be reversed. Auto-run (undo is preferred
#          to a question for anything reversible).
# medium — something leaves the machine or changes shared state: sending a
#          message, uploading, purchases, controlling a phone/browser.
# high   — restart/shutdown, destructive file operations, code execution,
#          persona/secrets, and anything we do not recognise. A confirmation
#          (and, at the voice layer, the owner) is mandatory.
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
    "rastracking": TIER_SAFE,
    "system_monitor": TIER_SAFE,
    "background_monitor": TIER_SAFE,
    "screen_processor": TIER_SAFE,
    # ── low: music / apps / reversible files ───────────────────────────────
    "open_app": TIER_LOW,
    "music_control": TIER_LOW,
    "video_player": TIER_LOW,
    "youtube_video": TIER_LOW,
    "file_controller": TIER_LOW,
    "file_processor": TIER_LOW,
    "desktop_control": TIER_LOW,
    "organizer": TIER_LOW,
    "focus_mode": TIER_LOW,
    "humor_mode": TIER_LOW,
    "reminder": TIER_LOW,
    "game_updater": TIER_LOW,
    "computer_settings": TIER_LOW,
    "proactive": TIER_LOW,
    # ── medium: sending / uploads / purchases / remote devices ─────────────
    "send_message": TIER_MEDIUM,
    "email_control": TIER_MEDIUM,
    "whatsapp_control": TIER_MEDIUM,
    "trello_control": TIER_MEDIUM,
    "cakto_sales": TIER_MEDIUM,
    "android_control": TIER_MEDIUM,
    "iphone_control": TIER_MEDIUM,
    "mac_control": TIER_MEDIUM,
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
    "phrase_blocklist": TIER_HIGH,
    "restart_assistant": TIER_HIGH,
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
}

_ARG_SCAN_KEYS = ("action", "operation", "op", "command", "mode", "task")

_REDACT_RE = re.compile(r"(api[_-]?key|token|password|secret|authorization)", re.IGNORECASE)
_REDACTED = "[REDACTED]"

_DEFAULT_TIMEOUT = 120.0
_RESULT_SUMMARY_CHARS = 500


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


class VoiceAuthorizeRequest(BaseModel):
    name: str


class RestartRequest(BaseModel):
    reason: str = ""


class LocalAskRequest(BaseModel):
    prompt: str


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
    memory / voice / restart / local_ask:
        Optional delegated components. Each may be an object exposing methods
        or a dict of callables. When absent the endpoint answers HTTP 501.
    audit_path:
        Optional JSONL file. One record per /call and /confirm.
    """

    def __init__(self, registry, dispatch, token: str, host: str = "127.0.0.1",
                 port: int = 8011, confirm=None, logger: Callable[[str], None] = print,
                 audit_path=None, memory=None, voice=None, restart=None,
                 local_ask=None, timeout: float = _DEFAULT_TIMEOUT):
        if not isinstance(token, str) or not token:
            raise ValueError("ToolBridge requires a non-empty bearer token")
        self._registry = registry
        self._dispatch = dispatch
        self._token = token
        self.host = host
        self._requested_port = int(port)
        self._port = int(port)
        self._confirm = confirm
        self._log_fn = logger or (lambda _msg: None)
        self._audit_path = Path(audit_path) if audit_path else None
        self._memory = memory
        self._voice = voice
        self._restart = restart
        self._local_ask = local_ask
        self._timeout = float(timeout)

        self._audit_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._server = None
        self._thread: Optional[threading.Thread] = None

        self.app = self._build_app()

    # ── policy ─────────────────────────────────────────────────────────────

    def risk_tier(self, name: str, args: Optional[dict] = None) -> str:
        """Resolve the tier for an action name, escalating on dangerous args."""
        tier = ACTION_TIERS.get(str(name or ""), TIER_HIGH)
        tokens = _ESCALATING_ARGS.get(str(name or ""))
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
            return await self._delegate(fn, name=payload.name,
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
                self._audit_action(name, args, tier, approved=False,
                                   result=approved.body.decode("utf-8", "replace")
                                   if isinstance(approved.body, bytes) else str(approved.body),
                                   status=confirm_status)
                return approved
            if not approved:
                message = f"Action '{name}' was not confirmed."
                self._audit_action(name, args, tier, approved=False,
                                   result=message, status="denied")
                return JSONResponse(
                    status_code=403,
                    content={"status": "denied", "action": name,
                             "tier": tier, "message": message},
                )

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
        return {"status": "ok", "action": name, "tier": tier,
                "approved": True, "result": text}

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
