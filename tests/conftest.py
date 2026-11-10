"""Isolamento global da fila persistente durante os testes.

`memory/tasks.json` é a fila REAL do dono. Testes que exercitam o caminho de
preempt/carryover (ex.: `test_stale_answer_order`) enfileiravam perguntas de
teste nesse arquivo — foi assim que «não precisa auto-play» e «como fica a
matrícula por dispositivo?» viraram dezenas de itens e o `_task_watch` passou a
responder pergunta velha depois do reinício (laudo de 03/11/2026).

O fixture abaixo redireciona a fila para um diretório temporário em TODO teste;
quem precisar de um caminho próprio continua monkeypatchando `TASKS_PATH`.

O segundo desliga a checagem de voz no Fish Audio: desde 09/11/2026 o cadastro
de persona pergunta ao serviço se o `voice_id` existe antes de gravar, e sem
isso cada teste de persona faria uma ida real à API com a chave do dono. Quem
testa a validação monkeypatcha `core.personas.fish_voice_lookup`.

O terceiro isola o **cofre do agendador**: desde 10/11/2026 o laço de hora
(``main.py:_task_watch_step`` → ``_scheduler_watch_step``) chama
``scheduler.tick()`` em toda passada. Sem isto, cada teste que exercita o
passo leria ``~/Jarvis/Memoria`` do dono — e gravaria ``disparou_em`` nas notas
reais dele. Quem precisa do cofre próprio continua monkeypatchando
``obsidian.VAULT`` por cima (é o que ``tests/test_agendador_gatilho.py`` faz).
"""
from __future__ import annotations

import pytest

from core import obsidian
from core import personas as persona_store
from core import scheduler
from core import task_queue


@pytest.fixture(autouse=True)
def _isolated_task_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(task_queue, "TASKS_PATH", tmp_path / "tasks.json")
    yield


@pytest.fixture(autouse=True)
def _isolated_scheduler_vault(tmp_path, monkeypatch):
    """Nenhum teste agenda, lê ou marca disparo no cofre real do dono."""
    monkeypatch.setattr(obsidian, "VAULT", tmp_path / "cofre")
    monkeypatch.setattr(scheduler, "REGISTRO_SCRIPTS",
                        tmp_path / "config" / "schedule_scripts.json")
    for nome, valor in (("_cache", {"assinatura": None, "linhas": {}}),
                        ("_scripts_cache", {"assinatura": None, "lista": []}),
                        ("_PORTAS", {}), ("_pendente_aviso", set()),
                        ("_pedidos_aprovacao", []), ("_ja_acordei", False)):
        monkeypatch.setattr(scheduler, nome, valor)
    yield


@pytest.fixture(autouse=True)
def _no_fish_voice_call(monkeypatch):
    """Sem chave a validação vira nota — e nenhum teste disca o Fish Audio."""
    monkeypatch.setattr(persona_store, "_fish_api_key", lambda: "")
    yield
