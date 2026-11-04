#!/usr/bin/env python3
"""Compara latência e custo de um turno com persona antes/depois.

Roda DEPOIS do reinício, quando houver amostras reais em JSONL. Sem arquivo,
ele não inventa número: imprime ``measured=False`` e sai com código 2.

Formato de cada linha (uma amostra)::

    {"phase": "baseline", "persona": "aline", "latency_ms": 8120.0,
     "tokens_in": 900, "tokens_out": 240, "cost": 0.0, "ok": true,
     "at": "2026-11-03T13:12:00-03:00", "source": "medido"}

Uso:
    .venv/bin/python scripts/measure_persona_turn.py \
        --before logs/personas/turns-baseline.jsonl \
        --after  logs/personas/turns-parallel.jsonl
    .venv/bin/python scripts/measure_persona_turn.py --demo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.persona_ops.measure import (MeasureError, compare, demo_samples,  # noqa: E402
                                      parse_jsonl)


def _summary_line(label: str, summary: dict) -> str:
    if not summary.get("measured"):
        return f"{label}: sem amostras válidas (não medido)"
    lat = summary["latency_ms"]
    tokens = summary["tokens"]
    return (
        f"{label}: n={summary['count_ok']} "
        f"p50={lat['p50']:.1f}ms p95={lat['p95']:.1f}ms "
        f"média={lat['mean']:.1f}ms tokens={tokens['total']} "
        f"custo={summary['cost']:.6f}"
        + (" [SIMULADO]" if summary.get("simulated") else "")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compara um turno com persona antes/depois da paralelização.")
    parser.add_argument("--before", type=Path,
                        help="JSONL da fase antes (baseline)")
    parser.add_argument("--after", type=Path,
                        help="JSONL da fase depois (parallel)")
    parser.add_argument("--demo", action="store_true",
                        help="usa amostras sintéticas marcadas como simuladas")
    parser.add_argument("--json", action="store_true",
                        help="imprime o relatório em JSON")
    parser.add_argument("--min-samples", type=int, default=5,
                        help="amostras boas mínimas por fase (padrão 5)")
    args = parser.parse_args(argv)

    if args.demo:
        before, after = demo_samples()
        result = compare(before, after, min_samples=args.min_samples)
        result["demo"] = True
    else:
        if not args.before or not args.after:
            parser.error("informe --before e --after (ou use --demo)")
        try:
            before = parse_jsonl(args.before)
            after = parse_jsonl(args.after)
        except MeasureError as exc:
            print(f"medição inválida: {exc}", file=sys.stderr)
            return 2
        result = compare(before, after, min_samples=args.min_samples)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_summary_line("antes", result["before"]))
        print(_summary_line("depois", result["after"]))
        print(
            f"delta p50={result['latency_ms_delta']:+.1f}ms "
            f"({result['latency_pct_delta']:+.1f}%) "
            f"speedup={result['speedup']:.2f}x "
            f"tokens={result['tokens_delta']:+d} "
            f"custo={result['cost_delta']:+.6f}")
        print(f"leitura: {result['note']}")
        if result.get("simulated"):
            print("AVISO: há amostras simuladas; isto não é medição real.")
        if not result["measured"]:
            print("Nada foi medido: não há amostras reais suficientes.")
    if not result["measured"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
