"""FASE3-TASK4: testes de execução concreta obrigatória com prova material.

Verifica que:
  1. Ações sem prova material (recibo HMAC + verificação externa) são marcadas
     como UNVERIFIED pelo executor.
  2. O guard rejeita afirmações de resultado sem prova material.
  3. O orquestrador emite evento TURN_UNVERIFIED para turnos com ações unverified.
  4. O painel GUIA exibe marcadores UNVERIFIED.
  5. Modo observador (primeiras 2 semanas) loga mas não bloqueia.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

# ── imports do projeto ───────────────────────────────────────────────────────
from core.flows.executor import (
    Executor, ExecutorResult,
    _has_material_proof, _fact_has_proof, _tier_has_side_effects,
    _get_action_tier, _is_observer_mode, _log_unverified,
    FASE3_TASK4_START, OBSERVER_MODE_WEEKS,
)
from core.flows.guard import guard
from core.flows.orchestrator import Orchestrator
from core.flows.config import FlowsConfig
from core.flows.router import Decision
from core.guide_panel import unverified_markers


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_activity_log(tmp_path):
    """Cria um activity log temporário e faz o executor usá-lo."""
    log_path = tmp_path / "activity.log"
    with patch("core.flows.executor._ACTIVITY_LOG_PATH", log_path):
        yield log_path


@pytest.fixture
def executor_with_runner():
    """Retorna um executor com runner injetável."""
    def make(runner):
        return Executor(runner=runner, catalog={"media_control", "open_app",
                                                 "hora", "runtime_status",
                                                 "send_message", "web_search"})
    return make


# ── 1. Prova material: detecção ──────────────────────────────────────────────

class TestMaterialProofDetection:
    """Verifica se o executor detecta corretamente a presença/ausência de prova."""

    def test_fact_without_proof(self):
        """Fato sem call_id/receipt_hmac/verified não tem prova material."""
        fact = {"ok": True, "assertion": True, "spoken": "ação concluída"}
        assert not _fact_has_proof(fact)

    def test_fact_with_full_proof(self):
        """Fato com call_id + receipt_hmac + verified=True tem prova material."""
        fact = {
            "ok": True, "assertion": True,
            "call_id": "abc-123",
            "receipt_hmac": "sha256:deadbeef",
            "verified": True,
        }
        assert _fact_has_proof(fact)

    def test_fact_with_partial_proof(self):
        """Fato com apenas call_id não tem prova material completa."""
        fact = {"ok": True, "call_id": "abc-123"}
        assert not _fact_has_proof(fact)

    def test_fact_verified_false(self):
        """Fato com verified=False não tem prova material válida."""
        fact = {
            "call_id": "abc-123",
            "receipt_hmac": "sha256:deadbeef",
            "verified": False,
        }
        assert not _fact_has_proof(fact)

    def test_has_material_proof_dict(self):
        """_has_material_proof funciona com dict."""
        payload = {"call_id": "x", "receipt_hmac": "y", "verified": True}
        assert _has_material_proof(payload)

    def test_has_material_proof_list(self):
        """_has_material_proof funciona com lista de fatos."""
        payload = [
            {"call_id": "x", "receipt_hmac": "y", "verified": True},
            {"call_id": "z", "receipt_hmac": "w", "verified": True},
        ]
        assert _has_material_proof(payload)

    def test_has_material_proof_list_partial(self):
        """Lista com um fato sem prova retorna False."""
        payload = [
            {"call_id": "x", "receipt_hmac": "y", "verified": True},
            {"ok": True},  # sem prova
        ]
        assert not _has_material_proof(payload)


# ── 2. Risk tier e efeito colateral ──────────────────────────────────────────

class TestRiskTierSideEffects:
    """Verifica se o tier determina corretamente ações com efeito colateral."""

    def test_safe_has_no_side_effects(self):
        """Tier 'safe' não tem efeito colateral."""
        assert not _tier_has_side_effects("safe")

    def test_low_has_side_effects(self):
        """Tier 'low' tem efeito colateral."""
        assert _tier_has_side_effects("low")

    def test_medium_has_side_effects(self):
        """Tier 'medium' tem efeito colateral."""
        assert _tier_has_side_effects("medium")

    def test_high_has_side_effects(self):
        """Tier 'high' tem efeito colateral."""
        assert _tier_has_side_effects("high")

    def test_get_action_tier_known(self):
        """Ferramenta conhecida retorna seu tier."""
        assert _get_action_tier("web_search") == "safe"
        assert _get_action_tier("media_control") == "low"
        assert _get_action_tier("send_message") == "medium"
        assert _get_action_tier("root_exec") == "high"

    def test_get_action_tier_unknown(self):
        """Ferramenta desconhecida retorna 'high' (conservador)."""
        assert _get_action_tier("ferramenta_inexistente") == "high"


# ── 3. Executor: marca como UNVERIFIED sem prova ─────────────────────────────

class TestExecutorUnverified:
    """Verifica se o executor marca resultados como UNVERIFIED sem prova material."""

    def test_action_without_proof_marked_unverified(self, executor_with_runner,
                                                      tmp_activity_log):
        """Ação com efeito colateral sem prova material é marcada unverified."""
        def runner(tool, args):
            return {"ok": True, "spoken": "música tocando"}

        exe = executor_with_runner(runner)
        result = exe.run("t1", "media_control", {})

        assert result.unverified is True
        assert result.ok is True
        # Fatos devem ter assertion=False (guard rejeita).
        for fact in result.facts:
            assert fact.get("assertion") is False
            assert fact.get("unverified") is True

    def test_action_with_proof_not_unverified(self, executor_with_runner,
                                                tmp_activity_log):
        """Ação com prova material NÃO é marcada unverified."""
        def runner(tool, args):
            return {
                "ok": True, "spoken": "música tocando",
                "call_id": "abc-123",
                "receipt_hmac": "sha256:deadbeef",
                "verified": True,
            }

        exe = executor_with_runner(runner)
        result = exe.run("t1", "media_control", {})

        assert result.unverified is False
        for fact in result.facts:
            assert fact.get("assertion") is True

    def test_safe_action_without_proof_not_unverified(self, executor_with_runner,
                                                        tmp_activity_log):
        """Ação safe (web_search) sem prova NÃO é marcada unverified."""
        def runner(tool, args):
            return {"ok": True, "spoken": "resultado da busca"}

        exe = executor_with_runner(runner)
        result = exe.run("t1", "web_search", {})

        assert result.unverified is False

    def test_unverified_logs_to_activity(self, executor_with_runner,
                                          tmp_activity_log):
        """Ação unverified grava TURN_UNVERIFIED no activity log."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe = executor_with_runner(runner)
        exe.run("t1", "media_control", {})

        content = tmp_activity_log.read_text(encoding="utf-8")
        assert "TURN_UNVERIFIED" in content
        assert "media_control" in content

    def test_unverified_event_emitted(self, executor_with_runner,
                                       tmp_activity_log):
        """Executor emita evento para o orquestrador via result.unverified."""
        events = []

        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe = executor_with_runner(runner)
        result = exe.run("t1", "media_control", {})
        assert result.unverified is True


