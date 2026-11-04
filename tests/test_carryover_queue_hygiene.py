"""Higiene da fila de carryover e deswedging do `running` (03/11/2026).

O laudo que abriu esta correção achou 69 itens `preempt-carryover` abertos,
sendo 45 «não precisa auto-play» e 23 «como fica a matrícula por dispositivo?»,
mais um `running` preso das 21:27. O contrato aqui:

  (a) pergunta nova SUPERSEDE as abertas da mesma sessão, sem perder as não
      respondidas (lista curta no payload);
  (b) dedupe por texto normalizado + sessão e TTL de 90 s de assunto
      abandonado;
  (c) `_task_watch` nunca responde item de outra sessão, item velho, nem item
      anterior à última fala do dono;
  (d) item `running` com pid morto / boot antigo / idade absurda volta a
      `queued`, inclusive no boot (`recover`).
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from unittest import mock

import pytest

import main
from core import task_queue


@pytest.fixture
def queue(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "tasks.json"
    monkeypatch.setattr(task_queue, "TASKS_PATH", path)
    task_queue.reset()
    yield task_queue, path
    task_queue.reset()


def _iso_ago(seconds: float) -> str:
    return (datetime.now().astimezone()
            - timedelta(seconds=seconds)).isoformat()


def _add_carryover(q, question, *, age_s=1.0, session=None, unanswered=None):
    payload = {
        "origin": "preempt-carryover",
        "turn": 1,
        "question": question,
        "reason": "teste",
        "session": (main._PROCESS_STARTED_WALL if session is None else session),
        "asked_at": _iso_ago(age_s),
        "last_speech": question,
        "unanswered": list(unanswered or [question]),
    }
    return q.add(f"Responder à pergunta do dono que ficou sem resposta: "
                 f"«{question[:160]}»", payload=payload)


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, line):
        self.logs.append(str(line))


def _jarvis():
    obj = object.__new__(main.JarvisLive)
    obj._turn_owner_text = {}
    obj._speech_audit_lock = threading.RLock()
    obj._carryover_questions = {}
    obj._owner_speech_wall = 0.0
    obj._is_speaking = False
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_live_ready = lambda: True
    obj.ui = _UI()
    obj.instructions = []
    obj._qoder_turn = lambda instruction, **kw: obj.instructions.append(
        (instruction, kw))
    return obj


# ── (a) supersessão no enfileiramento ──────────────────────────────────────

def test_new_question_supersedes_open_carryover_of_the_same_session(queue):
    q, _path = queue
    obj = _jarvis()
    obj._turn_owner_text = {1: "não precisa auto-play"}
    assert obj._carryover_question(1, "não precisa auto-play", "preempt") is True
    first = q.list_tasks("open")
    assert len(first) == 1
    assert first[0]["payload"]["unanswered"] == ["não precisa auto-play"]

    obj._turn_owner_text = {2: "como fica a matrícula por dispositivo?"}
    assert obj._carryover_question(
        2, "como fica a matrícula por dispositivo?", "preempt") is True

    open_rows = q.list_tasks("open")
    assert len(open_rows) == 1, "pergunta nova não pode conviver com a velha"
    fresh = open_rows[0]
    assert fresh["title"].endswith("«como fica a matrícula por dispositivo?»")
    assert fresh["payload"]["question"] == "como fica a matrícula por dispositivo?"
    assert fresh["payload"]["last_speech"] == (
        "como fica a matrícula por dispositivo?")
    assert fresh["payload"]["unanswered"] == [
        "não precisa auto-play", "como fica a matrícula por dispositivo?"]

    cancelled = q.list_tasks("cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["payload"]["question"] == "não precisa auto-play"
    assert cancelled[0]["payload"]["_cancel_reason"] == "superseded"


def test_same_text_is_not_enqueued_twice_in_the_same_session(queue):
    q, _path = queue
    obj = _jarvis()
    obj._turn_owner_text = {1: "não precisa auto-play"}
    assert obj._carryover_question(1, "não precisa auto-play") is True
    only = q.list_tasks("open")
    assert len(only) == 1

    obj._turn_owner_text = {2: "NÃO precisa auto-play?"}
    assert obj._carryover_question(2, "NÃO precisa auto-play?") is False
    assert [row["id"] for row in q.list_tasks("open")] == [only[0]["id"]]


def test_expired_abandoned_carryover_is_cancelled_before_enqueueing(queue):
    q, _path = queue
    old = _add_carryover(q, "não precisa auto-play", age_s=200)
    obj = _jarvis()
    obj._turn_owner_text = {1: "como fica a matrícula por dispositivo?"}
    assert obj._carryover_question(
        1, "como fica a matrícula por dispositivo?") is True

    cancelled = {row["id"]: row for row in q.list_tasks("cancelled")}
    assert old["id"] in cancelled
    assert cancelled[old["id"]]["payload"]["_cancel_reason"] == "expirado"
    open_rows = q.list_tasks("open")
    assert len(open_rows) == 1
    assert open_rows[0]["payload"]["question"] == (
        "como fica a matrícula por dispositivo?")


# ── (b/c) guarda de saída no _task_watch ───────────────────────────────────

def test_old_session_carryover_is_cancelled_not_answered(queue):
    q, _path = queue
    _add_carryover(q, "não precisa auto-play", age_s=2,
                   session=main._PROCESS_STARTED_WALL - 5000)
    obj = _jarvis()
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        assert obj._task_watch_step() is False
    assert obj.instructions == []
    rows = q.list_tasks()
    assert [row["status"] for row in rows] == ["cancelled"]


def test_carryover_without_session_is_echo_of_the_old_process(queue):
    q, _path = queue
    row = q.add("Responder à pergunta antiga", payload={
        "origin": "preempt-carryover", "turn": 7,
        "question": "não precisa auto-play"})
    obj = _jarvis()
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        assert obj._task_watch_step() is False
    assert obj.instructions == []
    assert q.list_tasks("cancelled")[0]["id"] == row["id"]


def test_expired_carryover_is_not_answered(queue):
    q, _path = queue
    _add_carryover(q, "como fica a matrícula por dispositivo?", age_s=200)
    obj = _jarvis()
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        assert obj._task_watch_step() is False
    assert obj.instructions == []
    assert q.list_tasks("open") == []
    assert q.list_tasks("cancelled")


def test_owner_spoke_after_the_item_cancels_the_answer(queue):
    q, _path = queue
    _add_carryover(q, "não precisa auto-play", age_s=5)
    obj = _jarvis()
    obj._owner_speech_wall = datetime.now().timestamp()
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        assert obj._task_watch_step() is False
    assert obj.instructions == []
    assert q.list_tasks("cancelled")


def test_fresh_carryover_is_answered_citing_the_payload_question(queue):
    q, _path = queue
    row = _add_carryover(q, "a tela fica sempre acesa?", age_s=5)
    obj = _jarvis()
    # Fala do dono de antes do item (a que causou o preempt) não invalida.
    obj._owner_speech_wall = datetime.now().timestamp() - 30
    with mock.patch.object(main, "_task_mirror", mock.Mock()):
        assert obj._task_watch_step() is True
    assert len(obj.instructions) == 1
    instruction, kwargs = obj.instructions[0]
    assert "a tela fica sempre acesa?" in instruction
    assert "[TAREFA DA FILA]" in instruction
    assert kwargs.get("task_id") == row["id"]
    assert q.list_tasks("running")[0]["id"] == row["id"]


# ── (d) deswedging do running ──────────────────────────────────────────────

def _row(**overrides):
    row = {
        "id": "run-0001", "title": "tarefa presa", "kind": "task",
        "status": "running", "priority": 0, "due": None,
        "created": _iso_ago(120), "started": _iso_ago(120),
        "finished": None, "persona": None, "project": None,
        "payload": {}, "result": "", "error": "",
    }
    row.update(overrides)
    return row


def test_running_with_dead_pid_goes_back_to_queued(queue):
    q, _path = queue
    q.save([_row(payload={"_runner": {"pid": 999999999,
                                      "boot_id": task_queue._boot_id()}})])
    rows = q.load()
    assert rows[0]["status"] == "queued"
    assert rows[0]["started"] is None
    assert "_runner" not in rows[0]["payload"]
    assert "pid morto" in rows[0]["payload"]["_recovered"]["reason"]


def test_running_from_previous_boot_goes_back_to_queued(queue):
    q, _path = queue
    q.save([_row(payload={"_runner": {"pid": os.getpid(),
                                      "boot_id": "boot-antigo"}})])
    rows = q.load()
    assert rows[0]["status"] == "queued"
    assert "boot antigo" in rows[0]["payload"]["_recovered"]["reason"]


def test_running_older_than_max_age_goes_back_to_queued_on_boot(queue):
    q, _path = queue
    old = _row(started=(datetime.now().astimezone() - timedelta(
        seconds=task_queue.RUNNING_MAX_AGE_S + 60)).isoformat())
    q.save([old])
    assert q.recover()[0]["status"] == "queued"
    # e o tick já entrega o item destravado
    due = q.tick()
    assert [row["id"] for row in due] == ["run-0001"]
    assert due[0]["status"] == "queued"


def test_fresh_running_with_live_owner_stays_running(queue):
    q, _path = queue
    added = q.add("tarefa de verdade")
    started = q.start(added["id"])
    assert started["status"] == "running"
    runner = started["payload"]["_runner"]
    assert runner["pid"] == os.getpid()
    assert q.load()[0]["status"] == "running"


# ── helpers de cancelamento/purga ──────────────────────────────────────────

def test_cancel_open_by_origin_filters_session_and_keeps_terminal(queue):
    q, _path = queue
    now = _iso_ago(1)
    same = q.add("mesma sessão", payload={
        "origin": "x", "session": 10.0, "asked_at": now})
    other_session = q.add("outra sessão", payload={
        "origin": "x", "session": 20.0, "asked_at": now})
    other_origin = q.add("outra origem", payload={
        "origin": "y", "session": 10.0, "asked_at": now})
    closed = q.add("já fechada", payload={
        "origin": "x", "session": 10.0, "asked_at": now})
    q.finish(closed["id"])

    cancelled = q.cancel_open_by_origin("x", session=10.0, reason="teste")
    assert [row["id"] for row in cancelled] == [same["id"]]
    status = {row["id"]: row["status"] for row in q.list_tasks()}
    assert status[same["id"]] == "cancelled"
    assert status[other_session["id"]] == "queued"
    assert status[other_origin["id"]] == "queued"
    assert status[closed["id"]] == "done"


def test_purge_by_origin_removes_junk_but_keeps_real_tasks(queue):
    q, _path = queue
    junk_done = q.add("eco1", payload={"origin": "junk"})
    q.finish(junk_done["id"])
    junk_open = q.add("eco2", payload={"origin": "junk"})
    real_done = q.add("Avisar o dono pelo WhatsApp quando terminar")
    q.finish(real_done["id"])
    real_open = q.add("Gravar jarvis-trabalho como conta")

    assert q.purge_by_origin("junk") == 2
    ids = {row["id"] for row in q.list_tasks()}
    assert ids == {real_done["id"], real_open["id"]}
    assert {row["status"] for row in q.list_tasks()} == {"done", "queued"}
