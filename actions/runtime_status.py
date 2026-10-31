"""Voice-readable runtime status: components + latency/error metrics."""
from __future__ import annotations

from core import health, metrics


def runtime_status(parameters: dict, player=None, speak=None,
                   session_memory=None) -> str:
    comp = health.snapshot()
    snap = metrics.snapshot()
    parts = []
    for name in ("qoder_live", "bridge", "memory", "voice_id", "librarian"):
        item = comp.get(name)
        if item:
            state = "ok" if item.get("ok") else "falha"
            detail = f" ({item.get('detail')})" if item.get("detail") else ""
            parts.append(f"{name}: {state}{detail}")
    counters = snap.get("counters") or {}
    obs = snap.get("observations") or {}
    lines = ["Saúde do runtime:"]
    lines.append("; ".join(parts) if parts else "componentes: sem dados ainda.")
    if counters:
        lines.append("contadores: " + ", ".join(f"{k}={v}" for k, v in sorted(counters.items())))
    for key in ("qoder.ttft_s", "qoder.total_s", "tool.latency_s"):
        item = obs.get(key)
        if item and item.get("count"):
            lines.append(f"{key}: média {item.get('avg')}s, p95 {item.get('p95')}s, "
                         f"n={item.get('count')}")
    return "\n".join(lines)


TOOL = {
    "name": "runtime_status",
    "description": (
        "Report the assistant's runtime health and latency metrics: Qoder Live, "
        "tool bridge, memory librarian, voice identification, turn latency and "
        "errors. Use when the user asks 'como está sua saúde?', 'está lento?', "
        "'o Qoder está de pé?' or asks for diagnostics. Read-only."
    ),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": runtime_status,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
