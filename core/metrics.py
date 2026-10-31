"""Tiny in-process metrics for the voice stack — no external service."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

_LOCK = threading.RLock()
_COUNTERS: dict[str, float] = defaultdict(float)
_OBSERVATIONS: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
_EVENTS: deque = deque(maxlen=80)


def inc(name: str, value: float = 1.0) -> None:
    with _LOCK:
        _COUNTERS[str(name)] += float(value)


def observe(name: str, value: float) -> None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return
    with _LOCK:
        _OBSERVATIONS[str(name)].append(value)


def event(text: str) -> None:
    text = " ".join(str(text or "").split())
    if not text:
        return
    with _LOCK:
        _EVENTS.append({"at": f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
                        "text": text[:200]})


def _summary(values) -> dict[str, Any]:
    data = sorted(float(v) for v in values)
    if not data:
        return {"count": 0}
    count = len(data)
    p95 = data[min(count - 1, int(round(0.95 * (count - 1))))]
    return {
        "count": count,
        "min": round(data[0], 3),
        "max": round(data[-1], 3),
        "avg": round(sum(data) / count, 3),
        "p95": round(p95, 3),
        "last": round(data[-1], 3),
    }


def snapshot() -> dict[str, Any]:
    with _LOCK:
        counters = {k: round(v, 3) for k, v in _COUNTERS.items()}
        observations = {k: _summary(v) for k, v in _OBSERVATIONS.items()}
        events = list(_EVENTS)[-10:]
    return {"counters": counters, "observations": observations, "events": events}


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _OBSERVATIONS.clear()
        _EVENTS.clear()
