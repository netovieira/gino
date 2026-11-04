"""Isolamento global da fila persistente durante os testes.

`memory/tasks.json` é a fila REAL do dono. Testes que exercitam o caminho de
preempt/carryover (ex.: `test_stale_answer_order`) enfileiravam perguntas de
teste nesse arquivo — foi assim que «não precisa auto-play» e «como fica a
matrícula por dispositivo?» viraram dezenas de itens e o `_task_watch` passou a
responder pergunta velha depois do reinício (laudo de 03/11/2026).

O fixture abaixo redireciona a fila para um diretório temporário em TODO teste;
quem precisar de um caminho próprio continua monkeypatchando `TASKS_PATH`.
"""
from __future__ import annotations

import pytest

from core import task_queue


@pytest.fixture(autouse=True)
def _isolated_task_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASKS_PATH", tmp_path / "tasks.json")
    yield
