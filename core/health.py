"""Component health for the runtime, readable by voice without a dashboard."""
from __future__ import annotations

import threading
import time

_LOCK = threading.RLock()
_COMPONENTS: dict[str, dict] = {}


def set_component(name: str, ok: bool, detail: str = "") -> None:
    name = str(name or "").strip()
    if not name:
        return
    with _LOCK:
        _COMPONENTS[name] = {
            "ok": bool(ok),
            "detail": " ".join(str(detail or "").split())[:200],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def snapshot() -> dict[str, dict]:
    with _LOCK:
        return {k: dict(v) for k, v in _COMPONENTS.items()}