# ── 4. Guard: rejeita afirmação sem prova material ───────────────────────────

class TestGuardRejectsUnverified:
    """Verifica se o guard rejeita afirmações sem prova material."""

    def test_guard_rejects_assertion_with_unverified_fact(self):
        """Guard rejeita frase que afirma resultado de fato unverified."""
        facts = [{
            "ok": True,
            "assertion": False,  # invalidada pelo executor
            "unverified": True,
            "spoken": "pronto",
        }]
        ok, reason = guard("ja esta pronto", facts, phase="complement")
        assert ok is False
        assert "sem fato autorizado" in reason or "sem prova material" in reason

    def test_guard_accepts_assertion_with_proof(self):
        """Guard aceita frase quando fato tem prova material."""
        facts = [{
            "ok": True,
            "assertion": True,
            "spoken": "pronto",
            "call_id": "abc-123",
            "receipt_hmac": "sha256:deadbeef",
            "verified": True,
        }]
        ok, reason = guard("ja esta pronto", facts, phase="complement")
        # Pode passar ou falhar na verificação externa (depende do verifier),
        # mas NÃO deve falhar por "sem prova material".
        if not ok:
            assert "sem prova material" not in reason

    def test_guard_rejects_explicit_unverified_marker(self):
        """Guard rejeita explicitamente fatos marcados como unverified."""
        facts = [{
            "ok": True,
            "assertion": True,  # mesmo com assertion=True
            "unverified": True,  # marca de sem prova
            "spoken": "pronto",
        }]
        ok, reason = guard("ja esta pronto", facts, phase="complement")
        assert ok is False
        assert "sem prova material" in reason


