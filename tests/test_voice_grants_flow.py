"""Integração voice_grants no fluxo vivo de execução.

Testa que o executor consulta voice_grants ANTES de exigir confirmação:
  - Pessoa com grant ativo: ação executada sem pedir confirmação
  - Pessoa sem grant: ação bloqueia e pede confirmação
  - Grant expirado: ação bloqueia e pede confirmação
  - Integração com actiongate (janelas de 120s)
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from core import voice_grants as vg
from core.flows.executor import Executor, ExecutorResult


@pytest.fixture()
def store(tmp_path):
    """Store temporário para não tocar o memory/voice_grants.json real."""
    st = vg.GrantStore(path=tmp_path / "voice_grants.json")
    vg.set_store(st)
    yield st
    vg.set_store(None)


@pytest.fixture()
def runner():
    """Runner mock que devolve sucesso."""
    def _run(tool: str, args: dict):
        return {"ok": True, "spoken": f"{tool} executado", "detail": "mock"}
    return _run


PRIV = {"owner_ok": True, "confirmed_by": "voiceprint_owner"}


class TestExecutorVoiceGrantsIntegration:
    """O executor consulta voice_grants antes de exigir confirmação."""

    def test_action_allowed_with_active_grant(self, store, runner):
        """Pessoa com grant ativo executa sem pedir confirmação."""
        # Cria grant para valmira em whatsapp
        store.add_grant("valmira", vg.Scope(vg.SCOPE_WHATSAPP), **PRIV)
        
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t1",
            tool="whatsapp_control",
            args={"action": "send", "to": "valmira"},
            requires_confirm=True,
            person="valmira"
        )
        
        # Grant ativo: executou sem pedir confirmação
        assert result.ok is True
        assert result.pending_confirm is False
        assert any(f.get("ok") for f in result.facts)

    def test_action_blocked_without_grant(self, store, runner):
        """Pessoa sem grant: ação bloqueada, pede confirmação."""
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t2",
            tool="whatsapp_control",
            args={"action": "send", "to": "desconhecido"},
            requires_confirm=True,
            person="desconhecido"
        )
        
        # Sem grant: pede confirmação
        assert result.pending_confirm is True
        assert result.ok is True  # ok=True mas pending_confirm=True

    def test_action_blocked_with_expired_grant(self, store, runner):
        """Grant expirado: ação bloqueada, pede confirmação."""
        now = time.monotonic()
        # Cria grant que expira em 5 segundos
        store.add_grant(
            "valmira",
            vg.Scope(vg.SCOPE_EMAIL),
            owner_ok=True,
            confirmed_by="senha_digitada",
            ttl_seconds=5.0,
            now=now
        )
        
        executor = Executor(runner=runner)
        
        # Tenta executar 6 segundos depois (grant expirado)
        result = executor.run(
            task_id="t3",
            tool="email_control",
            args={"action": "send", "to": "valmira@test.com"},
            requires_confirm=True,
            person="valmira",
            now=now + 6.0
        )
        
        # Grant expirado: pede confirmação
        assert result.pending_confirm is True

    def test_action_allowed_without_person(self, store, runner):
        """Sem pessoa identificada: comportamento padrão (pede confirmação)."""
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t4",
            tool="send_message",
            args={"to": "alguem"},
            requires_confirm=True
            # person não informado
        )
        
        # Sem pessoa: pede confirmação (comportamento padrão)
        assert result.pending_confirm is True

    def test_action_without_requires_confirm_ignores_grants(self, store, runner):
        """Ação que não exige confirmação ignora grants."""
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t5",
            tool="media_control",
            args={"action": "play"},
            requires_confirm=False,
            person="valmira"
        )
        
        # Não exige confirmação: executa direto, grant irrelevante
        assert result.ok is True
        assert result.pending_confirm is False


class TestActiongateIntegration:
    """Integração com actiongate (janelas de 120s)."""

    def test_actiongate_window_takes_precedence(self, store, runner):
        """Janela do actiongate aberta: ação executa mesmo sem grant."""
        from core import actiongate
        
        # Abre janela de confirmação do dono
        actiongate.open_window(seconds=120.0)
        
        try:
            executor = Executor(runner=runner)
            result = executor.run(
                task_id="t6",
                tool="whatsapp_control",
                args={"action": "send", "to": "alguem"},
                requires_confirm=True,
                person="desconhecido"  # sem grant
            )
            
            # Janela do actiongate aberta: executa (dono presente)
            assert result.ok is True
            assert result.pending_confirm is False
        finally:
            actiongate.close()

    def test_actiongate_expired_falls_back_to_grants(self, store, runner):
        """Janela do actiongate expirada: consulta grants."""
        from core import actiongate
        from unittest.mock import patch
        
        # Cria grant para valmira
        store.add_grant("valmira", vg.Scope(vg.SCOPE_WHATSAPP), **PRIV)
        
        executor = Executor(runner=runner)
        
        # Mock actiongate.authorized() para retornar False (janela expirada)
        with patch.object(actiongate, 'authorized', return_value=False):
            # Pessoa com grant: executa
            result = executor.run(
                task_id="t7",
                tool="whatsapp_control",
                args={"action": "send", "to": "valmira"},
                requires_confirm=True,
                person="valmira"
            )
            assert result.ok is True
            assert result.pending_confirm is False
            
            # Pessoa sem grant: pede confirmação
            result = executor.run(
                task_id="t8",
                tool="whatsapp_control",
                args={"action": "send", "to": "outra"},
                requires_confirm=True,
                person="outra"
            )
            assert result.pending_confirm is True


class TestScopeMapping:
    """Mapeamento de ferramentas para escopos de grant."""

    def test_whatsapp_maps_to_whatsapp_scope(self, store, runner):
        """whatsapp_control mapeia para SCOPE_WHATSAPP."""
        store.add_grant("valmira", vg.Scope(vg.SCOPE_WHATSAPP), **PRIV)
        
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t9",
            tool="whatsapp_control",
            args={"action": "send"},
            requires_confirm=True,
            person="valmira"
        )
        
        assert result.ok is True
        assert result.pending_confirm is False

    def test_email_maps_to_email_scope(self, store, runner):
        """email_control mapeia para SCOPE_EMAIL."""
        store.add_grant("valmira", vg.Scope(vg.SCOPE_EMAIL), **PRIV)
        
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t10",
            tool="email_control",
            args={"action": "send"},
            requires_confirm=True,
            person="valmira"
        )
        
        assert result.ok is True
        assert result.pending_confirm is False

    def test_send_message_maps_to_whatsapp_scope(self, store, runner):
        """send_message mapeia para SCOPE_WHATSAPP (mensagem direta)."""
        store.add_grant("valmira", vg.Scope(vg.SCOPE_WHATSAPP), **PRIV)
        
        executor = Executor(runner=runner)
        result = executor.run(
            task_id="t11",
            tool="send_message",
            args={"to": "valmira"},
            requires_confirm=True,
            person="valmira"
        )
        
        assert result.ok is True
        assert result.pending_confirm is False

    def test_scope_all_covers_everything(self, store, runner):
        """SCOPE_ALL cobre whatsapp, email e projeto."""
        store.add_grant("valmira", vg.Scope(vg.SCOPE_ALL), **PRIV)
        
        executor = Executor(runner=runner)
        
        # WhatsApp
        result = executor.run(
            task_id="t12",
            tool="whatsapp_control",
            args={},
            requires_confirm=True,
            person="valmira"
        )
        assert result.pending_confirm is False
        
        # Email
        result = executor.run(
            task_id="t13",
            tool="email_control",
            args={},
            requires_confirm=True,
            person="valmira"
        )
        assert result.pending_confirm is False
