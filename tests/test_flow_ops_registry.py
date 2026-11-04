"""Ciclo de vida fora do orquestrador: heartbeat, TTL, matar e fotografia."""
from __future__ import annotations

from types import MappingProxyType

import pytest

from core.flow_ops.registry import (FlowHeartbeat, LifecycleRegistry,
                                    LifecycleSnapshot, safe_flow_id)


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _registry(tmp_path, clock, kills=None, signal=None):
    return LifecycleRegistry(tmp_path / "registry", clock=clock,
                             on_kill=(kills.append if kills is not None else None),
                             signal_fn=signal)


def test_flow_birth_heartbeat_and_snapshot(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    reg.birth("whatsapp", kind="monitor", ttl_s=30, pid=777)
    beat = FlowHeartbeat(reg.root, "whatsapp", clock=clock)
    assert beat.beat(used_tokens=12, state={"seq": 1}) is True

    snap = reg.snapshot()
    assert isinstance(snap, LifecycleSnapshot)
    assert snap.alive("whatsapp") is True
    record = snap.get("whatsapp")
    assert record["pid"] == 777
    assert record["used_tokens"] == 12
    assert record["age_s"] == 0.0
    assert record["state"] == {"seq": 1}


def test_ttl_expiry_kills_once_and_dead_flow_cannot_beat(tmp_path):
    clock = Clock()
    kills: list = []
    reg = _registry(tmp_path, clock, kills)
    reg.birth("efemero", kind="monitor", ttl_s=10, pid=4242)
    beat = FlowHeartbeat(reg.root, "efemero", clock=clock)
    assert beat.beat() is True

    clock.now += 11
    assert reg.snapshot().alive("efemero") is False
    expiries = reg.expire()
    assert len(expiries) == 1
    assert expiries[0].flow_id == "efemero"
    assert expiries[0].action == "kill"
    assert expiries[0].reason == "ttl"
    assert expiries[0].pid == 4242
    assert len(kills) == 1

    # idempotente: o supervisor pode bater o ponto quantas vezes quiser
    assert reg.expire() == []
    assert len(kills) == 1
    # fluxo morto não ressuscita sozinho
    assert beat.beat() is False
    assert reg.snapshot().alive("efemero") is False

    # e a morte sobrevive a reinício do supervisor (está no disco)
    fresh = _registry(tmp_path, clock, kills)
    assert fresh.expire() == []


def test_explicit_kill_uses_signal_hook_and_persists(tmp_path):
    clock = Clock()
    signals: list[tuple[int, int]] = []
    reg = _registry(tmp_path, clock, signal=lambda pid, sig:
                    signals.append((pid, sig)))
    reg.birth("media", ttl_s=0, pid=999)
    expiry = reg.kill("media", "pedido_do_dono")
    assert expiry is not None and expiry.action == "kill"
    assert signals == [(999, 15)]
    assert reg.kill("media", "de novo") is None


def test_budget_of_non_killable_flow_asks_for_compaction_once(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    reg.birth("raciocinio", kind="reasoning", ttl_s=0, budget_tokens=100,
              killable=False)
    beat = FlowHeartbeat(reg.root, "raciocinio", clock=clock)
    beat.beat(used_tokens=150)
    expiries = reg.expire()
    assert len(expiries) == 1
    assert expiries[0].action == "compact"
    assert expiries[0].reason == "orcamento"
    assert reg.expire() == []
    assert reg.clear_notice("raciocinio") is True
    assert len(reg.expire()) == 1


def test_snapshot_is_immutable_and_detached_from_registry(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    reg.birth("a", ttl_s=5)
    snap = reg.snapshot()
    assert isinstance(snap.flows, MappingProxyType)
    with pytest.raises(TypeError):
        snap.flows["a"] = {}  # type: ignore[index]
    copy = snap.as_dict()
    copy["flows"].clear()
    assert len(snap) == 1
    # o que o quente tem em mãos não sabe expirar nem matar
    assert not hasattr(snap, "expire")
    assert not hasattr(snap, "kill")


def test_heartbeat_records_create_a_flow_even_without_birth(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    hb = FlowHeartbeat(reg.root, "solitario", clock=clock,
                       kind="monitor", ttl_s=4)
    assert hb.beat() is True
    assert reg.snapshot().alive("solitario") is True
    clock.now += 5
    assert reg.snapshot().alive("solitario") is False


def test_forget_removes_record(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    reg.birth("some", ttl_s=99)
    assert reg.forget("some") is True
    assert reg.snapshot().get("some") is None


def test_unsafe_flow_id_is_sanitized():
    assert safe_flow_id("../../etc/passwd") == "etc_passwd"
    with pytest.raises(ValueError):
        safe_flow_id("...")
