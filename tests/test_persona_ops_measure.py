"""Medição: resumo, comparação e o script que roda depois do reinício."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.persona_ops.measure import (MeasureError, TurnSample, compare,
                                      demo_samples, parse_jsonl, summarize)

ROOT = Path(__file__).resolve().parents[1]


def _samples(phase: str, latencies: list[float], *, simulated=False):
    return [TurnSample(phase, "aline", ms, tokens_in=100, tokens_out=20,
                       simulated=simulated) for ms in latencies]


def test_summarize_empty_is_not_measured():
    summary = summarize([])
    assert summary["measured"] is False
    assert summary["count"] == 0


def test_summarize_percentiles_and_cost():
    summary = summarize(_samples("baseline", [100, 200, 300, 400, 500]))
    assert summary["measured"] is True
    assert summary["latency_ms"]["p50"] == 300
    assert summary["latency_ms"]["p95"] == 480
    assert summary["tokens"]["total"] == 5 * 120


def test_compare_detects_speedup_without_deciding_for_the_owner():
    before = _samples("baseline", [800, 900, 1000, 1100, 1200])
    after = _samples("parallel", [400, 450, 500, 550, 600])
    result = compare(before, after)
    assert result["measured"] is True
    assert result["enough_samples"] is True
    assert result["speedup"] > 1.5
    assert result["latency_ms_delta"] < 0
    assert "decisão" in result["note"]


def test_compare_with_few_samples_does_not_claim_measurement():
    result = compare(_samples("baseline", [1000]),
                     _samples("parallel", [500]))
    assert result["measured"] is True
    assert result["enough_samples"] is False
    assert "nada foi medido" in result["note"]


def test_parse_jsonl_rejects_bad_rows(tmp_path):
    path = tmp_path / "turns.jsonl"
    path.write_text("\n".join([
        json.dumps({"phase": "baseline", "persona": "aline",
                    "latency_ms": 1000}),
        json.dumps({"phase": "nope", "persona": "aline",
                    "latency_ms": 1000}),
    ]), encoding="utf-8")
    try:
        parse_jsonl(path)
    except MeasureError as exc:
        assert "fase inválida" in str(exc)
    else:
        raise AssertionError("linha inválida passou")


def test_demo_is_always_marked_simulated():
    before, after = demo_samples()
    result = compare(before, after)
    assert result["simulated"] is True
    assert result["before"]["simulated"] is True


def test_script_runs_with_the_suite(tmp_path):
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "measure_persona_turn.py"),
         "--demo", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["demo"] is True
    assert payload["simulated"] is True
    assert payload["measured"] is True


def test_script_declares_missing_measurement(tmp_path):
    before = tmp_path / "before.jsonl"
    after = tmp_path / "after.jsonl"
    before.write_text(
        json.dumps({"phase": "baseline", "persona": "aline",
                    "latency_ms": 1000}) + "\n", encoding="utf-8")
    after.write_text("", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "measure_persona_turn.py"),
         "--before", str(before), "--after", str(after), "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    payload = json.loads(out.stdout)
    assert payload["measured"] is False
