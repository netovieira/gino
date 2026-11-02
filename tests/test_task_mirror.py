"""Espelho visível da execução: log, terminal, aliases e tradução do JSONL."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import task_mirror as action  # noqa: E402
from core import task_mirror  # noqa: E402


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    directory = tmp_path / "logs" / "mirror"
    opens: list[dict] = []

    def fake_open(entry):
        opens.append(dict(entry))
        return True, "Abri o terminal na tela."

    monkeypatch.setattr(task_mirror, "MIRROR_DIR", directory)
    monkeypatch.setattr(task_mirror, "_open_window", fake_open)
    task_mirror.reset()
    yield task_mirror, directory, opens
    task_mirror.reset()


@pytest.fixture
def spawned(tmp_path, monkeypatch):
    """Espelho com janela de verdade: o terminal devolve pid, o kill é falso."""
    directory = tmp_path / "logs" / "mirror"
    opened: list[dict] = []
    closed: list[int] = []
    titled: list[str] = []
    state = {"pid_returned": True, "kill_ok": True}

    def fake_open(params, **_kwargs):
        opened.append(dict(params))
        return {"ok": True,
                "pid": 4100 + len(opened) if state["pid_returned"] else None,
                "message": "Abri o terminal na tela."}

    def fake_kill(pid, log_path=""):
        closed.append(int(pid))
        return bool(state["kill_ok"])

    def fake_title(title):
        titled.append(str(title))
        return False

    from actions import terminal
    monkeypatch.setattr(task_mirror, "MIRROR_DIR", directory)
    monkeypatch.setattr(terminal, "open_terminal", fake_open)
    monkeypatch.setattr(task_mirror, "_terminate_pid", fake_kill)
    monkeypatch.setattr(task_mirror, "_close_by_title", fake_title)
    task_mirror.reset()
    yield task_mirror, directory, opened, closed, titled, state
    task_mirror.reset()


def _windows(m, ident):
    return list(m._entries[ident].get("windows") or ())


def _lines(directory, ident):
    path = directory / f"{task_mirror._safe(ident)}.log"
    return path.read_text(encoding="utf-8").splitlines()


# ── log + janela ────────────────────────────────────────────────────────────

def test_begin_creates_log_and_opens_once(mirror):
    m, directory, opens = mirror
    first = m.begin("tarefa-1", "Ajustar o login", source="queue")
    assert first["ok"] and first["opened"]
    assert len(opens) == 1
    assert opens[0]["title"] == "Ajustar o login"

    text = (directory / "tarefa-1.log").read_text(encoding="utf-8")
    assert "# tarefa: Ajustar o login" in text
    assert "# id: tarefa-1" in text

    second = m.begin("tarefa-1", "Ajustar o login", source="queue")
    assert second["opened"] is False
    assert len(opens) == 1


def test_begin_without_auto_open_keeps_window_closed(mirror):
    m, directory, opens = mirror
    result = m.begin("tarefa-2", "Sem janela", auto_open=False)
    assert result["ok"] and not result["opened"]
    assert opens == []
    assert (directory / "tarefa-2.log").is_file()


def test_append_stamps_and_deduplicates(mirror):
    m, directory, _opens = mirror
    m.begin("tarefa-3", "Rodar testes", auto_open=False)
    m.append("tarefa-3", "$ pytest -q")
    m.append("tarefa-3", "$ pytest -q")     # mesmo conteúdo em < 6 s: uma vez

    lines = _lines(directory, "tarefa-3")
    matches = [line for line in lines if "$ pytest -q" in line]
    assert len(matches) == 1
    assert matches[0].startswith("[")


def test_link_routes_subagent_lines_to_the_queue_log(mirror):
    m, directory, _opens = mirror
    m.begin("fila-1", "Tarefa da fila", auto_open=False)
    m.link("sub-9", "fila-1")

    m.append("sub-9", "ler core/task_queue.py")

    fila = _lines(directory, "fila-1")
    assert any("ler core/task_queue.py" in line for line in fila)
    assert not (directory / "sub-9.log").exists()


def test_finish_writes_closing_line(mirror):
    m, directory, _opens = mirror
    m.begin("tarefa-4", "Terminar", auto_open=False)
    m.finish("tarefa-4", "completed", "147 testes passaram")

    text = "\n".join(_lines(directory, "tarefa-4"))
    assert "── fim: concluído ──" in text
    assert "147 testes passaram" in text
    assert m.status()["mirrors"][0]["done"] is True


# ── a janela morre com a tarefa ─────────────────────────────────────────────

def test_finish_closes_the_window_it_opened(spawned, monkeypatch):
    m, _directory, opened, closed, _titled, _state = spawned
    monkeypatch.setattr(m, "_assistant_name", lambda: "Jino")

    m.begin("tarefa-9", "Ajustar o login", source="queue")
    assert opened[0]["title"] == "Jino — Ajustar o login"
    windows = _windows(m, "tarefa-9")
    assert len(windows) == 1

    m.finish("tarefa-9", "completed", "pronto")

    assert closed == [windows[0]["pid"]]
    assert _windows(m, "tarefa-9") == []
    assert m.status()["mirrors"][0]["done"] is True


@pytest.mark.parametrize("status", ["error", "failed", "cancelled", "stopped"])
def test_error_and_cancellation_close_the_window_too(spawned, status):
    m, _directory, _opened, closed, _titled, _state = spawned
    m.begin("tarefa-10", "Falhar no meio")
    pid = _windows(m, "tarefa-10")[0]["pid"]

    m.finish("tarefa-10", status, "fim anotado")

    assert closed == [pid]
    assert _windows(m, "tarefa-10") == []


def test_already_closed_window_is_tolerated(spawned):
    m, _directory, opened, closed, titled, state = spawned
    state["kill_ok"] = False       # o dono fechou no X: o pid já não existe

    m.begin("tarefa-11", "Janela fechada pelo dono")
    m.finish("tarefa-11", "completed")

    assert closed == [4100 + len(opened)]
    assert titled and titled[0] == opened[0]["title"]
    assert _windows(m, "tarefa-11") == []


def test_window_without_pid_is_closed_by_its_recorded_title(spawned):
    m, _directory, opened, closed, titled, state = spawned
    state["pid_returned"] = False

    m.begin("tarefa-12", "Janela sem pid")
    m.finish("tarefa-12", "completed")

    assert closed == []
    assert titled == [opened[0]["title"]]
    assert _windows(m, "tarefa-12") == []


def test_terminate_pid_of_a_window_already_gone_is_success():
    assert task_mirror._terminate_pid(0) is True
    assert task_mirror._terminate_pid(999999) is True


def test_window_without_a_task_name_still_closes(spawned, monkeypatch):
    m, _directory, opened, closed, _titled, _state = spawned
    monkeypatch.setattr(m, "_assistant_name", lambda: "Jino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_newest_log", lambda: ("", ""))

    m.begin(m.LIVE_ID, m.LIVE_TITLE, source="subagent")

    assert opened[0]["title"] == "Jino — execução em segundo plano"
    m.finish(m.LIVE_ID)
    assert closed and _windows(m, m.LIVE_ID) == []


def test_live_window_of_a_new_task_survives_a_late_finish(spawned, monkeypatch):
    m, _directory, opened, closed, _titled, _state = spawned
    monkeypatch.setattr(m, "_assistant_name", lambda: "Jino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_find_task_meta", lambda *a, **k: None)

    m.start_subagent("sub-A", "Primeira tarefa")
    first = _windows(m, m.LIVE_ID)[0]["pid"]
    m.finish_subagent("sub-A", "completed")
    assert closed == [first]

    m.start_subagent("sub-B", "Segunda tarefa")
    second = _windows(m, m.LIVE_ID)[0]["pid"]
    assert opened[-1]["title"] == "Jino — Segunda tarefa"
    assert second != first

    m.finish_subagent("sub-A", "completed")     # fim tardio da tarefa antiga
    assert closed == [first]
    assert _windows(m, m.LIVE_ID)[0]["pid"] == second

    m.finish_subagent("sub-B", "completed")
    assert closed == [first, second]


def test_window_opened_after_the_task_ended_closes_at_once(spawned, monkeypatch):
    m, _directory, opened, closed, _titled, _state = spawned
    from actions import terminal

    def finish_then_open(params, **_kwargs):
        opened.append(dict(params))
        m.finish("tarefa-13", "completed")   # acabou antes da janela nascer
        return {"ok": True, "pid": 4999, "message": "Abri o terminal na tela."}

    monkeypatch.setattr(terminal, "open_terminal", finish_then_open)

    m.begin("tarefa-13", "Corrida no nascimento")

    assert closed == [4999]
    assert _windows(m, "tarefa-13") == []


def test_show_reopens_a_finished_mirror_and_keeps_it_open(spawned, monkeypatch):
    m, _directory, opened, closed, _titled, _state = spawned
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    m.begin("tarefa-14", "Log encerrado")
    m.finish("tarefa-14", "completed")
    assert closed == [4100 + len(opened)]

    result = m.show(ident="tarefa-14", force=True)

    assert result["opened"]
    assert closed == [4100 + 1]        # a reabertura pedida pelo dono fica


def test_shared_live_window_waits_for_the_last_execution(spawned, monkeypatch):
    m, _directory, _opened, closed, _titled, _state = spawned
    monkeypatch.setattr(m, "_assistant_name", lambda: "Jino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_find_task_meta", lambda *a, **k: None)

    m.start_subagent("sub-A", "Tarefa A")
    m.start_subagent("sub-B", "Tarefa B")       # mesma janela: uma só aberta
    pid = _windows(m, m.LIVE_ID)[0]["pid"]

    m.finish_subagent("sub-A", "completed")
    assert closed == []                          # B ainda está trabalhando

    m.finish_subagent("sub-B", "completed")
    assert closed == [pid]


# ── show ────────────────────────────────────────────────────────────────────

def test_show_opens_the_running_queue_task(mirror, monkeypatch):
    m, directory, opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: {
        "id": "fila-2", "title": "Consertar o microfone"})
    m.begin("fila-2", "Consertar o microfone", source="queue", auto_open=False)
    assert opens == []

    result = m.show()

    assert result["ok"] and result["opened"]
    assert result["title"] == "Consertar o microfone"
    assert len(opens) == 1


def test_show_without_execution_is_honest(mirror, monkeypatch):
    m, _directory, _opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    result = m.show()
    assert result["ok"] is False
    assert "Não há execução" in result["message"]


def test_show_ignores_a_log_that_already_finished(mirror, monkeypatch):
    m, directory, opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "velha.log").write_text(
        "# tarefa: Ontem\n"
        "[10:00:00] $ pytest -q\n"
        "[10:01:00] ── fim: concluído ──\n",
        encoding="utf-8")

    result = m.show()

    assert result["ok"] is False
    assert opens == []


def test_show_reopens_an_unfinished_log_left_on_disk(mirror, monkeypatch):
    m, directory, opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fila-9.log").write_text(
        "# JARVIS · espelho de execução\n"
        "# tarefa: Ajustar o áudio\n"
        "# id: fila-9\n"
        "[10:00:00] $ pytest -q\n",
        encoding="utf-8")

    result = m.show()

    assert result["ok"] and result["opened"]
    assert result["title"] == "Ajustar o áudio"
    assert len(opens) == 1


def test_show_matches_a_task_title_in_the_owner_words(mirror, monkeypatch):
    m, _directory, opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: None)
    monkeypatch.setattr(task_mirror, "_match_task", lambda query: {
        "id": "fila-3", "title": "Instalar o ffmpeg"})
    m.begin("fila-3", "Instalar o ffmpeg", auto_open=False)

    result = m.show(query="me mostra trabalhando no instalar o ffmpeg")

    assert result["ok"] and result["title"] == "Instalar o ffmpeg"
    assert len(opens) == 1


# ── título da janela ────────────────────────────────────────────────────────

def test_window_title_uses_configured_name_and_own_task(mirror, monkeypatch):
    m, _directory, _opens = mirror
    monkeypatch.setattr(m, "_assistant_name", lambda: "Aurora")

    title = m._window_title({"id": "tarefa-6", "title": "Ajustar o login"})

    assert title == "Aurora — Ajustar o login"


def test_open_window_passes_the_task_title_to_the_terminal(monkeypatch):
    monkeypatch.setattr(task_mirror, "_assistant_name", lambda: "Aurora")
    seen: dict = {}
    from actions import terminal

    def fake_open(params, **_kwargs):
        seen.update(params)
        return {"ok": True, "pid": 4321, "message": "Abri o terminal na tela."}

    monkeypatch.setattr(terminal, "open_terminal", fake_open)
    entry = {"id": "tarefa-7", "title": "Consertar o áudio",
             "path": "/tmp/tarefa-7.log", "windows": []}

    ok, _message = task_mirror._open_window(entry)

    assert ok
    assert seen["title"] == "Aurora — Consertar o áudio"
    assert seen["hold"] is False        # o tail morre, o terminal fecha junto
    assert seen["command"].startswith("tail -n +1 -F -- ")
    assert entry["windows"] == [{"pid": 4321, "title": seen["title"],
                                 "marker": seen["command"]}]


def test_live_window_is_named_by_the_subagent_task(mirror, monkeypatch):
    m, directory, opens = mirror
    monkeypatch.setattr(m, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_find_task_meta", lambda *a, **k: None)

    result = m.start_subagent("sub-1", "Revisar o checkout", cwd=None)

    assert result["title"] == "Revisar o checkout"
    assert opens[0]["title"] == "Revisar o checkout"
    assert "# tarefa: Revisar o checkout" in (
        directory / "live.log").read_text(encoding="utf-8")


def test_old_live_log_names_the_window_from_its_last_subagent(mirror,
                                                             monkeypatch):
    m, directory, _opens = mirror
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "live.log"
    path.write_text(
        "# JARVIS · espelho de execução\n"
        "# tarefa: Execuções em segundo plano\n"
        "# id: live\n"
        "[16:49:56] ▸ subagente: Mapear o modelo local (abc123)\n",
        encoding="utf-8")
    monkeypatch.setattr(m, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_newest_log", lambda: ("", ""))

    title = m._window_title({
        "id": "live", "title": "Execuções em segundo plano", "path": str(path)})

    assert title == "Gino — Mapear o modelo local"


def test_long_task_title_is_truncated_but_still_names_the_task(mirror,
                                                               monkeypatch):
    m, _directory, _opens = mirror
    monkeypatch.setattr(m, "_assistant_name", lambda: "Gino")
    task = ("Acordar sozinho para anunciar tarefa quando o assistente "
            "está dormindo e uma conclusão de execução pesada chega")

    title = m._window_title({"id": "tarefa-8", "title": task})

    assert len(title) <= 80
    assert title.startswith("Gino — Acordar sozinho")
    assert title.endswith("…")


def test_window_title_falls_back_to_a_sane_label(mirror, monkeypatch):
    m, _directory, _opens = mirror
    monkeypatch.setattr(m, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_newest_log", lambda: ("", ""))

    title = m._window_title({
        "id": "live", "title": "Execuções em segundo plano",
        "path": "/caminho/que/nao/existe.log"})

    assert title == "Gino — execução em segundo plano"


def test_new_subagent_after_a_finished_live_reopens_a_named_window(
        mirror, monkeypatch):
    m, _directory, opens = mirror
    monkeypatch.setattr(m, "_assistant_name", lambda: "Gino")
    monkeypatch.setattr(m, "_queue_current", lambda: None)
    monkeypatch.setattr(m, "_find_task_meta", lambda *a, **k: None)
    m.begin(m.LIVE_ID, "Primeira tarefa", source="subagent")
    m.finish(m.LIVE_ID)

    result = m.start_subagent("sub-2", "Segunda tarefa", cwd=None)

    assert result["opened"]
    assert opens[-1]["title"] == "Segunda tarefa"


# ── formatação ──────────────────────────────────────────────────────────────

def test_format_tool_for_the_common_commands():
    assert task_mirror.format_tool("Bash", {"command": "pytest -q"}) == "$ pytest -q"
    assert task_mirror.format_tool("Read", {"file_path": "core/qoder.py"}) == \
        "ler core/qoder.py"
    assert task_mirror.format_tool("Edit", {"file_path": "main.py"}) == \
        "editar main.py"
    assert task_mirror.format_tool("Grep", {"pattern": "task_queue"}) == \
        "buscar task_queue"
    assert task_mirror.format_tool("Agent", {"description": "revisar"}) == \
        "subagente: revisar"


def test_transcript_line_shows_commands_and_results():
    tool = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "python -m pytest -q"}}]},
    })
    result = json.dumps({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result",
             "content": [{"type": "text", "text": "147 passed"}]}]},
    })
    assert task_mirror._format_transcript_line(tool) == ["$ python -m pytest -q"]
    assert task_mirror._format_transcript_line(result) == ["  │ 147 passed"]
    assert task_mirror._format_transcript_line("not json") == []


def test_tailer_copies_new_transcript_lines_into_the_log(mirror, monkeypatch):
    m, directory, _opens = mirror
    m.begin("tarefa-5", "Rodar testes", auto_open=False)
    transcript = directory.parent / "agent.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(task_mirror, "_find_task_meta", lambda *a, **k: {
        "transcriptPath": str(transcript)})

    stop = threading.Event()
    thread = threading.Thread(
        target=m._tail_transcript,
        args=("tarefa-5", "sub-1", "", None, stop), daemon=True)
    thread.start()

    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "echo oi"}}]},
        }) + "\n")
        fh.flush()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any("$ echo oi" in line for line in _lines(directory, "tarefa-5")):
            break
        time.sleep(0.05)

    stop.set()
    thread.join(timeout=2.0)
    assert any("$ echo oi" in line for line in _lines(directory, "tarefa-5"))


# ── ação do catálogo ────────────────────────────────────────────────────────

def test_action_show_returns_json(mirror, monkeypatch):
    m, _directory, _opens = mirror
    monkeypatch.setattr(task_mirror, "_queue_current", lambda: {
        "id": "fila-4", "title": "Tarefa"})
    m.begin("fila-4", "Tarefa", auto_open=False)

    payload = json.loads(action.task_mirror_action({"action": "show"}))

    assert payload["status"] == "ok"
    assert payload["title"] == "Tarefa"


def test_action_status_and_contract(mirror):
    payload = json.loads(action.task_mirror_action({"action": "status"}))
    assert payload["status"] == "ok"
    assert action.TOOL["name"] == "task_mirror"
    assert action.TOOL["handler"] is action.task_mirror_action