# ── 5. Orquestrador: evento TURN_UNVERIFIED ──────────────────────────────────

class TestOrchestratorUnverified:
    """Verifica se o orquestrador emite TURN_UNVERIFIED."""

    def test_orchestrator_marks_turn_unverified(self, tmp_activity_log):
        """Orquestrador emite evento TURN_UNVERIFIED para resultado unverified."""
        events = []

        def on_event(evt):
            events.append(evt)

        config = FlowsConfig()
        orch = Orchestrator(config, on_event=on_event,
                            log=lambda msg: None)

        # Cria um executor que retorna resultado sem prova.
        def runner(tool, args):
            return {"ok": True, "spoken": "pronto"}

        orch.executor = Executor(
            runner=runner,
            catalog={"media_control"},
        )

        # Cria um plano manualmente com uma ação.
        from core.flows.router import Action
        plan = orch.begin("t1", "musica")
        # Adiciona uma ação diretamente ao plano.
        plan.decision.actions = [Action(tool="media_control", args={},
                                         requires_confirm=False)]
        results = orch.run_plan(plan)

        # Verifica que o turno foi marcado como UNVERIFIED.
        unverified_events = [e for e in events if e.get("type") == "TURN_UNVERIFIED"]
        assert len(unverified_events) >= 1
        assert results[0].unverified is True

    def test_orchestrator_no_event_for_safe_action(self, tmp_activity_log):
        """Orquestrador NÃO emite TURN_UNVERIFIED para ação safe."""
        events = []

        def on_event(evt):
            events.append(evt)

        config = FlowsConfig()
        orch = Orchestrator(config, on_event=on_event,
                            log=lambda msg: None)

        def runner(tool, args):
            return {"ok": True, "spoken": "resultado"}

        orch.executor = Executor(
            runner=runner,
            catalog={"web_search"},
        )

        from core.flows.router import Action
        plan = orch.begin("t1", "pesquise algo")
        plan.decision.actions = [Action(tool="web_search", args={},
                                         requires_confirm=False)]
        results = orch.run_plan(plan)

        unverified_events = [e for e in events if e.get("type") == "TURN_UNVERIFIED"]
        assert len(unverified_events) == 0
        assert results[0].unverified is False


# ── 6. Modo observador ──────────────────────────────────────────────────────

class TestObserverMode:
    """Verifica o modo observador (primeiras 2 semanas)."""

    def test_observer_mode_within_two_weeks(self):
        """Dentro das 2 primeiras semanas, modo observador está ativo."""
        # FASE3_TASK4_START é 2026-11-15. Dentro de 2 semanas:
        within = FASE3_TASK4_START + timedelta(days=7)
        assert _is_observer_mode(within) is True

    def test_observer_mode_after_two_weeks(self):
        """Após 2 semanas, modo observador está desativo."""
        after = FASE3_TASK4_START + timedelta(days=15)
        assert _is_observer_mode(after) is False

    def test_observer_mode_before_start(self):
        """Antes do início da task, modo observador não está ativo."""
        before = FASE3_TASK4_START - timedelta(days=1)
        assert _is_observer_mode(before) is False

    def test_observer_mode_restores_assertions(self, executor_with_runner,
                                                 tmp_activity_log):
        """Em modo observador, asserções são restauradas (não bloqueia)."""
        # Garante que estamos em modo observador.
        now = FASE3_TASK4_START + timedelta(days=1)
        with patch("core.flows.executor._is_observer_mode", return_value=True):
            def runner(tool, args):
                return {"ok": True, "spoken": "feito"}

            exe = executor_with_runner(runner)
            result = exe.run("t1", "media_control", {})

            # unverified=True (marca), mas assertion=True (observador não bloqueia).
            assert result.unverified is True
            for fact in result.facts:
                assert fact.get("assertion") is True  # restaurado pelo observador

    def test_blocking_mode_invalidates_assertions(self, executor_with_runner,
                                                     tmp_activity_log):
        """Fora do modo observador, asserções são invalidadas (bloqueia)."""
        with patch("core.flows.executor._is_observer_mode", return_value=False):
            def runner(tool, args):
                return {"ok": True, "spoken": "feito"}

            exe = executor_with_runner(runner)
            result = exe.run("t1", "media_control", {})

            assert result.unverified is True
            for fact in result.facts:
                assert fact.get("assertion") is False  # invalidado


