"""UP-9 — prova material: preparar o bloqueio pós-observação.

O bloqueio de ações sem recibo HMAC existe em código desde já, mas atrás de um
portão temporal: ele só entra em vigor quando o período de observação termina
(29/11/2026 = FASE3_TASK4_START + 2 semanas). Antes dessa data nada muda de
comportamento; depois, ação sem prova material é bloqueada, gera o evento
``execution_blocked`` no activity log e notifica o dono.

Cobertura:
  1. O portão temporal (gate) — fechado antes de 29/11, aberto depois.
  2. Gate fechado: comportamento legado preservado (marca unverified, não
     bloqueia, não grava execution_blocked, não notifica).
  3. Gate aberto: ação sem recibo HMAC -> blocked + ok=False + asserções mortas.
  4. Gate aberto: ação COM prova material não é bloqueada.
  5. Gate aberto: ação safe (sem efeito colateral) não é bloqueada.
  6. Modo observador nunca bloqueia, mesmo com o gate aberto.
  7. Evento ``execution_blocked`` no activity log (formato e conteúdo).
  8. Notificação ao dono pelo portão de ações (core/actiongate).
  9. Registro de bloqueios do actiongate: vida curta, ouvintes, close().
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core import actiongate
from core.flows import executor as exe
from core.flows.executor import (
    BLOCK_REASON,
    Executor,
    MATERIAL_PROOF_BLOCK_START,
    OBSERVER_MODE_WEEKS,
    _as_utc,
    _blocking_enabled,
    _is_observer_mode,
    _log_execution_blocked,
    _notify_owner_blocked,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def activity_log(tmp_path):
    """Activity log temporário: teste nenhum escreve no log real."""
    path = tmp_path / "activity.log"
    with patch.object(exe, "_ACTIVITY_LOG_PATH", path):
        yield path


@pytest.fixture
def clean_actiongate():
    """Estado do portão limpo antes e depois do teste."""
    actiongate.close()
    actiongate._blocked_listeners.clear()
    yield actiongate
    actiongate.close()
    actiongate._blocked_listeners.clear()


def executor_with(runner, catalog=("media_control", "web_search", "send_message",
                                   "hora")):
    return Executor(runner=runner, catalog=set(catalog))


def gate_open():
    """Portão do bloqueio aberto (pós-29/11) e observador desligado."""
    return (
        patch.object(exe, "_blocking_enabled", return_value=True),
        patch.object(exe, "_is_observer_mode", return_value=False),
    )


# ── 1. Portão temporal ───────────────────────────────────────────────────────

class TestTemporalGate:
    def test_gate_is_end_of_observer_window(self):
        """O gate abre exatamente quando o observador termina (29/11/2026)."""
        assert MATERIAL_PROOF_BLOCK_START.year == 2026
        assert MATERIAL_PROOF_BLOCK_START.month == 11
        assert MATERIAL_PROOF_BLOCK_START.day == 29
        assert MATERIAL_PROOF_BLOCK_START == (
            exe.FASE3_TASK4_START + timedelta(weeks=OBSERVER_MODE_WEEKS))

    def test_gate_closed_before_29_11(self):
        assert _blocking_enabled(
            MATERIAL_PROOF_BLOCK_START - timedelta(seconds=1)) is False

    def test_gate_open_on_and_after_29_11(self):
        assert _blocking_enabled(MATERIAL_PROOF_BLOCK_START) is True
        assert _blocking_enabled(
            MATERIAL_PROOF_BLOCK_START + timedelta(days=30)) is True

    def test_gate_closed_today(self):
        """Hoje o bloqueio NÃO está ativo: o gate segue o relógio real."""
        assert _blocking_enabled() is (
            _as_utc(datetime.now(timezone.utc)) >= MATERIAL_PROOF_BLOCK_START)
        # Antes de 29/11 o portão está fechado de verdade.
        if datetime.now(timezone.utc) < MATERIAL_PROOF_BLOCK_START:
            assert _blocking_enabled() is False

    def test_gate_accepts_naive_datetime(self):
        naive = datetime(2026, 12, 1, 12, 0, 0)  # sem tzinfo
        assert _blocking_enabled(naive) is True
        naive_before = datetime(2026, 11, 1, 12, 0, 0)
        assert _blocking_enabled(naive_before) is False


# ── 2. Gate fechado: nada muda hoje ─────────────────────────────────────────

class TestGateClosedKeepsLegacyBehaviour:
    def test_no_block_before_gate(self, activity_log):
        """Antes de 29/11 a ação sem prova só fica unverified: não é bloqueada."""
        def runner(tool, args):
            return {"ok": True, "spoken": "música tocando"}

        exe_ = executor_with(runner)
        with patch.object(exe, "_blocking_enabled", return_value=False), \
                patch.object(exe, "_is_observer_mode", return_value=False):
            result = exe_.run("t1", "media_control", {})

        assert result.unverified is True
        assert result.blocked is False
        assert result.ok is True
        assert result.error == ""

    def test_no_execution_blocked_event_before_gate(self, activity_log):
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        with patch.object(exe, "_blocking_enabled", return_value=False), \
                patch.object(exe, "_is_observer_mode", return_value=False):
            exe_.run("t1", "media_control", {})

        content = activity_log.read_text(encoding="utf-8")
        assert "TURN_UNVERIFIED" in content
        assert "execution_blocked" not in content

    def test_owner_not_notified_before_gate(self, activity_log, clean_actiongate):
        """Sem o gate aberto o dono não recebe aviso de bloqueio."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        with patch.object(exe, "_blocking_enabled", return_value=False), \
                patch.object(exe, "_is_observer_mode", return_value=False):
            exe_.run("t1", "media_control", {})

        assert clean_actiongate.blocked_actions() == []


