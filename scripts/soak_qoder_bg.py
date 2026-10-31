"""24-hour soak: background Agent executions must never hold the conversation.

Runs the real Qoder sidecar (free model) in a loop, alternating:

  1. a turn that delegates a slow task to a background Agent (``sleep``),
  2. a conversational turn sent while that task is still running.

The conversation must finish before the background task, and the task must
still report completion afterwards. Every 10th cycle also preempts a long
answer with a new short turn.

Usage:
    python scripts/soak_qoder_bg.py --hours 24 --interval-min 15
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "qoder_live" / "index.mjs"
DEFAULT_MODEL = "Qwen3.8-Flash"


class Sidecar:
    def __init__(self, workdir: Path, model: str, log_path: Path):
        self.workdir = workdir
        self.model = model
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.reader: threading.Thread | None = None
        self.ready = threading.Event()
        self.closed = threading.Event()

    def start(self, timeout: float = 90.0) -> bool:
        env = os.environ.copy()
        env.update({
            "JARVIS_BRIDGE_URL": "http://127.0.0.1:9",
            "JARVIS_BRIDGE_TOKEN": "soak",
            "JARVIS_MODEL": self.model,
            "JARVIS_CWD": str(self.workdir),
            "JARVIS_ASSISTANT_NAME": "Gino",
            "JARVIS_SYSTEM_PROMPT_FILE": str(self.workdir / "prompt.txt"),
        })
        (self.workdir / "prompt.txt").write_text(
            "Você é Gino, um assistente de voz em português do Brasil. "
            "Responda em frases curtas e faláveis.\n", encoding="utf-8")
        self.ready.clear()
        self.closed.clear()
        self.q = queue.Queue()
        log = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            ["node", str(SIDECAR)], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, env=env, bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True,
                                       name="soak-sidecar-reader")
        self.reader.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.ready.is_set():
            if self.proc.poll() is not None:
                return False
            time.sleep(0.1)
        return self.ready.is_set()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("type") == "ready":
                self.ready.set()
            if msg.get("type") == "closed":
                self.closed.set()
            self.q.put(msg)
        self.closed.set()

    def send(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("sidecar is not running")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def ask(self, text: str, *, priority: str | None = None) -> str:
        msg_id = uuid.uuid4().hex[:12]
        payload = {"type": "ask", "id": msg_id, "text": text}
        if priority:
            payload["priority"] = priority
        self.send(payload)
        return msg_id

    def stop(self) -> None:
        try:
            self.send({"type": "close"})
        except Exception:
            pass
        try:
            if self.proc:
                self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class Soak:
    def __init__(self, args, run_dir: Path):
        self.args = args
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.summary_path = run_dir / "summary.json"
        self.workdir = run_dir / "cwd"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.sidecar = Sidecar(self.workdir, args.model, run_dir / "sidecar.log")
        self.summary = {
            "run_id": run_dir.name,
            "model": args.model,
            "started": datetime.now(timezone.utc).isoformat(),
            "hours": args.hours,
            "interval_min": args.interval_min,
            "cycles": 0, "completed": 0, "failed": 0, "restarts": 0,
            "parent_before_task": 0, "blocked": 0,
            "task_completed": 0, "task_missing": 0,
            "preempt_pass": 0, "preempt_fail": 0,
            "max_parent_s": 0.0, "max_conv_s": 0.0,
            "errors": [],
        }
        self.stop = threading.Event()

    # ── logging ─────────────────────────────────────────────────────────────
    def log(self, kind: str, **fields) -> None:
        line = {"ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind, **fields}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(f"[soak] {kind} {fields}", flush=True)

    def save(self) -> None:
        self.summary["updated"] = datetime.now(timezone.utc).isoformat()
        self.summary_path.write_text(
            json.dumps(self.summary, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ── sidecar lifecycle ───────────────────────────────────────────────────
    def ensure_sidecar(self) -> bool:
        if self.sidecar.proc and self.sidecar.proc.poll() is None and self.sidecar.ready.is_set():
            return True
        self.sidecar.stop()
        ok = self.sidecar.start()
        if ok:
            self.log("sidecar_ready", model=self.sidecar.model)
        else:
            self.log("sidecar_failed")
        return ok

    def restart(self) -> bool:
        self.summary["restarts"] += 1
        self.log("sidecar_restart", attempt=self.summary["restarts"])
        self.save()
        return self.ensure_sidecar()

    # ── message collection ──────────────────────────────────────────────────
    def wait_for(self, sink: list, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for i, (at, msg) in enumerate(sink):
                if predicate(msg):
                    return True
            if self.sidecar.closed.is_set() and not self.sidecar.q.qsize():
                return False
            try:
                msg = self.sidecar.q.get(timeout=0.25)
            except queue.Empty:
                continue
            sink.append((time.monotonic(), msg))
            if predicate(msg):
                return True
        return False

    @staticmethod
    def msg_id(msg: dict) -> str:
        return str(msg.get("id") or "")

    # ── one cycle ───────────────────────────────────────────────────────────
    def cycle(self, index: int) -> None:
        self.summary["cycles"] += 1
        t0 = time.monotonic()
        sink: list = []
        cycle_ok = True

        # 1) delegate a slow task
        task_text = (
            f"Delegue a um Agent em segundo plano a tarefa de rodar o comando "
            f"'sleep {self.args.task_seconds}; echo ciclo-{index}-ok'. Diga em "
            f"uma frase que ela está rodando e NÃO espere o resultado.")
        task_id = self.sidecar.ask(task_text)
        parent_ok = self.wait_for(
            sink, lambda m: m.get("type") == "done" and self.msg_id(m) == task_id,
            timeout=self.args.turn_timeout)
        parent_at = time.monotonic() - t0
        if parent_ok:
            self.summary["max_parent_s"] = max(self.summary["max_parent_s"], parent_at)
        else:
            cycle_ok = False
            self.log("parent_timeout", cycle=index, id=task_id)
        started = [m for _, m in sink
                   if m.get("type") == "task" and m.get("phase") == "started"]
        if not started:
            # The model may have answered without delegating; give it a moment.
            self.wait_for(sink, lambda m: m.get("type") == "task"
                          and m.get("phase") == "started", timeout=2.0)
            started = [m for _, m in sink
                       if m.get("type") == "task" and m.get("phase") == "started"]
        if not started:
            cycle_ok = False
            self.log("task_not_started", cycle=index, id=task_id)
        started_ids = {str(m.get("id")) for m in started}

        def _is_task_done(m: dict) -> bool:
            return (m.get("type") == "task" and m.get("phase") == "done"
                    and (not started_ids or str(m.get("id")) in started_ids))

        # 2) conversation while the task is still running
        conv_id = self.sidecar.ask(
            f"Responda em uma frase curta: qual é o número do ciclo {index}?",
            priority=None if parent_ok else "now")
        conv_ok = self.wait_for(
            sink, lambda m: m.get("type") == "done" and self.msg_id(m) == conv_id,
            timeout=self.args.turn_timeout)
        conv_at = time.monotonic() - t0
        if conv_ok:
            self.summary["max_conv_s"] = max(self.summary["max_conv_s"], conv_at)
        else:
            cycle_ok = False
            self.log("conversation_timeout", cycle=index, id=conv_id,
                     after_s=round(conv_at, 1))

        # 3) wait for the background result
        task_done = self.wait_for(sink, _is_task_done,
                                  timeout=max(30.0, self.args.task_seconds + 25.0))
        if task_done:
            self.summary["task_completed"] += 1
        else:
            self.summary["task_missing"] += 1
            cycle_ok = False
            self.log("task_done_missing", cycle=index)

        # 4) did the conversation finish while the task was still running?
        parent_msg = next((m for _, m in sink
                           if m.get("type") == "done" and self.msg_id(m) == task_id), None)
        task_done_msg = next((m for _, m in sink if _is_task_done(m)), None)
        if parent_msg and task_done_msg:
            # Order in the stream is the ground truth: the parent done must
            # appear before the task done for the conversation to be free.
            order = [m for _, m in sink if m in (parent_msg, task_done_msg)]
            if order and order[0] is parent_msg:
                self.summary["parent_before_task"] += 1
            else:
                self.summary["blocked"] += 1
                cycle_ok = False
                self.log("blocked_until_task", cycle=index)

        # 5) every 10th cycle: preemption of a long answer
        if index % 10 == 0:
            a = self.sidecar.ask("Escreva vinte parágrafos longos sobre o espaço.")
            time.sleep(1.0)
            b = self.sidecar.ask("Responda só: pronto.", priority="now")
            b_ok = self.wait_for(
                sink, lambda m: m.get("type") == "done" and self.msg_id(m) == b,
                timeout=30.0)
            a_int = any(m.get("type") == "interrupted" and self.msg_id(m) == a
                        for _, m in sink)
            if b_ok and a_int:
                self.summary["preempt_pass"] += 1
            else:
                self.summary["preempt_fail"] += 1
                cycle_ok = False
                self.log("preempt_fail", cycle=index, b_done=b_ok, a_int=a_int)

        if cycle_ok:
            self.summary["completed"] += 1
        else:
            self.summary["failed"] += 1
        errors = [str(m.get("error")) for _, m in sink if m.get("type") == "error"]
        if errors:
            self.summary["errors"].append({"cycle": index, "errors": errors[:3]})
        self.log("cycle_done", cycle=index, ok=cycle_ok,
                 parent_s=round(parent_at, 1), conv_s=round(conv_at, 1),
                 task_started=bool(started), task_done=bool(task_done))
        self.save()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--interval-min", type=float, default=15.0)
    ap.add_argument("--turn-timeout", type=float, default=45.0)
    ap.add_argument("--task-seconds", type=int, default=5)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default=str(ROOT / "logs" / "soak"))
    args = ap.parse_args()

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    soak = Soak(args, run_dir)

    def _stop(_signum, _frame):
        soak.stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not soak.ensure_sidecar():
        soak.log("abort", reason="sidecar did not become ready")
        soak.save()
        return 1

    interval = args.interval_min * 60.0
    start = time.monotonic()
    end = start + args.hours * 3600.0
    index = 1
    soak.log("start", run_id=run_id, hours=args.hours,
             interval_min=args.interval_min, model=args.model)
    failures_in_a_row = 0
    try:
        while not soak.stop.is_set() and time.monotonic() < end:
            if not soak.ensure_sidecar():
                failures_in_a_row += 1
                if failures_in_a_row >= 3:
                    soak.log("abort", reason="three sidecar starts failed")
                    break
                time.sleep(min(60.0, 5.0 * failures_in_a_row))
                continue
            try:
                soak.cycle(index)
            except Exception as e:
                soak.log("cycle_exception", cycle=index, error=repr(e))
                soak.summary["failed"] += 1
                soak.save()
                soak.restart()
            failures_in_a_row = 0
            index += 1
            next_at = start + (index - 1) * interval
            while not soak.stop.is_set() and time.monotonic() < next_at:
                time.sleep(min(5.0, max(0.1, next_at - time.monotonic())))
    finally:
        soak.summary["finished"] = datetime.now(timezone.utc).isoformat()
        soak.save()
        soak.sidecar.stop()
        soak.log("finish", **{k: soak.summary[k] for k in
                              ("cycles", "completed", "failed", "restarts",
                               "parent_before_task", "blocked",
                               "preempt_pass", "preempt_fail")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
