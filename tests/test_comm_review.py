"""Revisão de comunicação: janela de 6 h, offset e pendência na fila."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pytest

from core import activity, comm_review, task_queue


def _line(ts: float, source: str, text: str) -> str:
    stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    return f"[{stamp}] {source}: {text}\n"


def _flagged(line: str, **meta) -> str:
    return activity.with_meta(line.rstrip("\n"),
                              activity.make_meta(**meta)) + "\n"


def _write(path: Path, rows: list[str]) -> None:
    path.write_text("".join(rows), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    activity = tmp_path / "activity.log"
    report = tmp_path / "comm-review.md"
    state = tmp_path / "comm_review_state.json"
    comm_review.reset(activity_path=activity, report_path=report,
                      state_path=state)
    monkeypatch.setattr(task_queue, "TASKS_PATH",
                        tmp_path / "memory" / "tasks.json")
    monkeypatch.setattr(
        comm_review, "_git_cache",
        (time.time(), {"commit": "deadbeefcafebabe", "branch": "main",
                       "dirty": False}))
    task_queue.reset()
    yield activity, report, state
    comm_review.reset()
    task_queue.reset()


def test_first_run_reviews_only_the_last_six_hours(env):
    activity, report, state = env
    now = time.time()
    _write(activity, [
        _line(now - 7 * 3600, "YOU", "fora da janela"),
        _line(now - 2 * 3600, "YOU", "você consegue?"),
        _line(now - 2 * 3600 + 30, "Gino", "consigo."),
        _line(now - 3600, "SYS", "interrompido por voz — «teste»"),
    ])

    calls = []
    result = comm_review.review_once(
        now=now, enqueue=lambda p: calls.append(p) or True)

    assert result["stats"]["lines"] == 3
    assert result["stats"]["user"] == 1
    assert result["stats"]["assistant"] == 1
    assert result["stats"]["interruptions"] == 1
    assert calls == []                     # nada a anunciar nesta janela
    text = report.read_text(encoding="utf-8")
    assert "# Revisão de comunicação" in text
    assert "falas do dono: 1" in text
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["offset"] == activity.stat().st_size
    assert saved["last_run"] == pytest.approx(now)


def test_waits_six_hours_and_reads_only_the_new_slice(env):
    activity, report, _state = env
    now = time.time()
    _write(activity, [
        _line(now - 60, "YOU", "primeira"),
        _line(now - 30, "Gino", "ok"),
    ])
    first = comm_review.review_once(now=now, force=True,
                                    enqueue=lambda p: True)
    assert first["stats"]["user"] == 1

    with activity.open("a", encoding="utf-8") as f:
        f.write(_line(now + 6 * 3600 - 10, "YOU", "segunda"))

    assert comm_review.review_once(now=now + 60,
                                   enqueue=lambda p: True) is None
    second = comm_review.review_once(now=now + 6 * 3600 + 10,
                                     enqueue=lambda p: True)
    assert second["stats"]["user"] == 1
    assert second["stats"]["assistant"] == 0

    text = report.read_text(encoding="utf-8")
    assert text.count("falas do dono:") == 2
    assert text.count("## ") == 2


def test_finding_leaves_one_event_in_the_app_queue(env):
    activity, _report, _state = env
    now = time.time()
    _write(activity, [
        _line(now - 1000, "YOU", "você consegue?"),
        _line(now - 900, "SYS", "interrompido por voz — «a»"),
        _line(now - 800, "SYS", "interrompido por voz — «b»"),
        _line(now - 700, "SYS", "interrompido por voz — «c»"),
    ])

    result = comm_review.review_once(now=now, force=True)
    assert result["findings"]

    rows = task_queue.list_tasks("open")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "event"
    assert row["payload"]["source"] == "comm_review"
    assert "revisão de comunicação" in row["title"].lower()
    assert row["payload"]["report"].endswith("comm-review.md")

    # Nova janela com outra pendência não duplica o anúncio na fila.
    with activity.open("a", encoding="utf-8") as f:
        f.write(_line(now + 6 * 3600 - 100, "YOU", "de novo?"))
    comm_review.review_once(now=now + 6 * 3600)
    assert len(task_queue.list_tasks("open")) == 1


def test_unwritable_queue_keeps_pending_and_retries(env):
    activity, _report, state = env
    now = time.time()
    _write(activity, [_line(now - 1000, "YOU", "pergunta?")])

    calls = []

    def failing(payload):
        calls.append(payload)
        return False

    result = comm_review.review_once(now=now, force=True, enqueue=failing)
    assert result["findings"]
    assert len(calls) == 1
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["pending"]["announcement"]

    retried = []
    assert comm_review.review_once(
        now=now + 60, enqueue=lambda p: retried.append(p) or True) is None
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["pending"] is None
    assert retried == calls


def test_shrunk_log_restarts_offset_instead_of_breaking(env):
    activity, _report, _state = env
    now = time.time()
    _write(activity, [
        _line(now - 2 * 3600, "YOU", "antiga"),
        _line(now - 3600, "Gino", "ok"),
    ])
    comm_review.review_once(now=now, force=True, enqueue=lambda p: True)

    # rotação/reinício que recria o log menor que o offset salvo
    _write(activity, [_line(now + 6 * 3600, "YOU", "nova")])
    result = comm_review.review_once(
        now=now + 6 * 3600 + 1, enqueue=lambda p: True)

    assert result["stats"]["user"] == 1
    assert result["stats"]["assistant"] == 0


def test_quiet_window_reports_without_announcing(env):
    activity, report, _state = env
    now = time.time()
    _write(activity, [_line(now - 1000, "SYS", "só sistema")])

    calls = []
    comm_review.review_once(now=now, force=True,
                            enqueue=lambda p: calls.append(p) or True)

    assert calls == []
    assert "Sem sinal claro de atrito" in report.read_text(encoding="utf-8")


def test_announcement_waits_for_the_owner_window(env):
    activity, _report, state = env
    now = time.time()
    _write(activity, [_line(now - 1000, "YOU", "pergunta?")])

    calls = []
    result = comm_review.review_once(
        now=now, force=True, enqueue=lambda p: calls.append(p) or True,
        ready=lambda: False)

    assert result["findings"]
    assert calls == []                       # dormindo/mudo: só grava
    assert json.loads(state.read_text("utf-8"))["pending"]["announcement"]

    comm_review.review_once(
        now=now + 60, enqueue=lambda p: calls.append(p) or True,
        ready=lambda: True)

    assert len(calls) == 1
    assert json.loads(state.read_text("utf-8"))["pending"] is None


def test_report_failure_does_not_advance_the_window(env):
    activity, report, state = env
    now = time.time()
    report.mkdir()                  # abrir como arquivo vai falhar
    _write(activity, [_line(now - 1000, "YOU", "pergunta?")])

    assert comm_review.review_once(now=now, force=True,
                                   enqueue=lambda p: True) is None
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_run"] == pytest.approx(now - 6 * 3600)
    assert saved["offset"] == 0


def test_owner_questions_ignore_text_series_and_unknown_speakers(env):
    activity_path, _report, _state = env
    now = time.time()
    _write(activity_path, [
        _flagged(_line(now - 500, "YOU", "[FALANTE: Anthero] você consegue?"),
                 turn=1, chan="voz"),
        _flagged(_line(now - 480, "YOU", "pergunta digitada?"),
                 turn=2, chan="texto"),
        _flagged(_line(now - 460, "YOU", "[FALANTE: desconhecido] quem é você?"),
                 turn=3, chan="voz"),
        _flagged(_line(now - 440, "YOU", "[FALANTE: TV] você vai morrer?"),
                 turn=4, chan="voz"),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)

    assert result["stats"]["user"] == 1
    assert result["stats"]["unanswered_questions"] == 1
    assert result["stats"]["unanswered_turns"] == 1


def test_latency_from_audio_suffix_is_reported_as_measured(env):
    activity_path, report, _state = env
    now = time.time()
    _write(activity_path, [
        _flagged(_line(now - 900, "YOU", "primeira?"),
                 turn=1, chan="voz"),
        _flagged(_line(now - 800, "Gino", "resposta um."),
                 turn=1, chan="voz", requested=True, generated=True,
                 latency_ms=1000),
        _flagged(_line(now - 700, "Gino", "resposta dois."),
                 turn=2, chan="voz", requested=True, generated=True,
                 latency_ms=3000),
        _flagged(_line(now - 600, "Gino", "resposta três."),
                 turn=3, chan="voz", requested=True, generated=True,
                 latency_ms=8000),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)
    latency = result["stats"]["latency_ms"]

    assert latency == {"samples": 3, "median": 3000, "p90": 8000, "max": 8000}
    assert result["stats"]["late_first_sentences"] == 2
    text = report.read_text(encoding="utf-8")
    assert "mediana 3.0s" in text and "p90 8.0s" in text


def test_incidents_are_split_before_and_after_the_restart(env, monkeypatch):
    activity_path, report, _state = env
    now = time.time()
    monkeypatch.setattr(comm_review, "_PROCESS_START", now - 3600)
    _write(activity_path, [
        _line(now - 7200, "YOU", "pergunta antiga?"),
        _line(now - 7000, "SYS", "interrompido por voz — «antiga»"),
        _line(now - 1000, "YOU", "pergunta nova?"),
        _line(now - 900, "SYS", "interrompido por voz — «nova»"),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)
    split = result["stats"]["restart"]

    assert split["pre"]["unanswered_questions"] == 1
    assert split["post"]["unanswered_questions"] == 1
    assert split["pre"]["interruptions"] == 1
    assert split["post"]["interruptions"] == 1
    assert "score_pre" in result["stats"] and "score_post" in result["stats"]
    text = report.read_text(encoding="utf-8")
    assert "Incidentes por processo: pós-restart 2 · pré-restart 2" in text
    assert "Nota:" in text and "pós-restart" in text and "pré-restart" in text
    assert "deadbeefcafe" in text and "árvore limpa" in text


def test_silent_task_completion_gets_its_own_finding(env):
    activity_path, report, _state = env
    now = time.time()

    def iso(ts: float) -> str:
        return datetime.fromtimestamp(ts).astimezone().isoformat()

    task_queue.TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    task_queue.TASKS_PATH.write_text(json.dumps({"version": 1, "tasks": [
        {"id": "muda", "title": "tarefa muda", "kind": "task",
         "status": "done", "created": iso(now - 1200),
         "started": iso(now - 1000), "finished": iso(now - 700),
         "payload": {}},
        {"id": "anunciada", "title": "tarefa anunciada", "kind": "task",
         "status": "done", "created": iso(now - 1200),
         "started": iso(now - 1100), "finished": iso(now - 900),
         "payload": {}},
    ]}), encoding="utf-8")
    _write(activity_path, [
        _flagged(_line(now - 800, "Gino", "Terminei a tarefa anunciada."),
                 turn=1, chan="voz", requested=True, generated=True),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)

    assert result["stats"]["completed_tasks"] == 2
    assert result["stats"]["silent_completions"] == 1
    assert any("tarefas concluídas" in f["detail"].lower()
               for f in result["findings"])
    text = report.read_text(encoding="utf-8")
    assert "Tarefas concluídas: 2" in text
    assert "mudas (sem anúncio em até 3 min): 1" in text


def test_humanization_counts_long_spoken_replies_only(env):
    activity_path, _report, _state = env
    now = time.time()
    forty = " ".join(["palavra"] * 40)
    _write(activity_path, [
        _flagged(_line(now - 600, "Gino", forty),
                 turn=1, chan="voz", requested=True, generated=True),
        _flagged(_line(now - 500, "Gino", "Um. Dois. Três."),
                 turn=2, chan="voz", requested=True, generated=True),
        _flagged(_line(now - 400, "Gino", forty),
                 turn=3, chan="texto", requested=True, generated=True),
        _flagged(_line(now - 300, "Gino", forty),
                 turn=4, chan="voz", requested=True, generated=True,
                 origin="narração"),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)

    assert result["stats"]["human_long_replies"] == 2


def test_rates_use_the_window_hours_instead_of_a_raw_count(env):
    activity_path, _report, state = env
    now = time.time()
    state.write_text(json.dumps({
        "last_run": now - 2 * 3600, "offset": 0, "pending": None,
    }), encoding="utf-8")
    _write(activity_path, [
        _line(now - 6000, "YOU", "primeira pergunta?"),
        _line(now - 5000, "YOU", "segunda pergunta?"),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)

    assert result["stats"]["window_hours"] == pytest.approx(2.0, abs=0.01)
    assert result["stats"]["unanswered_questions"] == 2
    assert result["stats"]["per_hour"]["unanswered_questions"] == pytest.approx(1.0)


def test_start_installs_one_daemon_thread(env):
    comm_review.start(interval=60, poll=3600)
    thread = comm_review._thread
    assert thread is not None and thread.is_alive()

    comm_review.start(interval=60, poll=3600)
    assert comm_review._thread is thread

    comm_review.stop()
    assert not thread.is_alive()


def test_sanitized_and_cut_flags_are_counted_instead_of_guessed(env):
    """A revisão lê a flag da esteira de fala: vazamento medido, não adivinha."""
    activity_path, report, _state = env
    now = time.time()
    _write(activity_path, [
        _flagged(_line(now - 900, "Gino", "Pronto."),
                 turn=1, chan="voz", requested=True, generated=True,
                 sanitized="tool+path"),
        _flagged(_line(now - 800, "Gino", "Feito."),
                 turn=2, chan="voz", requested=True, generated=False,
                 cut=True),
        _flagged(_line(now - 700, "Gino", "Certo."),
                 turn=3, chan="voz", requested=True, generated=True,
                 sanitized="lang"),
    ])

    result = comm_review.review_once(now=now, force=True,
                                     enqueue=lambda p: True)

    assert result["stats"]["sanitized_speech"] == 2
    assert result["stats"]["cut_speech"] == 1
    assert result["stats"]["sanitized_kinds"] == {"tool": 1, "path": 1, "lang": 1}
    assert any("sanitização antes do TTS" in f["detail"]
               for f in result["findings"])
    text = report.read_text(encoding="utf-8")
    assert "Vazamentos na fala: 2 sanitizados" in text
    assert "cortes no meio: 1" in text
