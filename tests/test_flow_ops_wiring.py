"""Ligação explícita: montar sem instalar, e cada peça no seu ponto."""
from __future__ import annotations

from core.flow_ops.wiring import (ATTACHMENT_POINTS, DEFAULT_OPS_DIR, FlowOps,
                                  attachment_points, build)


def test_build_mounts_every_piece_without_touching_the_pipeline(tmp_path):
    delivered: list[dict] = []
    ops = build(tmp_path, sink=delivered.append)
    try:
        assert isinstance(ops, FlowOps)
        assert ops.root == tmp_path
        assert ops.registry.root == tmp_path / "registry"
        assert ops.queue.path == tmp_path / "pending_turns.jsonl"
        assert ops.warnings == []
        assert ops.cold._thread is None  # nada de thread por importar/montar

        decision = ops.route_and_deliver("toca Queen", turn_id="t1")
        assert decision.delivered is True
        assert delivered and delivered[0]["type"] == "brief"

        task = ops.coordinate(decision)
        assert task.turn_id == "t1"

        beat = ops.heartbeat("monitor_multimidia", kind="media_monitor",
                             ttl_s=60)
        assert beat.beat() is True
        snapshot = ops.lifecycle()
        assert snapshot.alive("monitor_multimidia") is True

        assert ops.speak("Abri o Spotify.", []).accepted is False
        assert ops.speak("Spotify aberto.",
                         [{"ok": True, "spoken": "Spotify aberto"}]
                         ).accepted is True

        assert ops.queue.enqueue("t1", {"text": "toca Queen"}) is True
        seen: list[dict] = []
        report = ops.replay_pending(seen.append)
        assert report.acked == 1 and seen == [{"text": "toca Queen"}]
        assert ops.queue.enqueue("t1", {}) is False

        step = ops.supervisor_tick()
        assert set(step) == {"rules_reloaded", "cold", "expiries"}
    finally:
        ops.close()


def test_attachment_points_name_the_live_call_sites():
    points = attachment_points()
    assert set(points) == {"hot", "cold", "registry", "rules", "durable",
                           "mouth"}
    for key, value in points.items():
        assert "core/flows/" in value, key
        assert len(value) > 40
    assert ATTACHMENT_POINTS is not points  # cópia, não referência mutável


def test_missing_rules_file_warns_and_falls_back_to_defaults(tmp_path):
    ops = build(tmp_path, rules_path=tmp_path / "sem_regras.json",
                sink=lambda _event: None)
    try:
        assert ops.warnings and "ausente" in ops.warnings[0]
        decision = ops.route_and_deliver("qualquer coisa", turn_id="t1")
        assert decision.origin == "default"
        assert decision.action == ops.rules.default_action
    finally:
        ops.close()


def test_default_ops_dir_is_outside_versioned_config():
    assert DEFAULT_OPS_DIR.name == "flows"
    assert DEFAULT_OPS_DIR.parent.name == "logs"
