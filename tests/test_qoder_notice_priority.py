"""Dono na frente do relatório: o pedido dele nunca espera task_notification.

Laudo de 02/11/2026: a sessão Qoder era uma só e os pedidos do dono entravam na
fila atrás do relatório de conclusão de uma tarefa em segundo plano; a primeira
frase do dono saía 28–59 s depois. A política mora em
``qoder_live/notice_gate.mjs`` (puro) e o sidecar só traduz os eventos do SDK.

Aqui o comportamento é provado rodando os testes do módulo em Node, e a ligação
com o sidecar é conferida no fonte: um ``ask`` ou uma fila do dono cede o
relatório em vez de esperar por ele.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "qoder_live" / "index.mjs"
GATE_TEST = ROOT / "qoder_live" / "notice_gate.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")
def test_notice_gate_behaviour_in_node():
    result = subprocess.run(
        ["node", "--test", str(GATE_TEST)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pass 5" in result.stdout


def test_sidecar_yields_the_report_instead_of_waiting():
    source = SIDECAR.read_text(encoding="utf-8")
    assert 'from "./notice_gate.mjs"' in source

    ask_body = re.search(r"async function ask\(item\) \{(.*?)\n\}", source, re.S)
    assert ask_body is not None
    body = ask_body.group(1)
    assert "noticePending || noticeOpen" in body
    assert "pumpQueue()" in body, (
        "pedido do dono precisa ceder o relatório, não esperar por ele")
    # O antigo bloqueio (esperar o relatório até o teto de 90 s) não volta.
    assert "Date.now() < noticeDeadline" not in body

    pump_body = re.search(r"function pumpQueue\(\) \{(.*?)\n\}", source, re.S)
    assert pump_body is not None
    assert "yieldNoticeToOwner" in pump_body.group(1)
    # A fila do dono não fica bloqueada pela janela do relatório.
    assert "busy || noticePending || !queue.length" not in pump_body.group(1)


def test_sidecar_tracks_notice_announcements_to_avoid_repeats():
    source = SIDECAR.read_text(encoding="utf-8")
    assert "noticeGate.notice(" in source
    assert "noticeGate.takeRequest()" in source
    assert "requestNoticeReport(" in source
    assert "noticeGate.say(" in source
