"""
Qoder Live sidecar driver — one persistent Qoder session per JARVIS process.

WHAT THIS IS
    ``qoder_live/index.mjs`` is a Node sidecar that keeps a warm Qoder daemon
    and a multi-turn session alive, with the JARVIS tools exposed over MCP. It
    speaks newline-delimited JSON (JSONL) on stdin/stdout and logs on stderr.
    This module is the Python half: it spawns that process, parses its stdout,
    and turns the events into callbacks the voice pipeline can consume.

PROTOCOL (see ``qoder_live/index.mjs`` for the source of truth)
    Python -> sidecar:
        {"type": "ask", "id": "...", "text": "...", "speaker": "Anthero"}
        {"type": "interrupt"}
        {"type": "interrupt", "persona": "<slug>"|"all"}
        {"type": "close"}

    Sidecar -> Python:
        {"type": "ready", "model": "Qwen3.8-Flash", "warmup_ms": 1234}
        {"type": "models", "models": [...], "source": "remote"|"fallback"}
        {"type": "model_switch", "from": "...", "to": "...", "role": "..."}
        {"type": "session", "id": "..."}            (resumable session id)
        {"type": "session", "id": "...", "persona": "<slug>"}
        {"type": "text", "id": "...", "text": "...", "delta": true|false}
        {"type": "partial", "id": "...", "text": "<delta>", "delta": true}
            (pré-aquecimento do orquestrador; nunca é falado por si só)
        {"type": "text", "id": null, "text": "...", "delta": false}
            (resposta espontânea a task_notification: sem turno do dono)
        {"type": "tool", "id": "...", "name": "...", "input": {...}}
        {"type": "subagent", "id": "...", "task_id": "...",
         "tool_use_id": "...", "name": "...", "input": {...}}
        {"type": "subagent", "id": "...", "task_id": "...",
         "tool_use_id": "...", "kind": "text", "text": "..."}
        {"type": "task", "phase": "started|progress|done", "id": "...",
         "description": "...", "summary": "...", "status": "..."}
        {"type": "persona_text", "persona": "<slug>", "text": "...", "delta": false}
        {"type": "persona_done", "persona": "<slug>", "text": "...", "status": "ok"|"error"|"interrupted"}
        {"type": "persona_error", "persona": "<slug>", "error": "..."}
        {"type": "thinking", "id": "..."|null, "persona": "<slug>"?, "text": "<frase específica>"}
        {"type": "done", "id": "...", "text": "..."}
        {"type": "error", "id": "..."|null, "error": "..."}
        {"type": "closed"}
        {"type": "accepted"|"queued"|"interrupted", "id": "..."}   (ignored)

    The sidecar auto-warms on boot and prints ``ready`` when the daemon and the
    prompt are loaded, so no explicit warm-up message is needed. An ``error``
    with no previous ``ready`` means the warm-up failed and the session is
    unusable.

THREADING / ASYNCIO
    ``start()`` blocks the calling thread until ``ready`` arrives (up to
    ``timeout`` seconds). It is meant to be called from a worker thread, for
    example ``await asyncio.to_thread(live.start)`` from the app's event loop —
    never directly inside a coroutine, which would freeze audio and the HUD.
    The stdout and stderr pumps are daemon threads named ``qoder-live-*``.

    Callbacks run on the reader thread, in the order the sidecar emitted them.
    They must be fast and must not call ``start()``/``stop()`` synchronously
    (``stop()`` joins that same thread; it detects and skips self-join, but a
    callback that blocks on the start lock can stall a restart). Exceptions
    raised by callbacks are logged and swallowed so one bad handler cannot
    kill the reader.

    ``ask()`` does not wait for the answer: it writes the line and returns the
    turn id. Text, tools and completion arrive later through callbacks, which
    is what lets the voice loop synthesize the first sentence while the rest of
    the answer is still being produced.

    If ``start()`` times out the process is left warming: ``on_ready`` may
    still fire later and the owner can switch to it then. ``stop()`` is always
    the one that reaps the process.

    Nothing here imports Qt or ``main.py``; it stays importable from tests and
    from headless scripts.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = PROJECT_ROOT / "qoder_live" / "index.mjs"
READER_THREAD_NAME = "qoder-live-reader"
STDERR_THREAD_NAME = "qoder-live-stderr"

NOOP_LOGGER: Callable[[str], None] = lambda *_args, **_kwargs: None  # noqa: E731


class QoderLive:
    """Owns the Node sidecar process and exposes its JSONL session in Python."""

    def __init__(
        self,
        script: Optional[str | os.PathLike[str]] = None,
        cwd: Optional[str | os.PathLike[str]] = None,
        env: Optional[dict[str, str]] = None,
        node_cmd: str = "node",
        on_text: Optional[Callable[[Optional[str], str, bool], None]] = None,
        on_partial: Optional[Callable[[Optional[str], str], None]] = None,
        on_tool: Optional[Callable[[Optional[str], str, dict[str, Any]], None]] = None,
        on_done: Optional[Callable[[Optional[str], str], None]] = None,
        on_error: Optional[Callable[[Optional[str], str], None]] = None,
        on_ready: Optional[Callable[[Optional[str], Optional[int]], None]] = None,
        on_session: Optional[Callable[[str], None]] = None,
        on_closed: Optional[Callable[[], None]] = None,
        on_task: Optional[Callable[[dict[str, Any]], None]] = None,
        on_subagent: Optional[Callable[[dict[str, Any]], None]] = None,
        on_persona_text: Optional[Callable[[str, str], None]] = None,
        on_persona_done: Optional[Callable[[str, str, str], None]] = None,
        on_persona_error: Optional[Callable[[str, str], None]] = None,
        on_persona_session: Optional[Callable[[str, str], None]] = None,
        on_models: Optional[Callable[[list[dict[str, Any]]], None]] = None,
        on_model_switch: Optional[Callable[[str, str, str], None]] = None,
        on_thinking: Optional[Callable[[Optional[str], str], None]] = None,
        logger: Optional[Callable[[str], None]] = print,
    ) -> None:
        self.script = str(script) if script is not None else str(DEFAULT_SCRIPT)
        self.cwd = str(cwd) if cwd is not None else str(PROJECT_ROOT)
        # Extra variables merged over the parent environment (bridge URL/token,
        # model, etc.). Values are stringified because Popen requires strings.
        self.env = {str(key): str(value) for key, value in (env or {}).items()}
        self.node_cmd = str(node_cmd)

        self.on_text = on_text
        self.on_partial = on_partial
        self.on_tool = on_tool
        self.on_done = on_done
        self.on_error = on_error
        self.on_ready = on_ready
        self.on_session = on_session
        self.on_closed = on_closed
        self.on_task = on_task
        self.on_subagent = on_subagent
        self.on_persona_text = on_persona_text
        self.on_persona_done = on_persona_done
        self.on_persona_error = on_persona_error
        self.on_persona_session = on_persona_session
        self.on_models = on_models
        self.on_model_switch = on_model_switch
        self.on_thinking = on_thinking
        self.logger = logger or NOOP_LOGGER

        self._lock = threading.Lock()          # readiness + process state
        self._write_lock = threading.Lock()    # stdin serialization
        self._start_lock = threading.RLock()   # spawn/stop serialization

        self._proc: Optional[subprocess.Popen[str]] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._generation = 0
        self._ready_event = threading.Event()
        self._ready = False
        self._ever_ready = False
        self._closed_notified = False
        self._stopping = False
        self._last_error: Optional[str] = None

    # ── public state ─────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """True only while the sidecar is alive and has emitted ``ready``."""
        return self.is_ready()

    def is_ready(self) -> bool:
        """True only while the sidecar is alive and has emitted ``ready``."""
        with self._lock:
            proc = self._proc
            return bool(self._ready and proc is not None and proc.poll() is None)

    @property
    def pid(self) -> Optional[int]:
        with self._lock:
            proc = self._proc
            return proc.pid if proc is not None else None

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, timeout: float = 90.0) -> bool:
        """Spawn the sidecar and wait up to ``timeout`` seconds for ``ready``.

        Idempotent: if the current process is alive, no second process is
        spawned; the call just waits on the existing readiness event (which is
        already set when the sidecar is up). Blocking — call it from a worker
        thread (``asyncio.to_thread``) when the app runs an event loop.
        """
        timeout = max(0.0, float(timeout))
        with self._start_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    # Reap the pipes/threads of the dead instance before reuse.
                    self._join_threads(1.0)
                    self._close_streams(proc)
                    with self._lock:
                        if self._proc is proc:
                            self._proc = None
                self._spawn_locked()
            event = self._ready_event

        deadline = time.monotonic() + timeout
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if event.wait(min(0.25, remaining)):
                break
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                break

        if not self._ready:
            with self._lock:
                if self._last_error is None:
                    self._last_error = f"timed out after {timeout:.1f}s waiting for sidecar ready"
            self._log(self._last_error)
            return False
        return True

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the sidecar to close, then wait/terminate/kill and join threads.

        Never raises for a broken pipe, an already dead process or a missing
        stdin: shutdown must be safe from a ``finally`` block.
        """
        timeout = max(0.1, float(timeout))
        had_proc = False
        with self._start_lock:
            with self._lock:
                proc = self._proc
                had_proc = proc is not None or self._ever_ready
                self._stopping = True
                self._ready = False

            if proc is None:
                self._ready_event.set()
            else:
                self._log(f"stopping pid={proc.pid}")
                self._send({"type": "close"})
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._log("close did not finish in time; terminating")
                    try:
                        proc.terminate()
                        proc.wait(timeout=min(5.0, timeout))
                    except (subprocess.TimeoutExpired, OSError):
                        try:
                            proc.kill()
                            proc.wait(timeout=2.0)
                        except (subprocess.TimeoutExpired, OSError):
                            pass
                self._join_threads(2.0)
                self._close_streams(proc)
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                self._ready_event.set()

        if had_proc:
            self._notify_closed()

    # ── conversation ─────────────────────────────────────────────────────────

    def ask(self, text: str, speaker: Optional[str] = None, id: Optional[str] = None,
            priority: Optional[str] = None, context: Optional[str] = None) -> str:
        """Queue a turn on the warm session and return its id.

        Returns immediately; ``on_text``/``on_tool``/``on_done``/``on_error``
        deliver the answer. ``priority='now'`` preempts the active turn in the
        sidecar (background agents keep running). Raises ``RuntimeError``
        (message from ``last_error`` when known) if the sidecar is not ready or
        has died.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("ask() needs non-empty text")
        self._ensure_ready()
        message_id = str(id) if id is not None else str(uuid.uuid4())
        payload: dict[str, Any] = {"type": "ask", "id": message_id, "text": text}
        if speaker:
            payload["speaker"] = str(speaker)
        if priority in ("now", "next", "later"):
            payload["priority"] = priority
        if context:
            payload["context"] = str(context)
        self._send(payload, raise_errors=True)
        return message_id

    def interrupt(self) -> None:
        """Barge-in: ask the sidecar to stop the current turn.

        The sidecar may still have a completion in flight; this method does not
        drop any pending id or callback, so whatever it emits afterwards is
        still delivered to ``on_done``/``on_error``. Best effort: a dead
        sidecar only logs.
        """
        with self._lock:
            proc = self._proc
            usable = self._ready and proc is not None
        if not usable or proc is None or proc.poll() is not None:
            self._log("interrupt ignored: sidecar is not ready")
            return
        self._send({"type": "interrupt"})

    def interrupt_persona(self, persona: Optional[str] = None) -> None:
        """Abort one persona session (or all of them) inside the sidecar.

        ``persona`` is the persona slug; ``None`` means every persona. The
        owner's own turn is untouched: ``{"type": "interrupt"}`` without a
        persona keeps meaning "stop the main turn". The sidecar answers with
        ``persona_done``/``status="interrupted"`` for each persona that had a
        turn in flight. Best effort, like :meth:`interrupt`.
        """
        with self._lock:
            proc = self._proc
            usable = self._ready and proc is not None
        if not usable or proc is None or proc.poll() is not None:
            self._log("interrupt_persona ignored: sidecar is not ready")
            return
        target = str(persona).strip() if persona else "all"
        if not target:
            target = "all"
        self._send({"type": "interrupt", "persona": target})

    # ── process plumbing ─────────────────────────────────────────────────────

    def _spawn_locked(self) -> None:
        self._generation += 1
        generation = self._generation
        self._ready = False
        self._ever_ready = False
        self._closed_notified = False
        self._stopping = False
        self._last_error = None
        self._ready_event = threading.Event()

        env = dict(os.environ)
        env.update(self.env)
        argv = [self.node_cmd, self.script]
        try:
            proc = subprocess.Popen(
                argv,
                cwd=self.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self._proc = None
            self._last_error = f"failed to start sidecar: {exc}"
            self._log(self._last_error)
            self._ready_event.set()
            return

        self._proc = proc
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            args=(proc, generation),
            name=READER_THREAD_NAME,
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(proc, generation),
            name=STDERR_THREAD_NAME,
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        self._log(f"spawned pid={proc.pid} script={self.script}")

    def _send(self, payload: dict[str, Any], *, raise_errors: bool = False) -> bool:
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc = self._proc
            stdin = proc.stdin if proc is not None else None
            if stdin is None:
                error = self._last_error or "sidecar is not running"
                if raise_errors:
                    raise RuntimeError(error)
                return False
            try:
                stdin.write(data)
                stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                message = f"write failed: {exc}"
                with self._lock:
                    self._ready = False
                    if self._last_error is None:
                        self._last_error = message
                self._ready_event.set()
                self._log(message)
                if raise_errors:
                    raise RuntimeError(self._last_error or message) from exc
                return False

    def _ensure_ready(self) -> None:
        with self._lock:
            proc = self._proc
            ready = self._ready
            error = self._last_error
            alive = proc is not None and proc.poll() is None
            if proc is not None and not alive:
                self._ready = False
        if ready and alive:
            return
        if error:
            raise RuntimeError(error)
        if proc is None:
            raise RuntimeError("Qoder Live sidecar is not running")
        if not alive:
            raise RuntimeError("Qoder Live sidecar process has exited")
        raise RuntimeError("Qoder Live sidecar is not ready")

    def _read_stdout(self, proc: subprocess.Popen[str], generation: int) -> None:
        stdout = proc.stdout
        try:
            if stdout is None:
                return
            while True:
                try:
                    line = stdout.readline()
                except (ValueError, OSError) as exc:
                    self._log(f"stdout read failed: {exc}")
                    break
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._log(f"ignoring non-JSON line: {line[:160]}")
                    continue
                if not isinstance(message, dict):
                    self._log("ignoring non-object JSON message")
                    continue
                if generation != self._generation:
                    break
                try:
                    self._dispatch(message)
                except Exception as exc:  # a handler bug must not kill the pump
                    self._log(f"message handler failed: {exc!r}")
        finally:
            self._handle_eof(proc, generation)

    def _read_stderr(self, proc: subprocess.Popen[str], generation: int) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        while generation == self._generation:
            try:
                line = stderr.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            text = line.rstrip()
            if text:
                self._log(f"stderr: {text}")

    def _handle_eof(self, proc: subprocess.Popen[str], generation: int) -> None:
        if generation != self._generation:
            return
        try:
            code = proc.poll()
        except Exception:  # pragma: no cover - poll is trivial
            code = None
        with self._lock:
            was_ready = self._ready or self._ever_ready
            self._ready = False
            if not self._stopping and self._last_error is None:
                self._last_error = f"sidecar exited unexpectedly (code {code})"
        self._ready_event.set()
        if not self._stopping:
            self._log(f"sidecar stdout closed (exit code {code})")
        if was_ready:
            self._notify_closed()

    def _close_streams(self, proc: subprocess.Popen[str]) -> None:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _join_threads(self, timeout: float) -> None:
        current = threading.current_thread()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is None or thread is current:
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._log(f"thread {thread.name} still running; leaving it daemonised")

    # ── event dispatch ───────────────────────────────────────────────────────

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ready":
            model = message.get("model")
            warmup_ms = message.get("warmup_ms")
            with self._lock:
                self._ready = True
                self._ever_ready = True
                self._last_error = None
            self._ready_event.set()
            self._log(f"ready model={model} warmup_ms={warmup_ms}")
            self._emit("on_ready", model, warmup_ms)
        elif kind == "text":
            self._emit(
                "on_text",
                message.get("id"),
                str(message.get("text") or ""),
                bool(message.get("delta")),
            )
        elif kind == "partial":
            self._emit(
                "on_partial",
                message.get("id"),
                str(message.get("text") or ""),
            )
        elif kind == "tool":
            tool_input = message.get("input")
            self._emit(
                "on_tool",
                message.get("id"),
                str(message.get("name") or ""),
                tool_input if isinstance(tool_input, dict) else {},
            )
        elif kind == "task":
            self._emit("on_task", message)
        elif kind == "subagent":
            self._emit("on_subagent", message)
        elif kind == "models":
            models = message.get("models")
            self._emit("on_models", models if isinstance(models, list) else [])
        elif kind == "model_switch":
            self._emit(
                "on_model_switch",
                str(message.get("from") or ""),
                str(message.get("to") or ""),
                str(message.get("role") or ""),
            )
        elif kind == "session":
            session_id = str(message.get("id") or "")
            if session_id:
                persona = message.get("persona")
                if persona:
                    self._emit("on_persona_session", str(persona), session_id)
                else:
                    self._emit("on_session", session_id)
        elif kind == "persona_text":
            self._emit(
                "on_persona_text",
                str(message.get("persona") or ""),
                str(message.get("text") or ""),
            )
        elif kind == "persona_done":
            self._emit(
                "on_persona_done",
                str(message.get("persona") or ""),
                str(message.get("text") or ""),
                str(message.get("status") or ""),
            )
        elif kind == "persona_error":
            self._emit(
                "on_persona_error",
                str(message.get("persona") or ""),
                str(message.get("error") or "unknown persona error"),
            )
        elif kind == "thinking":
            persona = message.get("persona")
            self._emit(
                "on_thinking",
                str(persona) if persona else None,
                str(message.get("text") or ""),
            )
        elif kind == "done":
            self._emit("on_done", message.get("id"), str(message.get("text") or ""))
        elif kind == "error":
            error = str(message.get("error") or "unknown sidecar error")
            with self._lock:
                startup_failure = not self._ready
                self._last_error = error
                if startup_failure:
                    self._ready = False
            if startup_failure:
                self._ready_event.set()
            self._log(f"error id={message.get('id')}: {error}")
            self._emit("on_error", message.get("id"), error)
        elif kind == "closed":
            with self._lock:
                self._ready = False
                if not self._stopping and self._last_error is None:
                    self._last_error = "sidecar closed"
            self._ready_event.set()
            self._notify_closed()
        elif kind in ("accepted", "queued", "interrupted"):
            # Lifecycle acknowledgements: no client-side state to update.
            return
        else:
            self._log(f"ignoring unknown message type: {kind!r}")

    def _emit(self, name: str, *args: Any) -> None:
        callback = getattr(self, name, None)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            self._log(f"{name} callback failed: {exc!r}")

    def _notify_closed(self) -> None:
        with self._lock:
            if self._closed_notified:
                return
            self._closed_notified = True
        self._emit("on_closed")

    def _log(self, message: str) -> None:
        try:
            self.logger(f"[QoderLive] {message}")
        except Exception:
            pass