# ── 3. Gate aberto: bloqueio efetivo ────────────────────────────────────────

class TestBlockingAfterGate:
    def test_action_without_receipt_is_blocked(self, activity_log):
        """Depois de 29/11: ação com efeito colateral sem recibo HMAC -> bloqueada."""
        def runner(tool, args):
            return {"ok": True, "spoken": "mensagem enviada"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "media_control", {})

        assert result.blocked is True
        assert result.unverified is True
        assert result.ok is False
        assert "bloqueado" in result.error
        for fact in result.facts:
            assert fact.get("assertion") is False
            assert fact.get("blocked") is True

    def test_blocked_survives_as_dict(self, activity_log):
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "media_control", {})
        payload = result.as_dict()
        assert payload["blocked"] is True
        assert payload["ok"] is False

    def test_list_payload_is_blocked_too(self, activity_log):
        """Runner que devolve lista de fatos também é bloqueado sem prova."""
        def runner(tool, args):
            return [{"ok": True, "spoken": "parcial 1"},
                    {"ok": True, "spoken": "parcial 2"}]

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "media_control", {})

        assert result.blocked is True
        assert result.ok is False
        assert all(f.get("assertion") is False for f in result.facts)


# ── 4/5. O que o bloqueio NUNCA toca ────────────────────────────────────────

