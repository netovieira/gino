"""Portão de ferramenta real e monitor externo com tag de não confiável."""
from __future__ import annotations

import threading
import time

from core.flows.config import FlowsConfig
from core.flows.executor import Executor, readonly_runner
from core.flows.live import FlowsRuntime


def _config(**kw):
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=True,
                partial_speech=False, executor_enabled=False)
    base.update(kw)
    return FlowsConfig(**base)


def test_readonly_runner_produces_a_real_fact():
    result = Executor(runner=readonly_runner, enabled=True).run("k1", "hora")
    assert result.ok and result.facts
    fact = result.facts[0]
    assert fact["spoken"].startswith("são ")
    # Fato real, não simulado: só então a boca pode afirmar.
    assert fact["assertion"] is True


def test_readonly_runner_refuses_mutating_tool():
    result = Executor(runner=readonly_runner, enabled=True).run(
        "k1", "restart_assistant", {"action": "assistant"})
    assert result.ok is False
    assert result.facts and result.facts[0]["assertion"] is False


def test_unknown_tool_never_reaches_the_runner():
    calls = []
    ex = Executor(runner=lambda tool, args: calls.append(tool) or {"ok": True},
                  enabled=True)
    result = ex.run("k1", "ferramenta_inexistente")
    assert result.ok is False
    assert calls == []


def test_confirmation_gate_never_executes_even_with_runner():
    calls = []
    ex = Executor(runner=lambda tool, args: calls.append(tool) or {"ok": True},
                  enabled=True)
    result = ex.run("k1", "restart_assistant", {"action": "assistant"},
                    requires_confirm=True)
    assert result.pending_confirm is True
    assert calls == []                      # lei (b): só pede a frase
    assert result.facts[0]["assertion"] is False
    assert "confirmo reiniciar o assistente" in result.facts[0]["pending_confirm"]


def test_live_turn_sends_confirm_action_to_the_gate():
    calls = []
    logs = []
    runtime = FlowsRuntime(
        _config(), speak=lambda _t: None, log=logs.append,
        executor_runner=lambda tool, args: calls.append(tool) or {"ok": True},
        start_expiry_thread=False)
    # "desliga o computador" continua no portão (dono, 10/11/2026): só o
    # reinício do próprio processo perdeu a janela.
    runtime.start_turn("t1", "desliga o computador")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any("portão de ferramenta" in line for line in logs):
            break
        time.sleep(0.01)
    assert calls == []                      # nada executou
    assert any("nada executado" in line for line in logs)
    runtime.finish("t1")


def test_readonly_turn_does_not_touch_the_gate():
    calls = []
    logs = []
    runtime = FlowsRuntime(
        _config(), speak=lambda _t: None, log=logs.append,
        executor_runner=lambda tool, args: calls.append(tool) or {"ok": True},
        start_expiry_thread=False)
    plan = runtime.start_turn("t1", "e aí, tudo bem?")
    assert plan is not None and not any(a.requires_confirm
                                        for a in plan.decision.actions)
    time.sleep(0.05)
    assert calls == []
    assert not any("portão de ferramenta" in line for line in logs)
    runtime.finish("t1")


def test_monitor_event_is_untrusted_and_never_becomes_task():
    runtime = FlowsRuntime(_config(), speak=lambda _t: None,
                           start_expiry_thread=False)
    result = runtime.monitor_event("email_otp:email",
                                   "código de verificação recebido")
    assert result["trust"] == "untrusted"
    assert result["decision"] == "blocked"
    assert result["task"] is None
