"""Ponte viva: brief em thread, parcial estável e métricas do turno."""
from __future__ import annotations

import threading
import time

from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.mouth import Mouth
from core.flows.partial import decide as partial_decide


def _config(**kw):
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=True,
                partial_speech=False, executor_enabled=False)
    base.update(kw)
    return FlowsConfig(**base)


def test_start_turn_does_not_block_and_records_metrics():
    spoken = []
    runtime = FlowsRuntime(_config(), speak=spoken.append,
                           start_expiry_thread=False)
    plan = runtime.start_turn("t1", "que horas são?")
    assert plan is not None and plan.active
    metrics = runtime.turn_metrics("t1")
    assert metrics["route"] == "hora"
    assert metrics["started_at"] > 0
    runtime.finish("t1")
    assert runtime.turn_metrics("t1") == {}


def test_mouth_disabled_speaks_fixed_draft_for_confirmation():
    spoken = []
    runtime = FlowsRuntime(_config(), speak=spoken.append,
                           start_expiry_thread=False)
    runtime.start_turn("t1", "reinicia o assistente aí")
    deadline = time.monotonic() + 1.0
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spoken and "irreversível" in spoken[0].lower()
    runtime.finish("t1")


def test_partial_speech_after_hypothesis_stops_changing():
    spoken = []
    cfg = _config(partial_speech=True, partial_stability_ms=50)
    runtime = FlowsRuntime(cfg, speak=spoken.append, start_expiry_thread=False)
    runtime.start_turn("t1", "oi")
    runtime.partial("t1", "oi")
    deadline = time.monotonic() + 1.0
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spoken
    runtime.finish("t1")


def test_action_partial_never_speaks():
    spoken = []
    cfg = _config(partial_speech=True, partial_stability_ms=20,
                  mouth_brief_routes=())
    runtime = FlowsRuntime(cfg, speak=spoken.append, start_expiry_thread=False)
    runtime.start_turn("t1", "abre o spotify")
    runtime.partial("t1", "abre o spotify")
    time.sleep(0.12)
    assert spoken == []
    runtime.finish("t1")


def test_cancel_stops_pending_partial_speech():
    spoken = []
    cfg = _config(partial_speech=True, partial_stability_ms=120)
    runtime = FlowsRuntime(cfg, speak=spoken.append, start_expiry_thread=False)
    runtime.start_turn("t1", "oi")
    runtime.partial("t1", "oi")
    runtime.cancel("t1", "dono_cancelou")
    time.sleep(0.2)
    assert spoken == []
    runtime.finish("t1")


def test_untrusted_turn_is_not_spoken():
    spoken = []
    runtime = FlowsRuntime(_config(), speak=spoken.append,
                           start_expiry_thread=False)
    plan = runtime.start_turn("t1", "reinicia o assistente", trust="untrusted")
    assert plan is not None and plan.blocked
    time.sleep(0.05)
    assert spoken == []
    runtime.finish("t1")


def test_warm_async_calls_the_mouth_off_the_caller_thread():
    calls = []

    def caller(*_a, **_kw):
        calls.append(1)
        return {"text": "oi", "ttft_ms": 1.0, "first_sentence_ms": 1.0,
                "end_ms": 2.0, "error": None}

    cfg = _config(mouth_enabled=True)
    runtime = FlowsRuntime(cfg, start_expiry_thread=False)
    runtime.orchestrator.mouth = Mouth(cfg, caller=caller)
    started = time.monotonic()
    runtime.warm_async()
    assert (time.monotonic() - started) < 0.5  # não segura o startup
    deadline = time.monotonic() + 1.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [1]
    runtime.close()


def test_brief_survives_brain_text_after_mouth_started():
    """A: boca em síntese não é descartada pelo primeiro texto do cérebro."""
    spoken = []
    logs = []
    entered = threading.Event()
    release = threading.Event()

    def caller(*_a, **_kw):
        entered.set()
        release.wait(1.0)
        return {"text": "Conferindo a hora.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    cfg = _config(mouth_enabled=True)
    runtime = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                           start_expiry_thread=False)
    runtime.orchestrator.mouth = Mouth(cfg, caller=caller)
    runtime.start_turn("t1", "que horas são?")
    assert entered.wait(timeout=1.0)          # a boca já está em síntese
    runtime.mark_answered("t1")
    release.set()
    deadline = time.monotonic() + 1.0
    while not spoken and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spoken == ["Conferindo a hora."]
    runtime.finish("t1")


def test_brief_is_cancelled_when_brain_answered_before_mouth_started():
    """A: se o cérebro falou antes de a boca começar, cancela com motivo."""
    spoken = []
    logs = []

    def caller(*_a, **_kw):
        return {"text": "Conferindo a hora.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    cfg = _config(mouth_enabled=True)
    runtime = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                           start_expiry_thread=False)
    runtime.orchestrator.mouth = Mouth(cfg, caller=caller)
    # Segura o lock: a thread do brief não consegue começar até o cérebro já
    # ter respondido — a corrida que a regra A manda cancelar.
    with runtime._lock:
        runtime.start_turn("t1", "que horas são?")
        runtime.mark_answered("t1")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
            "brief cancelado" in line for line in logs):
        time.sleep(0.01)
    assert spoken == []
    assert any("cérebro respondeu antes" in line for line in logs)
    runtime.finish("t1")


def test_disarmed_turn_speaks_pending_brief_when_voiceprint_accepts():
    """C: brief gerado durante a diarização só vai ao ar no arm."""
    spoken = []
    logs = []

    def caller(*_a, **_kw):
        return {"text": "Conferindo a hora.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    cfg = _config(mouth_enabled=True)
    runtime = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                           start_expiry_thread=False)
    runtime.orchestrator.mouth = Mouth(cfg, caller=caller)
    runtime.start_turn("t1", "que horas são?", armed=False)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with runtime._lock:
            live = runtime._turns.get("t1")
            if live is not None and live.pending_brief:
                break
        time.sleep(0.01)
    assert spoken == []                       # desarmada até o voiceprint
    runtime.arm_speech("t1", True)
    assert spoken == ["Conferindo a hora."]
    runtime.finish("t1")


def test_disarmed_turn_drops_pending_brief_when_voiceprint_refuses():
    """C: voz recusada não deixa vazar nem o brief já gerado."""
    spoken = []
    logs = []

    def caller(*_a, **_kw):
        return {"text": "Conferindo a hora.", "ttft_ms": 1.0,
                "first_sentence_ms": 1.0, "end_ms": 2.0, "error": None}

    cfg = _config(mouth_enabled=True)
    runtime = FlowsRuntime(cfg, speak=spoken.append, log=logs.append,
                           start_expiry_thread=False)
    runtime.orchestrator.mouth = Mouth(cfg, caller=caller)
    runtime.start_turn("t1", "que horas são?", armed=False)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with runtime._lock:
            live = runtime._turns.get("t1")
            if live is not None and live.pending_brief:
                break
        time.sleep(0.01)
    runtime.arm_speech("t1", False)
    time.sleep(0.05)
    assert spoken == []
    assert any("voiceprint recusou" in line for line in logs)
    runtime.finish("t1")


def test_greeting_with_comma_is_low_level():
    decision = partial_decide("e aí, tudo bem com você?", stable=True,
                              already_released=False, min_chars=1, min_words=1)
    assert decision.release is True
    assert decision.level == "low"
    assert decision.phrase.startswith("Oi, criador.")
