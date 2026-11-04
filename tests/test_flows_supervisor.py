"""Supervisor: TTL mata só efêmeros; raciocínio compacta e nunca morre."""
from __future__ import annotations

from core.flows.config import Budget, FlowsConfig
from core.flows.supervisor import (KIND_EXECUTOR, KIND_MEDIA_MONITOR,
                                   KIND_MONITOR, KIND_REASONING, Supervisor)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _supervisor():
    clock = Clock()
    sup = Supervisor(clock=clock)
    cfg = FlowsConfig()
    sup.register("chat_raciocinio", KIND_REASONING, cfg.reasoning, now=clock.now)
    sup.register("chat_ferramentas", KIND_EXECUTOR, cfg.executor, now=clock.now)
    sup.register("chat_whatsapp", KIND_MONITOR, cfg.monitor, now=clock.now)
    sup.register("chat_email", KIND_MONITOR, cfg.monitor, now=clock.now)
    sup.register("monitor_multimidia", KIND_MEDIA_MONITOR,
                 cfg.media_monitor, now=clock.now)
    return clock, sup, cfg


def test_ttl_kills_executor_but_not_reasoning_or_monitors():
    clock, sup, _ = _supervisor()
    clock.advance(601)
    killed = dict(sup.expire(now=clock.now))
    assert "chat_ferramentas" in killed
    assert "chat_raciocinio" not in killed
    assert "chat_whatsapp" not in killed
    assert sup.chats["chat_raciocinio"].alive
    assert not sup.chats["chat_ferramentas"].alive


def test_ttl_kills_monitors_at_thirty_minutes():
    clock, sup, _ = _supervisor()
    clock.advance(1801)
    killed = dict(sup.expire(now=clock.now))
    assert "chat_whatsapp" in killed
    assert "chat_email" in killed
    assert "chat_raciocinio" not in killed


def test_media_monitor_dies_at_five_minutes():
    clock, sup, _ = _supervisor()
    clock.advance(301)
    killed = dict(sup.expire(now=clock.now))
    assert "monitor_multimidia" in killed


def test_monitors_without_touch_survive_expire_from_real_config():
    """WhatsApp/e-mail nunca recebem touch(): o TTL do arquivo real é 0.

    Regressão: com ttl_s=1800 o ``expire()`` matava os dois vigilantes a cada
    ciclo de 5 s depois de 30 min de silêncio, mesmo com eles em uso.
    """
    clock = Clock()
    sup = Supervisor(clock=clock)
    cfg = FlowsConfig.load()          # config/flows.json, sem override
    sup.register("chat_whatsapp", KIND_MONITOR, cfg.monitor, now=clock.now)
    sup.register("chat_email", KIND_MONITOR, cfg.monitor, now=clock.now)
    clock.advance(12 * 3600)          # 12 h sem nenhum carimbo de atividade
    assert sup.expire(now=clock.now) == []
    assert sup.chats["chat_whatsapp"].alive
    assert sup.chats["chat_email"].alive
    assert cfg.monitor.ttl_s <= 0


def test_positive_ttl_ephemeral_is_still_killed():
    """O orçamento de contexto legítimo continua valendo."""
    clock = Clock()
    sup = Supervisor(clock=clock)
    sup.register("chat_ferramentas", KIND_EXECUTOR,
                 Budget(16000, 600.0, 0.8, True), now=clock.now)
    clock.advance(601)
    killed = dict(sup.expire(now=clock.now))
    assert killed["chat_ferramentas"] == "silencio dentro do TTL"
    assert not sup.chats["chat_ferramentas"].alive


def test_touch_keeps_ephemeral_alive():
    clock, sup, _ = _supervisor()
    clock.advance(500)
    sup.touch("chat_ferramentas", now=clock.now)
    clock.advance(200)
    killed = dict(sup.expire(now=clock.now))
    assert "chat_ferramentas" not in killed


def test_reasoning_compacts_at_80_percent_and_is_never_killed():
    _, sup, _ = _supervisor()
    assert sup.add_usage("chat_raciocinio", 32000) == "compact"
    assert sup.chats["chat_raciocinio"].alive
    assert sup.add_usage("chat_raciocinio", 50000) == "compact"
    assert sup.chats["chat_raciocinio"].alive
    assert sup.kill("chat_raciocinio", "teste") is False
    assert sup.chats["chat_raciocinio"].alive


def test_executor_budget_kills_chat():
    _, sup, _ = _supervisor()
    assert sup.add_usage("chat_ferramentas", 16500) == "killed"
    assert not sup.chats["chat_ferramentas"].alive
    assert "orcamento estourado" in sup.chats["chat_ferramentas"].kills


def test_monitor_budget_kills_chat():
    _, sup, _ = _supervisor()
    assert sup.add_usage("chat_whatsapp", 8001) == "killed"
    assert not sup.chats["chat_whatsapp"].alive


def test_each_process_has_its_own_budget_object():
    cfg = FlowsConfig()
    assert cfg.reasoning.ephemeral is False
    assert cfg.executor.tokens == 16000
    assert cfg.monitor.tokens == 8000
    assert cfg.media_monitor.ttl_s == 300
    assert cfg.executor.ttl_s == 600
    assert Budget(1).ephemeral is True
