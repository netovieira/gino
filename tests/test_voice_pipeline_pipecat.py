"""Testes da avaliacao de Pipecat como orquestrador de pipeline de voz.

FASE4-TASK3: Pipecat NAO substitui o orquestrador. Ele cuida so da cadeia
de audio (Mic -> VAD -> STT / TTS -> Speaker). O orquestrador atual
(core/flows/orchestrator.py) continua dono da logica de negocio e das leis
do guard.

Metricas de sucesso:
  - Pipeline processa audio de ponta a ponta (topologia com stubs)
  - Latencia do pipeline de audio < 50 ms
  - Leis do guard preservadas (5 leis documentadas)
  - Recomendacao clara: ADIAR migracao completa
"""
from __future__ import annotations

import time

import pytest

from core.voice_pipeline import (
    RECOMMENDATION,
    PipecatEvaluation,
    VoicePipelineMetrics,
    build_audio_chain,
    evaluate,
)


# ── Topologia da cadeia de audio ─────────────────────────────────────────────

class TestAudioChain:
    """A cadeia de audio tem os estagios esperados (Mic -> Speaker)."""

    def test_chain_has_seven_stages(self):
        chain = build_audio_chain(stub=True)
        assert len(chain) == 7

    def test_chain_starts_with_mic(self):
        chain = build_audio_chain(stub=True)
        assert chain[0]["stage"] == "mic_input"

    def test_chain_ends_with_speaker(self):
        chain = build_audio_chain(stub=True)
        assert chain[-1]["stage"] == "speaker_output"

    def test_chain_includes_vad(self):
        chain = build_audio_chain(stub=True)
        stages = [s["stage"] for s in chain]
        assert "vad" in stages

    def test_chain_includes_stt_and_tts(self):
        chain = build_audio_chain(stub=True)
        stages = [s["stage"] for s in chain]
        assert "stt" in stages
        assert "tts" in stages

    def test_orchestrator_is_external_node(self):
        """O orquestrador NAO e um no Pipecat — e modulo externo."""
        chain = build_audio_chain(stub=True)
        orch = [s for s in chain if s["stage"] == "orchestrator"][0]
        assert "EXTERNO" in orch["node"]
        assert "core/flows/orchestrator" in orch["node"]

    def test_guard_is_external_node(self):
        """O guard NAO e um no Pipecat — e modulo externo."""
        chain = build_audio_chain(stub=True)
        guard = [s for s in chain if s["stage"] == "guard"][0]
        assert "EXTERNO" in guard["node"]
        assert "core/flows/guard" in guard["node"]


# ── Avaliacao: Pipecat NAO substitui orquestrador ────────────────────────────

class TestEvaluation:
    """O veredito e claro: Pipecat nao substitui o orquestrador."""

    def test_cannot_replace_orchestrator(self):
        result = evaluate()
        assert result.can_replace_orchestrator is False

    def test_audio_chain_only(self):
        result = evaluate()
        assert result.audio_chain_only is True

    def test_reason_is_not_empty(self):
        result = evaluate()
        assert len(result.reason) > 50

    def test_reason_mentions_guard(self):
        result = evaluate()
        assert "guard" in result.reason.lower()

    def test_reason_mentions_business_logic(self):
        result = evaluate()
        assert "logica de negocio" in result.reason.lower() or "negocio" in result.reason.lower()

    def test_guard_laws_preserved(self):
        result = evaluate()
        assert len(result.guard_laws) == 5

    def test_guard_laws_include_lei_a(self):
        result = evaluate()
        laws = " ".join(result.guard_laws)
        assert "lei_a" in laws

    def test_guard_laws_include_lei_b(self):
        result = evaluate()
        laws = " ".join(result.guard_laws)
        assert "lei_b" in laws

    def test_guard_laws_include_lei_c(self):
        result = evaluate()
        laws = " ".join(result.guard_laws)
        assert "lei_c" in laws

    def test_guard_laws_include_lei_d(self):
        result = evaluate()
        laws = " ".join(result.guard_laws)
        assert "lei_d" in laws

    def test_guard_laws_include_speech_guard(self):
        result = evaluate()
        laws = " ".join(result.guard_laws)
        assert "speech_guard" in laws