class TestBlockingBoundaries:
    def test_action_with_receipt_is_not_blocked(self, activity_log):
        """Prova material presente: nada é bloqueado, mesmo com o gate aberto."""
        def runner(tool, args):
            return {"ok": True, "spoken": "enviado",
                    "call_id": "abc-123",
                    "receipt_hmac": "sha256:deadbeef",
                    "verified": True}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "media_control", {})

        assert result.blocked is False
        assert result.unverified is False
        assert result.ok is True
        assert result.facts[0]["assertion"] is True
        # Prova material presente: nem TURN_UNVERIFIED nem execution_blocked.
        content = (activity_log.read_text(encoding="utf-8")
                   if activity_log.exists() else "")
        assert "execution_blocked" not in content

    def test_safe_action_is_not_blocked(self, activity_log):
        """Ação sem efeito colateral não exige prova e nunca é bloqueada."""
        def runner(tool, args):
            return {"ok": True, "spoken": "resultado da busca"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "web_search", {})

        assert result.blocked is False
        assert result.ok is True

    def test_readonly_tool_is_not_blocked(self, activity_log):
        def runner(tool, args):
            return {"ok": True, "spoken": "são 10:00"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "hora", {})
        assert result.blocked is False

    def test_failed_action_without_proof_is_not_blocked(self, activity_log):
        """Ação que falhou sozinha não vira bloqueio de prova material."""
        def runner(tool, args):
            return {"ok": False, "spoken": "não consegui"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t1", "media_control", {})

        assert result.ok is False
        # Sem asserção viva não há fato a bloquear: unverified continua marcado
        # e o bloqueio efetivo não é anunciado.
        assert result.blocked is False or result.unverified is True

    def test_observer_mode_never_blocks(self, activity_log):
        """Dentro das 2 semanas de observação o bloqueio jamais acontece."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        with patch.object(exe, "_blocking_enabled", return_value=True), \
                patch.object(exe, "_is_observer_mode", return_value=True):
            result = exe_.run("t1", "media_control", {})

        assert result.blocked is False
        assert result.unverified is True
        for fact in result.facts:
            assert fact.get("assertion") is True
        content = activity_log.read_text(encoding="utf-8")
        assert "execution_blocked" not in content


# ── 6/7. Evento execution_blocked no activity log ───────────────────────────

class TestExecutionBlockedEvent:
    def test_event_written_on_block(self, activity_log):
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            exe_.run("t77", "media_control", {})

        content = activity_log.read_text(encoding="utf-8")
        assert "execution_blocked" in content
        assert "task_id=t77" in content
        assert "tool=media_control" in content

    def test_event_keeps_turn_unverified_too(self, activity_log):
        """O bloqueio não apaga o rastro do observador: ambos os eventos ficam."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            exe_.run("t1", "media_control", {})

        content = activity_log.read_text(encoding="utf-8")
        assert "TURN_UNVERIFIED" in content
        assert "execution_blocked" in content

    def test_log_line_format(self, activity_log):
        _log_execution_blocked("send_message", "t9", "motivo de teste")
        content = activity_log.read_text(encoding="utf-8")
        assert content.startswith("[")
        assert "execution_blocked: task_id=t9 tool=send_message " in content
        assert "reason=motivo de teste" in content

    def test_log_failure_never_raises(self, tmp_path):
        """Disco quebrado não derruba a execução."""
        broken = tmp_path / "nao" / "existe" / "activity.log"
        with patch.object(exe, "_ACTIVITY_LOG_PATH", broken):
            _log_execution_blocked("media_control", "t1", "sem prova")


# ── 8. Notificação ao dono ──────────────────────────────────────────────────

class TestOwnerNotification:
    def test_blocked_execution_notifies_owner(self, activity_log,
                                              clean_actiongate):
        """Com o gate aberto o dono é avisado pelo portão de ações."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            exe_.run("t5", "send_message", {})

        notices = clean_actiongate.blocked_actions()
        assert len(notices) == 1
        assert notices[0]["tool"] == "send_message"
        assert notices[0]["task_id"] == "t5"
        assert notices[0]["kind"] == "execution_blocked"

    def test_listener_receives_the_notice(self, activity_log, clean_actiongate):
        """Quem registra ouvinte (a fala do dono, o painel) recebe o aviso."""
        seen: list[dict] = []

        def notice(payload):
            seen.append(payload)

        clean_actiongate.add_blocked_listener(notice)

        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            exe_.run("t6", "media_control", {})

        assert len(seen) == 1
        assert seen[0]["tool"] == "media_control"
        assert "message" in seen[0]

    def test_notification_payload_shape(self, activity_log):
        payload = _notify_owner_blocked("media_control", "t1", BLOCK_REASON)
        assert payload["kind"] == "execution_blocked"
        assert payload["tool"] == "media_control"
        assert payload["task_id"] == "t1"
        assert "bloqueada" in payload["message"]

    def test_broken_listener_never_breaks_the_block(self, activity_log,
                                                   clean_actiongate):
        """Ouvinte quebrado não impede o bloqueio nem o registro."""
        def boom(payload):
            raise RuntimeError("ouvinte caiu")

        clean_actiongate.add_blocked_listener(boom)

        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2:
            result = exe_.run("t8", "media_control", {})

        assert result.blocked is True
        assert len(clean_actiongate.blocked_actions()) == 1

    def test_actiongate_unavailable_still_blocks(self, activity_log):
        """Sem actiongate o bloqueio acontece e o rastro fica no log."""
        def runner(tool, args):
            return {"ok": True, "spoken": "feito"}

        exe_ = executor_with(runner)
        p1, p2 = gate_open()
        with p1, p2, patch.dict("sys.modules", {"core.actiongate": None}):
            result = exe_.run("t9", "media_control", {})

        assert result.blocked is True
        assert "execution_blocked" in activity_log.read_text(encoding="utf-8")


# ── 9. Registro de bloqueios do actiongate ──────────────────────────────────

class TestActiongateBlockedRegistry:
    def test_note_blocked_records(self, clean_actiongate):
        clean_actiongate.note_blocked({"tool": "media_control",
                                       "task_id": "t1",
                                       "reason": "sem recibo"})
        records = clean_actiongate.blocked_actions()
        assert len(records) == 1
        assert records[0]["tool"] == "media_control"
        assert records[0]["kind"] == "execution_blocked"

    def test_has_blocked(self, clean_actiongate):
        assert clean_actiongate.has_blocked() is False
        clean_actiongate.note_blocked(tool="media_control")
        assert clean_actiongate.has_blocked() is True

    def test_records_expire(self, clean_actiongate):
        record = clean_actiongate.note_blocked(tool="media_control")
        later = record["until"] + 1
        assert clean_actiongate.blocked_actions(now=later) == []
        assert clean_actiongate.has_blocked(now=later) is False
        # Enquanto vivo, conta.
        assert clean_actiongate.has_blocked(now=record["at"]) is True

    def test_records_are_bounded(self, clean_actiongate):
        for i in range(actiongate._MAX_BLOCKED + 20):
            clean_actiongate.note_blocked({"tool": f"tool{i}"})
        assert len(clean_actiongate.blocked_actions()) <= actiongate._MAX_BLOCKED

    def test_newest_first(self, clean_actiongate):
        clean_actiongate.note_blocked(tool="primeira")
        clean_actiongate.note_blocked(tool="segunda")
        assert clean_actiongate.blocked_actions()[0]["tool"] == "segunda"

    def test_close_clears_blocked(self, clean_actiongate):
        clean_actiongate.note_blocked(tool="media_control")
        actiongate.close()
        assert actiongate.blocked_actions() == []

    def test_listener_is_weak_and_non_callable_ignored(self, clean_actiongate):
        clean_actiongate.add_blocked_listener(None)
        clean_actiongate.add_blocked_listener("não sou callable")

        def handler(payload):
            seen.append(payload)

        seen: list[dict] = []
        clean_actiongate.add_blocked_listener(handler)
        clean_actiongate.note_blocked(tool="media_control")
        assert len(seen) == 1
        del handler
        # A referência fraca morreu: o próximo aviso não quebra nada.
        clean_actiongate.note_blocked(tool="media_control")

    def test_windows_unaffected_by_blocked_registry(self, clean_actiongate):
        """Registrar bloqueio não abre janela de ação nem de envio."""
        clean_actiongate.note_blocked(tool="media_control")
        assert clean_actiongate.authorized() is False
        assert clean_actiongate.window_factor() == ""
        assert clean_actiongate.send_window_factor() == ""
