"""Protocolo parcial do sidecar: o delta chega ao Python, sem virar fala."""
from __future__ import annotations

from core.qoder_live import QoderLive


def test_partial_message_reaches_callback():
    seen = []
    live = QoderLive(on_partial=lambda turn_id, text: seen.append((turn_id, text)),
                     logger=lambda _msg: None)
    live._dispatch({"type": "partial", "id": "t1", "text": "que horas",
                    "delta": True})
    live._dispatch({"type": "partial", "id": "t1", "text": " são",
                    "delta": True})
    assert seen == [("t1", "que horas"), ("t1", " são")]


def test_partial_without_callback_is_ignored():
    live = QoderLive(logger=lambda _msg: None)
    live._dispatch({"type": "partial", "id": "t1", "text": "oi"})