# ── Metricas de latencia ─────────────────────────────────────────────────────

class TestMetrics:
    """Latencia do pipeline de audio e do pipeline completo."""

    def test_audio_chain_latency_under_50ms(self):
        result = evaluate()
        assert result.metrics.audio_chain_latency_ms < 50.0

    def test_full_pipeline_latency_measured(self):
        result = evaluate()
        assert result.metrics.full_pipeline_latency_ms > 0

    def test_guard_preserved_in_metrics(self):
        result = evaluate()
        assert result.metrics.guard_preserved is True

    def test_stages_listed(self):
        result = evaluate()
        assert len(result.metrics.stages) == 7

    def test_stages_include_all_phases(self):
        result = evaluate()
        expected = {"mic", "vad", "stt", "orchestrator", "guard", "tts", "speaker"}
        actual = set(result.metrics.stages)
        assert expected == actual

    def test_pipecat_available(self):
        """Pipecat foi instalado no ambiente de testes."""
        result = evaluate()
        assert result.metrics.pipecat_available is True

    def test_pipecat_version_not_empty(self):
        result = evaluate()
        assert len(result.metrics.pipecat_version) > 0

    def test_metrics_to_dict(self):
        result = evaluate()
        d = result.metrics.to_dict()
        assert "audio_chain_latency_ms" in d
        assert "guard_preserved" in d
        assert "pipecat_available" in d
        assert isinstance(d["stages"], list)


# ── Recomendacao documentada ─────────────────────────────────────────────────

class TestRecommendation:
    """A recomendacao e clara e documentada."""

    def test_recommendation_says_postpone(self):
        assert "ADIAR" in RECOMMENDATION

    def test_recommendation_says_audio_only(self):
        assert "audio" in RECOMMENDATION.lower()

    def test_recommendation_mentions_guard(self):
        assert "guard" in RECOMMENDATION.lower()

    def test_recommendation_mentions_orchestrator(self):
        assert "orquestrador" in RECOMMENDATION.lower()

    def test_recommendation_not_empty(self):
        assert len(RECOMMENDATION) > 50


# ── Summary ───────────────────────────────────────────────────────────────────

class TestSummary:
    """O summary da avaliacao e legivel e completo."""

    def test_summary_not_empty(self):
        result = evaluate()
        assert len(result.summary()) > 20

    def test_summary_mentions_false(self):
        result = evaluate()
        assert "False" in result.summary()

    def test_summary_mentions_latency(self):
        result = evaluate()
        assert "ms" in result.summary()


# ── Integracao com guard existente ───────────────────────────────────────────

class TestGuardIntegration:
    """O guard existente continua funcionando independentemente do Pipecat."""

    def test_speech_guard_still_works(self):
        """A sanitizacao de fala (speech_guard) nao e afetada pelo Pipecat."""
        from core.speech_guard import sanitize

        result = sanitize("Use o Bash para rodar grep no arquivo /home/test.py")
        assert result.text != ""
        assert "tool" in result.kinds or "path" in result.kinds or "command" in result.kinds

    def test_flows_guard_still_works(self):
        """O guard de fluxo (flows/guard) detecta anuncio de acao."""
        from core.flows.guard import announcement

        assert announcement("Abrindo o Terminal") != ""
        assert announcement("Queen tocando no Spotify") != ""

    def test_flows_guard_rejects_assertion_without_fact(self):
        """O guard detecta assercao sem fato autorizado."""
        from core.flows.guard import has_assertion

        assert has_assertion("Pronto, esta feito") != ""
        assert has_assertion("Bom dia") == ""
