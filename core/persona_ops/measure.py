"""Medição de um turno com persona antes/depois da paralelização.

O script ``scripts/measure_persona_turn.py`` é a ponta de fora: lê amostras
JSONL, resume latência/tokens/custo e compara as duas fases. Este módulo é a
parte pura, testável sem tocar em áudio, rede ou no assistente vivo.

Formato de uma amostra (uma linha JSON)::

    {"phase": "baseline", "persona": "aline", "latency_ms": 8120.0,
     "tokens_in": 900, "tokens_out": 240, "cost": 0.0, "ok": true,
     "at": "2026-11-03T13:12:00-03:00", "source": "qoder/medido"}

Nada aqui inventa medição: amostra ausente vira ``measured=False`` e o resumo
diz isso. ``--demo`` gera números sintéticos marcados ``simulated=True`` para
exercitar o relatório, nunca para afirmar ganho real.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

PHASES = ("baseline", "parallel")


class MeasureError(ValueError):
    """Amostra ou arquivo de medição inválido."""


@dataclass(frozen=True)
class TurnSample:
    phase: str
    persona: str
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    ok: bool = True
    simulated: bool = False
    at: str = ""
    source: str = ""

    @property
    def tokens(self) -> int:
        return int(self.tokens_in) + int(self.tokens_out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "persona": self.persona,
            "latency_ms": round(float(self.latency_ms), 3),
            "tokens_in": int(self.tokens_in),
            "tokens_out": int(self.tokens_out),
            "cost": round(float(self.cost), 6),
            "ok": bool(self.ok),
            "simulated": bool(self.simulated),
            "at": self.at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TurnSample":
        if not isinstance(raw, Mapping):
            raise MeasureError("amostra deve ser objeto JSON")
        phase = str(raw.get("phase") or "").strip().casefold()
        if phase not in PHASES:
            raise MeasureError(f"fase inválida: {phase!r}")
        persona = str(raw.get("persona") or "").strip()
        if not persona:
            raise MeasureError("amostra sem persona")
        try:
            latency = float(raw.get("latency_ms"))
        except (TypeError, ValueError):
            raise MeasureError("latency_ms deve ser número") from None
        if latency < 0:
            raise MeasureError("latency_ms negativo")
        try:
            tokens_in = max(0, int(raw.get("tokens_in") or 0))
            tokens_out = max(0, int(raw.get("tokens_out") or 0))
            cost = max(0.0, float(raw.get("cost") or 0.0))
        except (TypeError, ValueError):
            raise MeasureError("tokens/custo inválidos") from None
        return cls(
            phase=phase, persona=persona, latency_ms=latency,
            tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
            ok=bool(raw.get("ok", True)),
            simulated=bool(raw.get("simulated", False)),
            at=str(raw.get("at") or ""), source=str(raw.get("source") or ""))


def parse_jsonl(path: str | Path) -> list[TurnSample]:
    target = Path(path)
    if not target.is_file():
        raise MeasureError(f"arquivo de medição não encontrado: {target}")
    out: list[TurnSample] = []
    for number, line in enumerate(
            target.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise MeasureError(
                f"linha {number} de {target} não é JSON: {exc}") from None
        try:
            out.append(TurnSample.from_dict(raw))
        except MeasureError as exc:
            raise MeasureError(f"linha {number} de {target}: {exc}") from None
    return out


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * (percent / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def summarize(samples: Iterable[TurnSample]) -> dict[str, Any]:
    items = [s for s in samples if isinstance(s, TurnSample)]
    ok_items = [s for s in items if s.ok]
    latencies = [float(s.latency_ms) for s in ok_items]
    if not items:
        return {
            "measured": False, "count": 0, "count_ok": 0,
            "latency_ms": {"p50": 0.0, "p95": 0.0, "mean": 0.0,
                           "min": 0.0, "max": 0.0},
            "tokens": {"in": 0, "out": 0, "total": 0, "mean": 0.0},
            "cost": 0.0,
            "simulated": False,
        }
    tokens_in = sum(s.tokens_in for s in ok_items)
    tokens_out = sum(s.tokens_out for s in ok_items)
    return {
        "measured": bool(ok_items),
        "count": len(items),
        "count_ok": len(ok_items),
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 3),
            "p95": round(_percentile(latencies, 95), 3),
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "min": round(min(latencies), 3) if latencies else 0.0,
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "tokens": {
            "in": tokens_in, "out": tokens_out,
            "total": tokens_in + tokens_out,
            "mean": round((tokens_in + tokens_out) / len(ok_items), 2)
            if ok_items else 0.0,
        },
        "cost": round(sum(s.cost for s in ok_items), 6),
        "simulated": any(s.simulated for s in items),
    }


def compare(before: Iterable[TurnSample],
            after: Iterable[TurnSample], *,
            min_samples: int = 5) -> dict[str, Any]:
    """Compara as fases sem decidir pelo dono: devolve números e leitura."""
    old = summarize(before)
    new = summarize(after)
    enough = (old["count_ok"] >= min_samples
              and new["count_ok"] >= min_samples)
    speedup = 0.0
    delta_ms = 0.0
    delta_pct = 0.0
    if old["measured"] and new["measured"]:
        old_p50 = float(old["latency_ms"]["p50"])
        new_p50 = float(new["latency_ms"]["p50"])
        delta_ms = round(new_p50 - old_p50, 3)
        if new_p50 > 0:
            speedup = round(old_p50 / new_p50, 3)
        if old_p50 > 0:
            delta_pct = round((delta_ms / old_p50) * 100.0, 2)
    return {
        "measured": bool(old["measured"] and new["measured"]),
        "enough_samples": bool(enough),
        "min_samples": int(min_samples),
        "before": old,
        "after": new,
        "latency_ms_delta": delta_ms,
        "latency_pct_delta": delta_pct,
        "speedup": speedup,
        "tokens_delta": int(new["tokens"]["total"]) - int(old["tokens"]["total"]),
        "cost_delta": round(float(new["cost"]) - float(old["cost"]), 6),
        "simulated": bool(old["simulated"] or new["simulated"]),
        "note": (
            "sem amostras suficientes: nada foi medido de verdade"
            if not enough else
            "números medidos; a decisão de sub-orquestrador é do dono"
        ),
    }


def demo_samples() -> tuple[list[TurnSample], list[TurnSample]]:
    """Amostras sintéticas para exercitar o relatório; nunca são medição."""
    before = [TurnSample("baseline", "aline", 8000.0 + i * 120.0,
                         tokens_in=900, tokens_out=240, simulated=True,
                         source="demo") for i in range(6)]
    after = [TurnSample("parallel", "aline", 4500.0 + i * 80.0,
                        tokens_in=900, tokens_out=240, simulated=True,
                        source="demo") for i in range(6)]
    return before, after
