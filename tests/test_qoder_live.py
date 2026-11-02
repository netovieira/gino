"""Tests for core/qoder_live.py — spawn, JSONL protocol, callbacks, teardown.

The real Qoder model is never called here. Every test drives a tiny temporary
Python "sidecar" (``node_cmd=sys.executable``) that speaks the same JSONL
protocol as ``qoder_live/index.mjs``: it emits ``ready``, echoes asks, honours
``interrupt``/``close``, and can be told to fail, hang or emit garbage through
``FAKE_MODE``.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid

import pytest

from core.qoder_live import QoderLive

FAKE_SOURCE = r'''"""Fake Qoder Live sidecar used by tests/test_qoder_live.py."""
import json
import os
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    mode = os.environ.get("FAKE_MODE", "normal")
    record = os.environ.get("FAKE_RECORD", "")
    if mode == "garbage":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
    if mode == "fail":
        out({"type": "error", "error": "boom: warmup failed"})
        return 1
    if mode == "hang":
        time.sleep(60)
        return 0
    out({"type": "ready", "model": "Fake-Model", "warmup_ms": 7})
    out({
        "type": "models",
        "source": "fallback",
        "models": [{
            "value": "Fake-Model",
            "displayName": "Fake-Model",
            "isFree": True,
            "priceFactor": 0.0,
            "originalPriceFactor": 0.0,
            "availableContextWindows": [200000],
            "maxInputTokens": 200000,
            "efforts": ["low", "medium", "high", "max"],
            "defaultEffort": "medium",
        }],
    })
    current = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if record:
            with open(record, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "ask":
            current = msg.get("id")
            speaker = msg.get("speaker")
            text = str(msg.get("text", ""))
            if mode == "slow":
                out({"type": "text", "id": current, "text": "start", "delta": True})
                continue
            out({"type": "text", "id": current, "text": "eco:", "delta": True})
            out({"type": "text", "id": current, "text": " " + text, "delta": True})
            out({
                "type": "tool",
                "id": current,
                "name": "jarvis_call",
                "input": {"name": "music_control"},
            })
            out({"type": "done", "id": current, "text": speaker + ":" + text if speaker else "-:" + text})
            current = None
        elif kind == "persona_ask":
            persona = str(msg.get("persona") or "")
            task = str(msg.get("task") or "")
            out({"type": "session", "id": "persona-session-" + persona, "persona": persona})
            if msg.get("fail"):
                out({"type": "persona_error", "persona": persona, "error": "persona falhou"})
                out({"type": "persona_done", "persona": persona, "text": "",
                     "status": "error"})
            else:
                out({"type": "persona_text", "persona": persona,
                     "text": "resposta de " + persona, "delta": False})
                out({"type": "persona_text", "persona": persona,
                     "text": " sobre " + task, "delta": False})
                out({"type": "persona_done", "persona": persona,
                     "text": "resposta de " + persona + " sobre " + task,
                     "status": "ok"})
        elif kind == "interrupt":
            persona = msg.get("persona")
            if persona is not None:
                targets = [persona] if persona != "all" else ["fulana", "beltrano"]
                for slug in targets:
                    out({"type": "persona_done", "persona": slug, "text": "",
                         "status": "interrupted"})
            elif current is not None:
                out({"type": "interrupted", "id": current})
                out({"type": "done", "id": current, "text": "late completion"})
                current = None
        elif kind == "close":
            out({"type": "closed"})
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


class Recorder:
    """Thread-safe-ish callback sink (list.append is atomic under the GIL)."""

    def __init__(self):
        self.texts = []
        self.tools = []
        self.dones = []
        self.errors = []
        self.readies = []
        self.closed = 0
        self.persona_texts = []
        self.persona_dones = []
        self.persona_errors = []
        self.persona_sessions = []
        self.models = []
        self.thinking = []
        self.subagent = []

    def handlers(self):
        return {
            "on_text": lambda msg_id, text, delta: self.texts.append((msg_id, text, delta)),
            "on_tool": lambda msg_id, name, tool_input: self.tools.append(
                (msg_id, name, tool_input)
            ),
            "on_done": lambda msg_id, text: self.dones.append((msg_id, text)),
            "on_error": lambda msg_id, error: self.errors.append((msg_id, error)),
            "on_ready": lambda model, warmup_ms: self.readies.append((model, warmup_ms)),
            "on_closed": lambda: setattr(self, "closed", self.closed + 1),
            "on_persona_text": lambda persona, text: self.persona_texts.append(
                (persona, text)
            ),
            "on_persona_done": lambda persona, text, status: self.persona_dones.append(
                (persona, text, status)
            ),
            "on_persona_error": lambda persona, error: self.persona_errors.append(
                (persona, error)
            ),
            "on_persona_session": lambda persona, session_id: self.persona_sessions.append(
                (persona, session_id)
            ),
            "on_models": lambda models: self.models.append(models),
            "on_thinking": lambda persona, text: self.thinking.append(
                (persona, text)
            ),
            "on_subagent": lambda event: self.subagent.append(event),
        }


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def sidecar_threads():
    return [t for t in threading.enumerate() if t.name.startswith("qoder-live")]


def assert_no_sidecar_threads(timeout=5.0):
    assert wait_until(lambda: not sidecar_threads(), timeout), (
        f"leaked threads: {[t.name for t in sidecar_threads()]}"
    )


@pytest.fixture(scope="session")
def fake_script(tmp_path_factory):
    path = tmp_path_factory.mktemp("qoder-live") / "fake_sidecar.py"
    path.write_text(FAKE_SOURCE, encoding="utf-8")
    return str(path)


@pytest.fixture
def make_live(fake_script):
    instances = []

    def factory(**overrides):
        env = {"FAKE_MODE": "normal"}
        env.update(overrides.pop("env", {}) or {})
        overrides.setdefault("node_cmd", sys.executable)
        overrides.setdefault("logger", lambda *_args, **_kwargs: None)
        live = QoderLive(script=fake_script, env=env, **overrides)
        instances.append(live)
        return live

    yield factory

    for live in instances:
        try:
            live.stop(timeout=5.0)
        except Exception:  # cleanup must never mask the real failure
            pass


# ── start / ready ────────────────────────────────────────────────────────────


def test_start_waits_for_ready(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.ready is False
    assert live.is_ready() is False

    assert live.start(timeout=15.0) is True

    assert live.is_ready() is True
    assert live.ready is True
    assert live.last_error is None
    assert isinstance(live.pid, int)
    assert wait_until(lambda: rec.readies)
    assert rec.readies == [("Fake-Model", 7)]


def test_start_is_idempotent(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True
    first_pid = live.pid

    assert live.start(timeout=5.0) is True
    assert live.pid == first_pid
    assert wait_until(lambda: len(rec.readies) == 1)
    assert len(rec.readies) == 1


def test_ask_generates_uuid_when_id_missing(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True

    returned = live.ask("oi")
    assert str(uuid.UUID(returned)) == returned
    assert wait_until(lambda: rec.dones)
    assert rec.dones == [(returned, "-:oi")]


def test_start_timeout_on_silent_sidecar(make_live):
    live = make_live(env={"FAKE_MODE": "hang"})
    started = time.monotonic()
    assert live.start(timeout=0.4) is False
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"start() blocked for {elapsed:.1f}s"
    assert live.is_ready() is False
    assert "timed out" in (live.last_error or "")

    live.stop(timeout=3.0)
    assert live.is_ready() is False
    assert_no_sidecar_threads()


def test_start_error_before_ready(make_live):
    rec = Recorder()
    live = make_live(env={"FAKE_MODE": "fail"}, **rec.handlers())

    assert live.start(timeout=10.0) is False
    assert live.is_ready() is False
    assert "boom" in (live.last_error or "")
    assert wait_until(lambda: rec.errors)
    assert rec.errors[0][0] is None
    assert "boom" in rec.errors[0][1]
    # A failed boot never announced "closed" on its own.
    assert rec.closed == 0

    live.stop(timeout=3.0)
    assert_no_sidecar_threads()


def test_non_json_noise_is_ignored(make_live):
    rec = Recorder()
    live = make_live(env={"FAKE_MODE": "garbage"}, **rec.handlers())
    assert live.start(timeout=15.0) is True
    assert wait_until(lambda: rec.readies)

    live.ask("oi")
    assert wait_until(lambda: rec.dones)
    assert rec.dones[0][1] == "-:oi"


def test_restart_after_stop(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True
    live.stop(timeout=5.0)
    assert live.is_ready() is False

    assert live.start(timeout=15.0) is True
    live.ask("de novo")
    assert wait_until(lambda: len(rec.dones) == 1)
    assert rec.dones[0][1] == "-:de novo"


# ── ask / callbacks ──────────────────────────────────────────────────────────


def test_ask_streams_text_tool_and_done(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True

    returned = live.ask("toca música", speaker="Anthero", id="fixed-1")

    assert returned == "fixed-1"
    assert wait_until(lambda: rec.dones)
    assert [(item[0], item[1], item[2]) for item in rec.texts] == [
        ("fixed-1", "eco:", True),
        ("fixed-1", " toca música", True),
    ]
    assert rec.tools == [("fixed-1", "jarvis_call", {"name": "music_control"})]
    assert rec.dones == [("fixed-1", "Anthero:toca música")]


def test_callback_exception_is_logged_and_does_not_kill_reader(make_live):
    logs = []
    rec = Recorder()

    def exploding_text(*_args):
        raise ValueError("callback exploded")

    live = make_live(
        logger=logs.append,
        on_text=exploding_text,
        on_done=rec.handlers()["on_done"],
        on_ready=rec.handlers()["on_ready"],
    )
    assert live.start(timeout=15.0) is True

    live.ask("sobrevive", id="cb-1")
    assert wait_until(lambda: rec.dones)
    assert rec.dones == [("cb-1", "-:sobrevive")]
    assert any("on_text callback failed" in line and "callback exploded" in line for line in logs)


def test_thinking_events_are_forwarded():
    seen = []
    live = QoderLive(on_thinking=lambda persona, text: seen.append((persona, text)),
                     logger=lambda *_a: None)
    live._dispatch({"type": "thinking", "id": "t1",
                    "text": "Enviando a mensagem para Maria no WhatsApp."})
    live._dispatch({"type": "thinking", "persona": "fulana",
                    "text": "A Fulana ainda está trabalhando nisso."})
    assert seen == [
        (None, "Enviando a mensagem para Maria no WhatsApp."),
        ("fulana", "A Fulana ainda está trabalhando nisso."),
    ]


def test_task_events_are_forwarded_to_the_owner():
    seen = []
    live = QoderLive(on_task=seen.append, logger=lambda *_a: None)
    live._dispatch({"type": "task", "phase": "started", "id": "t1",
                    "description": "push"})
    live._dispatch({"type": "task", "phase": "done", "id": "t1",
                    "status": "completed", "summary": "ok"})
    assert [e["phase"] for e in seen] == ["started", "done"]
    assert seen[1]["status"] == "completed"


def test_subagent_events_are_forwarded():
    seen = []
    live = QoderLive(on_subagent=seen.append, logger=lambda *_a: None)
    live._dispatch({"type": "subagent", "task_id": "t1", "tool_use_id": "call_1",
                    "name": "Bash", "input": {"command": "pytest -q"}})
    live._dispatch({"type": "subagent", "task_id": "t1", "tool_use_id": "call_1",
                    "text": "Trabalhando no executor."})
    assert seen[0]["name"] == "Bash"
    assert seen[0]["task_id"] == "t1"
    assert seen[1]["text"] == "Trabalhando no executor."


def test_interrupt_does_not_clear_inflight_delivery(make_live):
    rec = Recorder()
    live = make_live(env={"FAKE_MODE": "slow"}, **rec.handlers())
    assert live.start(timeout=15.0) is True

    msg_id = live.ask("demorado")
    assert wait_until(lambda: rec.texts)
    assert rec.dones == []

    live.interrupt()

    # The completion raced the interrupt; it is still delivered under its id.
    assert wait_until(lambda: rec.dones)
    assert rec.dones == [(msg_id, "late completion")]
    assert live.is_ready() is True


def test_protocol_lines_written_to_stdin(make_live, tmp_path):
    record_path = tmp_path / "stdin.jsonl"
    live = make_live(env={"FAKE_RECORD": str(record_path)})
    assert live.start(timeout=15.0) is True

    live.ask("olá", speaker="Heloisa", id="abc-123")
    wait_until(lambda: record_path.exists() and "abc-123" in record_path.read_text())
    live.interrupt()
    live.stop(timeout=5.0)

    lines = [
        json.loads(line)
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {"type": "ask", "id": "abc-123", "text": "olá", "speaker": "Heloisa"} in lines
    assert {"type": "interrupt"} in lines
    assert {"type": "close"} in lines


def test_ask_carries_the_subject_epoch_context(make_live, tmp_path):
    record_path = tmp_path / "stdin-context.jsonl"
    live = make_live(env={"FAKE_RECORD": str(record_path)})
    assert live.start(timeout=15.0) is True

    live.ask("oi", id="ctx-1", context="[NOVO ASSUNTO APÓS PAUSA]")
    wait_until(lambda: record_path.exists() and "ctx-1" in record_path.read_text())
    live.stop(timeout=5.0)

    lines = [
        json.loads(line)
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(line.get("id") == "ctx-1"
               and line.get("context") == "[NOVO ASSUNTO APÓS PAUSA]"
               for line in lines)


def test_ask_requires_ready_sidecar(make_live):
    live = make_live()
    with pytest.raises(RuntimeError):
        live.ask("cedo demais")


def test_ask_after_stop_raises(make_live):
    live = make_live()
    assert live.start(timeout=15.0) is True
    live.stop(timeout=5.0)
    assert live.is_ready() is False
    with pytest.raises(RuntimeError):
        live.ask("depois de parar")


def test_empty_text_is_rejected(make_live):
    live = make_live()
    assert live.start(timeout=15.0) is True
    with pytest.raises(ValueError):
        live.ask("   ")


# ── teardown / crashes ───────────────────────────────────────────────────────


def test_stop_is_idempotent_and_notifies_closed_once(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True
    assert live.pid is not None

    assert live.stop(timeout=5.0) is None
    assert live.stop(timeout=5.0) is None

    assert live.is_ready() is False
    assert live.ready is False
    assert wait_until(lambda: rec.closed == 1)
    assert rec.closed == 1
    assert_no_sidecar_threads()


def test_unexpected_exit_marks_not_ready_and_stop_survives(make_live):
    rec = Recorder()
    live = make_live(**rec.handlers())
    assert live.start(timeout=15.0) is True
    pid = live.pid
    assert pid is not None

    os.kill(pid, signal.SIGKILL)

    assert wait_until(lambda: not live.is_ready())
    assert wait_until(lambda: rec.closed == 1)
    assert live.last_error is not None
    assert "exited" in live.last_error

    # BrokenPipe / reaped process must not raise from stop().
    live.stop(timeout=3.0)
    assert_no_sidecar_threads()


def test_stop_before_start_is_safe(make_live):
    live = make_live()
    live.stop(timeout=1.0)
    assert live.is_ready() is False
    assert live.pid is None
    with pytest.raises(RuntimeError):
        live.ask("nada")
