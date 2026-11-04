"""Fila durável: replay idempotente depois de queda, exatamente uma vez."""
from __future__ import annotations

import json

from core.flow_ops.durable import ACKED, CLAIMED, PENDING, DurableQueue


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_enqueue_is_idempotent_and_survives_reopen(tmp_path):
    path = tmp_path / "q.jsonl"
    clock = Clock()
    first = DurableQueue(path, clock=clock)
    assert first.enqueue("t1", {"text": "toca Queen"}) is True
    assert first.enqueue("t1", {"text": "duplicado"}) is False

    reopened = DurableQueue(path, clock=clock)
    pending = reopened.pending()
    assert [rec.turn_id for rec in pending] == ["t1"]
    assert pending[0].payload["text"] == "toca Queen"
    assert pending[0].status == PENDING


def test_pending_turn_comes_back_exactly_once_after_crash(tmp_path):
    path = tmp_path / "q.jsonl"
    clock = Clock()
    crashed = DurableQueue(path, clock=clock)
    crashed.enqueue("t1", {"text": "um"})
    crashed.enqueue("t2", {"text": "dois"})
    # simula queda: o objeto morre sem ack e sem close
    del crashed

    seen: list[str] = []
    after_crash = DurableQueue(path, clock=clock)
    report = after_crash.replay(lambda payload: seen.append(payload["text"]))
    assert report.replayed == 2 and report.acked == 2
    assert seen == ["um", "dois"]

    # o replay roda de novo depois de outro reinício: nada volta
    again = DurableQueue(path, clock=clock)
    second = again.replay(lambda payload: seen.append("NAO"))
    assert second.replayed == 0 and second.acked == 0
    assert seen == ["um", "dois"]
    assert [rec.status for rec in again.pending()] == []


def test_claimed_but_unacked_turn_is_not_replayed(tmp_path):
    """At-most-once: se a queda foi depois do claim, repetir seria pior."""
    path = tmp_path / "q.jsonl"
    queue = DurableQueue(path)
    queue.enqueue("t1", {"text": "perigoso"})
    claimed = queue.claim("t1")
    assert claimed is not None and claimed.status == CLAIMED
    del queue

    seen: list[str] = []
    reopened = DurableQueue(path)
    report = reopened.replay(lambda payload: seen.append(payload["text"]))
    assert report.replayed == 0
    assert seen == []
    assert reopened.get("t1").status == CLAIMED


def test_ack_makes_turn_permanently_done_and_dedupes(tmp_path):
    path = tmp_path / "q.jsonl"
    queue = DurableQueue(path)
    assert queue.enqueue("t1", {}) is True
    assert queue.ack("t1") is True
    assert queue.ack("t1") is False
    assert queue.enqueue("t1", {}) is False
    assert queue.get("t1").status == ACKED


def test_handler_exception_releases_for_next_replay(tmp_path):
    path = tmp_path / "q.jsonl"
    queue = DurableQueue(path)
    queue.enqueue("t1", {"n": 1})
    first = queue.replay(lambda _payload: (_ for _ in ()).throw(RuntimeError("x")))
    assert first.failed == 1 and first.acked == 0
    assert queue.get("t1").status == PENDING

    seen: list[dict] = []
    second = queue.replay(seen.append)
    assert second.acked == 1 and seen == [{"n": 1}]


def test_handler_returning_false_keeps_turn_pending(tmp_path):
    queue = DurableQueue(tmp_path / "q.jsonl")
    queue.enqueue("t1", {})
    assert queue.replay(lambda _payload: False).failed == 1
    assert queue.get("t1").status == PENDING


def test_torn_last_line_does_not_erase_previous_turns(tmp_path):
    path = tmp_path / "q.jsonl"
    queue = DurableQueue(path)
    queue.enqueue("t1", {"text": "bom"})
    queue.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"op":"enqueue","turn_id":"t2","payl')
        handle.flush()

    reopened = DurableQueue(path)
    assert [rec.turn_id for rec in reopened.pending()] == ["t1"]
    assert reopened.enqueue("t3", {}) is True
    assert [rec.turn_id for rec in reopened.pending()] == ["t1", "t3"]


def test_stats_reports_pending_and_acks(tmp_path):
    queue = DurableQueue(tmp_path / "q.jsonl")
    queue.enqueue("t1", {})
    queue.enqueue("t2", {})
    queue.ack("t1")
    stats = queue.stats()
    assert stats["turns"] == 2
    assert stats["pending"] == 1
    assert stats["acked"] == 1
    assert stats["pending_ids"] == ["t2"]