# ── 7. Painel GUIA: marcadores UNVERIFIED ────────────────────────────────────

class TestGuidePanelUnverified:
    """Verifica se o painel GUIA exibe marcadores UNVERIFIED."""

    def test_unverified_markers_empty(self, tmp_activity_log):
        """Sem logs, marcadores está vazio."""
        markers = unverified_markers()
        assert markers == []

    def test_unverified_markers_from_log(self, tmp_activity_log):
        """Marcadores são lidos do activity log."""
        # Escreve um evento TURN_UNVERIFIED no log.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] TURN_UNVERIFIED: task_id=t1 tool=media_control reason=sem prova\n"
        tmp_activity_log.write_text(line, encoding="utf-8")

        with patch("core.flows.executor._ACTIVITY_LOG_PATH", tmp_activity_log):
            markers = unverified_markers()

        assert len(markers) >= 1
        assert markers[0]["tool"] == "media_control"
        assert markers[0]["task_id"] == "t1"

    def test_guide_sections_include_execution(self, tmp_path):
        """sections() inclui a seção EXECUÇÃO CONCRETA."""
        from core.guide_panel import sections
        secs = sections(tmp_path)
        exec_sections = [s for s in secs if "EXECUÇÃO" in s.get("title", "")]
        assert len(exec_sections) >= 1
        assert exec_sections[0]["short"] == "EXEC"


# ── 8. Integração: fluxo completo ────────────────────────────────────────────

class TestFullIntegration:
    """Teste de integração: executor -> guard -> orquestrador."""

    def test_unverified_action_blocked_by_guard(self, tmp_activity_log):
        """Ação sem prova: executor marca unverified, guard rejeita afirmação."""
        def runner(tool, args):
            return {"ok": True, "spoken": "pronto"}

        exe = Executor(runner=runner, catalog={"media_control"})
        result = exe.run("t1", "media_control", {})

        assert result.unverified is True

        # O guard deve rejeitar a afirmação.
        facts = result.facts
        ok, reason = guard("ja esta pronto", facts, phase="complement")
        assert ok is False

    def test_verified_action_passes_guard(self, tmp_activity_log):
        """Ação com prova: executor não marca unverified, guard aceita."""
        def runner(tool, args):
            return {
                "ok": True, "spoken": "pronto",
                "call_id": "abc-123",
                "receipt_hmac": "sha256:deadbeef",
                "verified": True,
            }

        exe = Executor(runner=runner, catalog={"media_control"})
        result = exe.run("t1", "media_control", {})

        assert result.unverified is False

        # O guard deve aceitar (ou falhar em outra verificação, não em prova).
        facts = result.facts
        ok, reason = guard("ja esta pronto", facts, phase="complement")
        if not ok:
            assert "sem prova material" not in reason


# ── 9. Activity log: formato do evento ───────────────────────────────────────

class TestActivityLogFormat:
    """Verifica o formato do evento TURN_UNVERIFIED no activity log."""

    def test_log_format(self, tmp_activity_log):
        """Evento tem formato esperado: [timestamp] TURN_UNVERIFIED: ..."""
        _log_unverified("media_control", "t1", "teste")
        content = tmp_activity_log.read_text(encoding="utf-8")
        assert "TURN_UNVERIFIED" in content
        assert "tool=media_control" in content
        assert "task_id=t1" in content
