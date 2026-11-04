#!/usr/bin/env python3
"""Mede a arquitetura de fluxos com a boca real (bonsai-2:9b) e o TTS real.

Não executa ferramenta nenhuma e não toca áudio: as tarefas são pré-preparadas
pelo roteador, e só a consulta de hora usa estado real da máquina. Números não
medidos aparecem como ``None`` no JSON — nunca são inventados.

Uso:
    .venv/bin/python scripts/measure_flows.py
    .venv/bin/python scripts/measure_flows.py --no-tts
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.flows.config import FlowsConfig  # noqa: E402
from core.flows.executor import Executor, readonly_runner  # noqa: E402
from core.flows.orchestrator import Orchestrator  # noqa: E402
from core.flows.partial import decide as partial_decide  # noqa: E402

PHRASES = [
    ("f1", "e aí, tudo bem com você?"),
    ("f2", "que horas são agora?"),
    ("f3", "dá um play naquela música do Queen aí"),
    ("f4", "abre o Spotify... melhor não, deixa quieto"),
    ("f5", "põe o Spotify"),
    ("f6", "me lembra de ligar pro dentista amanhã às 15h"),
    ("f7", "e aquela música que eu pedi ontem, qual era?"),
    ("f8", "dá um jeito naquele negócio lá do fulano"),
    ("f9", "reinicia o assistente aí"),
]

BASELINE_FIRST_SENTENCE_S = 7.0
BASELINE_TOTAL_S = 11.0
BASELINE_TTS_FIRST_AUDIO_S = 2.78


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _tts_probe(text: str) -> dict:
    """Sintetiza a frase no TTS real e mede o PCM pronto (não reproduz)."""
    try:
        from core import brain
        from memory.config_manager import get_plugin_config
        settings = brain.settings(get_plugin_config)
        started = time.monotonic()
        pcm = brain.synth_pcm24k(text, settings)
        return {"text": text, "ms": round(_ms(started), 1),
                "bytes": len(pcm or b""), "engine": settings.get("tts_engine"),
                "error": None if pcm else "pcm vazio"}
    except Exception as exc:
        return {"text": text, "ms": None, "bytes": 0, "error": repr(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-tts", action="store_true",
                        help="não mede a síntese de áudio")
    parser.add_argument("--out", default="/tmp/flows-measure.json")
    args = parser.parse_args()

    cfg = FlowsConfig.load().with_overrides(
        enabled=True, mouth_enabled=True, partial_speech=True,
        partial_stability_ms=250, log_events=False,
        mouth_brief_routes=("saudacao", "musica", "hora", "app", "lembrete",
                            "confirma", "consulta", "contexto", "acao",
                            "cancelamento", "irreversivel"))
    orch = Orchestrator(cfg)
    warm = orch.mouth.warm()
    results = []
    for turn_id, text in PHRASES:
        started = time.monotonic()
        plan = orch.begin(turn_id, text, trust="owner")
        begin_ms = _ms(started)
        brief_started = time.monotonic()
        brief = orch.brief(plan)
        brief_wall_ms = _ms(brief_started)
        results.append({
            "id": turn_id,
            "text": text,
            "route": plan.route,
            "intent": plan.decision.intent,
            "begin_ms": round(begin_ms, 3),
            "brief_ms": round(brief.ms, 1),
            "brief_wall_ms": round(brief_wall_ms, 1),
            "brief_ttft_ms": round(brief.ttft_ms, 1),
            "sentence": brief.sentence,
            "fixed": brief.fixed,
            "guard": brief.guard_tripped,
            "error": brief.error,
        })
        orch.finish(turn_id)

    # Complemento real: hora do sistema (o único fato que não é simulado).
    complement = None
    now = dt.datetime.now().strftime("%H:%M")
    plan = orch.begin("c1", "que horas são agora?", trust="owner")
    fact = {"spoken": f"são {now}", "ok": True, "assertion": True,
            "simulated": False, "detail": "datetime.now real"}
    started = time.monotonic()
    comp = orch.complement(plan, [fact])
    complement = {"sentence": comp.sentence, "ms": round(comp.ms, 1),
                  "wall_ms": round(_ms(started), 1),
                  "fixed": comp.fixed, "guard": comp.guard_tripped,
                  "fact": fact["spoken"]}

    # Stream: deltas de 2 caracteres a cada 20 ms; mede quando a rota fecha e
    # quando uma hipótese de nível baixo poderia falar.
    stream = []
    for turn_id, text in PHRASES:
        plan = orch.begin(f"s_{turn_id}", text, trust="owner")
        t0 = time.monotonic()
        first_prewarm_ms = None
        for index in range(0, len(text), 2):
            delta = text[index:index + 2]
            t_partial = time.monotonic()
            orch.partial(f"s_{turn_id}", delta)
            if first_prewarm_ms is None:
                first_prewarm_ms = round(_ms(t_partial), 1)
            time.sleep(0.02)
        stable_ms = _ms(t0) + cfg.partial_stability_ms
        decision = partial_decide(
            text, stable=True, already_released=False,
            min_chars=cfg.partial_min_chars, min_words=cfg.partial_min_words)
        stream.append({
            "id": turn_id,
            "first_partial_ms": first_prewarm_ms,
            "stable_at_ms": round(stable_ms, 1),
            "would_speak": decision.release,
            "level": decision.level,
            "reason": decision.reason,
            "phrase": decision.phrase,
        })
        orch.finish(f"s_{turn_id}")

    # Portão de ferramenta REAL: a hora vem do relógio pelo executor, o fato
    # só vira asserção porque o runner devolveu sucesso real não simulado.
    gate = Executor(runner=readonly_runner, enabled=True)
    plan = orch.begin("g1", "que horas são agora?", trust="owner")
    t_gate = time.monotonic()
    tool_result = gate.run("g1", "hora")
    gate_ms = _ms(t_gate)
    comp_gate = orch.complement(plan, tool_result.facts)
    gate_phrase_ms = _ms(t_gate)
    executor_measure = {
        "tool": "hora", "ok": tool_result.ok,
        "assertion": tool_result.facts[0]["assertion"] if tool_result.facts else None,
        "fact": tool_result.facts[0]["spoken"] if tool_result.facts else "",
        "executor_ms": round(gate_ms, 2),
        "fact_to_phrase_ms": round(gate_phrase_ms, 1),
        "sentence": comp_gate.sentence, "fixed": comp_gate.fixed,
        "guard": comp_gate.guard_tripped,
    }
    # Monitor de correio com tag de não confiável: nunca vira task.
    monitor = orch.monitor_event(
        "email_otp:email", "código de verificação recebido")
    orch.finish("g1")

    tts_text = next(
        (r["sentence"] for r in results
         if r["sentence"] and not r["fixed"] and not r["guard"]),
        "Conferindo a hora.")
    tts = None
    if not args.no_tts:
        tts = _tts_probe(tts_text)

    # Só frases realmente geradas e aceitas pelo guard entram na média: uma
    # frase rejeitada (f6) não é "1ª frase" entregue ao dono.
    usable = [r["brief_ms"] for r in results
              if r["brief_ms"] and not r["fixed"] and not r["guard"]
              and not r["error"]]
    avg_first_ms = (sum(usable) / len(usable)) if usable else None
    first_audio_ms = None
    if avg_first_ms is not None and tts and tts.get("ms"):
        first_audio_ms = round(avg_first_ms + tts["ms"], 1)

    payload = {
        "when": dt.datetime.now().isoformat(timespec="seconds"),
        "model": cfg.mouth_model,
        "warm": warm,
        "baseline": {
            "first_sentence_s": BASELINE_FIRST_SENTENCE_S,
            "total_s": BASELINE_TOTAL_S,
            "tts_first_audio_s": BASELINE_TTS_FIRST_AUDIO_S,
        },
        "results": results,
        "averages": {
            "first_sentence_ms": round(avg_first_ms, 1)
            if avg_first_ms is not None else None,
            "first_sentence_samples": len(usable),
            "first_sentence_excluded": [
                {"id": r["id"], "why": r["guard"] or r["error"]
                 or ("template fixo" if r["fixed"] else "")}
                for r in results
                if r["guard"] or r["error"] or r["fixed"]
            ],
            "first_audio_measured_ms": first_audio_ms,
            "first_audio_estimate_ms": round(
                avg_first_ms + BASELINE_TTS_FIRST_AUDIO_S * 1000.0, 1)
            if avg_first_ms is not None else None,
            "baseline_first_sentence_ms": BASELINE_FIRST_SENTENCE_S * 1000,
        },
        "complement": complement,
        "executor": executor_measure,
        "monitor": monitor,
        "stream": stream,
        "tts": tts,
        "router": orch.metrics()["router"],
        "mouth": orch.metrics()["mouth"],
        "not_measured": [
            "reprodução no alto-falante (o script mede PCM pronto, não play)",
            "Qoder real concorrente (a integração só pré-aquece rota/contexto)",
            "ferramenta que muda estado (o portão só executa leitura: hora)",
        ],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(json.dumps(payload["averages"], ensure_ascii=False, indent=2))
    print(f"JSON: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
