"""Persistent task/reminder queue and its voice action."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import tasks as action  # noqa: E402
from core import task_queue  # noqa: E402


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "tasks.json"
    monkeypatch.setattr(task_queue, "TASKS_PATH", path)
    task_queue.reset()
    yield task_queue, path
    task_queue.reset()


# ── core queue ──────────────────────────────────────────────────────────────

def test_add_and_add_many_persist(queue):
    q, path = queue
    first = q.add("  Comprar pão  ", priority=2, persona="Ana", project="casa")
    assert first["title"] == "Comprar pão"
    assert len(first["id"]) >= 8
    assert first["kind"] == "task"
    assert first["status"] == "queued"
    assert first["priority"] == 2
    assert first["due"] is None
    assert first["created"] and first["started"] is None
    assert first["persona"] == "Ana"
    assert first["project"] == "casa"
    assert first["payload"] == {}

    more = q.add_many([
        "Pagar conta",
        {"title": "Consulta", "kind": "reminder",
         "due": "2026-11-01T09:00:00", "priority": 7},
    ])
    assert [row["title"] for row in more] == ["Pagar conta", "Consulta"]
    assert more[1]["kind"] == "reminder"
    assert more[1]["due"].startswith("2026-11-01T09:00")
    assert len({first["id"], *[row["id"] for row in more]}) == 3

    stored = json.loads(path.read_text("utf-8"))
    assert stored["version"] == 1
    assert [row["title"] for row in stored["tasks"]] == [
        "Comprar pão", "Pagar conta", "Consulta"]
    assert [row["id"] for row in q.load()] == [
        first["id"], *[row["id"] for row in more]]


def test_order_is_priority_then_created_and_next_is_read_only(queue):
    q, _path = queue
    low = q.add("baixa", priority=0)
    high_first = q.add("alta primeiro", priority=10)
    high_second = q.add("alta depois", priority=10)

    assert q.next_task()["id"] == high_first["id"]
    assert q.list_tasks("queued")[0]["id"] == low["id"]
    # next_task only answers; it does not claim the task
    assert q.list_tasks("queued")[0]["status"] == "queued"
    assert q.start(high_first["id"])["status"] == "running"
    assert q.next_task()["id"] == high_second["id"]


def test_only_one_task_runs_at_a_time(queue):
    q, _path = queue
    first = q.add("primeira")
    second = q.add("segunda")

    started = q.start(first["id"])
    assert started["status"] == "running"
    assert started["started"]
    assert q.start(second["id"]) is None
    running = q.list_tasks("running")
    assert len(running) == 1 and running[0]["id"] == first["id"]

    finished = q.finish(first["id"], result="ok")
    assert finished["status"] == "done"
    assert finished["finished"]
    assert q.start(second["id"])["status"] == "running"


def test_finish_error_and_cancel(queue):
    q, _path = queue
    ok = q.add("concluir")
    q.start(ok["id"])
    done = q.finish(ok["id"], result="feito")
    assert done["status"] == "done" and done["result"] == "feito"
    assert done["error"] == ""

    bad = q.add("falhar")
    q.start(bad["id"])
    failed = q.finish(bad["id"], ok=False, error="sem rede")
    assert failed["status"] == "error" and failed["error"] == "sem rede"

    waiting = q.add("desistir")
    cancelled = q.cancel(waiting["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["finished"]
    assert q.cancel(waiting["id"])["status"] == "cancelled"

    assert q.start("nao-existe") is None
    assert q.finish("nao-existe") is None
    assert q.cancel("nao-existe") is None


def test_tick_returns_due_tasks_without_starting_them(queue):
    q, _path = queue
    now = datetime(2026, 1, 1, 12, 0, 0)
    past = q.add("remédio", kind="reminder", due="2026-01-01T11:00:00")
    future = q.add("dentista", kind="reminder", due="2026-01-01T13:00:00")
    ready = q.add("tarefa sem prazo")
    event = q.add("evento", kind="event", due="2026-01-01T12:00:00")

    due = q.tick(now)
    ids = {row["id"] for row in due}
    assert ids == {past["id"], ready["id"], event["id"]}
    assert future["id"] not in ids
    assert all(row["status"] == "queued" for row in due)
    assert all(row["status"] == "queued" for row in q.list_tasks())

    early = q.tick("2026-01-01T10:00:00")
    assert {row["id"] for row in early} == {ready["id"]}


def test_malformed_file_repairs_to_empty(queue):
    q, path = queue
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert q.load() == []
    repaired = json.loads(path.read_text("utf-8"))
    assert repaired == {"version": 1, "tasks": []}

    path.write_text('{"version": 1, "tasks": "broken"}', encoding="utf-8")
    assert q.load() == []
    added = q.add("depois do conserto")
    assert q.list_tasks("queued")[0]["id"] == added["id"]


def test_history_is_bounded_but_open_tasks_survive(queue):
    q, _path = queue
    rows = [{
        "id": f"done-{i:04d}", "title": f"done {i}", "kind": "task",
        "status": "done", "priority": 0, "due": None,
        "created": "2026-01-01T00:00:00", "started": None,
        "finished": "2026-01-01T00:00:01", "persona": None,
        "project": None, "payload": {}, "result": "", "error": "",
    } for i in range(505)]
    rows.append({
        "id": "open-queued", "title": "aberta", "kind": "task",
        "status": "queued", "priority": 0, "due": None,
        "created": "2026-01-02T00:00:00", "started": None,
        "finished": None, "persona": None, "project": None,
        "payload": {}, "result": "", "error": "",
    })
    rows.append({
        "id": "open-running", "title": "rodando", "kind": "task",
        "status": "running", "priority": 0, "due": None,
        "created": "2026-01-02T00:00:01", "started": "2026-01-02T00:00:02",
        "finished": None, "persona": None, "project": None,
        "payload": {}, "result": "", "error": "",
    })
    q.save(rows)

    done = q.list_tasks("done")
    assert len(done) == 500
    assert done[0]["title"] == "done 5"
    assert {row["id"] for row in q.list_tasks("open")} == {
        "open-queued", "open-running"}


def test_status_line_and_format_alias(queue):
    q, _path = queue
    empty = q.status()
    assert empty["line"] == "A fila está vazia."
    assert empty["current"] is None and empty["queued"] == 0

    first = q.add("Comprar pão")
    waiting = q.status()
    assert waiting["line"] == "1 tarefas na fila."
    assert waiting["next"]["id"] == first["id"]

    q.start(first["id"])
    current = q.status()
    assert current["line"] == "Já está na fila; ainda estou fazendo Comprar pão."
    assert current["current"]["id"] == first["id"]
    assert current["queued"] == 0

    second = q.add("Pagar conta")
    still_current = q.format_status()
    assert still_current["line"] == (
        "Já está na fila; ainda estou fazendo Comprar pão.")
    assert still_current["queued"] == 1
    assert still_current["next"]["id"] == second["id"]

    q.finish(first["id"])
    assert q.status()["line"] == "1 tarefas na fila."


# ── action wrapper ──────────────────────────────────────────────────────────

def _payload(result: str) -> dict:
    return json.loads(result)


def test_action_add_splits_list_and_status_uses_line(queue):
    q, _path = queue
    result = _payload(action.tasks({
        "action": "add",
        "title": "Comprar pão\nPagar conta; Ligar para Ana",
        "priority": 3,
        "persona": "Ana",
    }))
    assert result["status"] == "ok"
    assert result["count"] == 3
    assert [row["title"] for row in result["tasks"]] == [
        "Comprar pão", "Pagar conta", "Ligar para Ana"]
    assert all(row["priority"] == 3 for row in result["tasks"])
    assert all(row["persona"] == "Ana" for row in result["tasks"])
    assert result["line"] == "3 tarefas na fila."

    snapshot = _payload(action.tasks({"action": "status"}))
    assert snapshot["status"] == "ok"
    assert snapshot["line"] == "3 tarefas na fila."
    assert len(snapshot["tasks"]) == 3
    assert snapshot["queued"] == 3

    q.start(result["tasks"][0]["id"])
    snapshot = _payload(action.tasks({"action": "status"}))
    assert snapshot["line"] == (
        "Já está na fila; ainda estou fazendo Comprar pão.")
    assert snapshot["current"]["id"] == result["tasks"][0]["id"]


def test_action_items_kinds_due_and_closing_actions(queue):
    _q, _path = queue
    result = _payload(action.tasks({
        "action": "add",
        "items": [
            "Beber água",
            {"title": "Consulta", "kind": "reminder",
             "due": "2026-11-01T09:00:00", "priority": 8},
        ],
    }))
    assert result["status"] == "ok"
    assert result["count"] == 2
    reminder = result["tasks"][1]
    assert reminder["kind"] == "reminder"
    assert reminder["due"].startswith("2026-11-01T09:00")

    listed = _payload(action.tasks({"action": "list", "status": "queued"}))
    assert listed["count"] == 2

    cancelled = _payload(action.tasks({
        "action": "cancel", "id": result["tasks"][0]["id"]}))
    assert cancelled["status"] == "ok"
    assert cancelled["task"]["status"] == "cancelled"

    done = _payload(action.tasks({
        "action": "done", "id": reminder["id"], "result": "compareceu"}))
    assert done["status"] == "ok"
    assert done["task"]["status"] == "done"
    assert done["task"]["result"] == "compareceu"

    snapshot = _payload(action.tasks({"action": "status"}))
    assert snapshot["tasks"] == []
    assert snapshot["line"] == "A fila está vazia."

    missing = _payload(action.tasks({"action": "cancel", "id": "nope"}))
    assert missing["status"] == "error"
    assert missing["tasks"] == []
