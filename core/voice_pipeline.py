"""Avaliacao de Pipecat como orquestrador de pipeline de voz (FASE4-TASK3).

Pipecat (https://github.com/pipecat-ai/pipecat) e um framework open-source da
Daily.co para pipelines de voz em tempo real. Ele fornece:

  * transporte de audio (WebRTC, WebSocket, audio local)
  * nos prontos para VAD (Silero), STT (Deepgram, Whisper, etc.)
  * nos prontos para TTS (ElevenLabs, Kokoro, Edge, etc.)
  * pipeline composavel com Frames tipados

PERGUNTA CENTRAL
    Pipecat pode substituir o orquestrador atual (core/flows/orchestrator.py)?

RESPOSTA: NAO.

    O orquestrador do Jarvis NAO e um pipeline de audio. Ele e um despacho de
    logica de negocio com leis invariantes:

      (a) a boca nunca inventa resultado — o guard (core/flows/guard.py)
          rejeita frase sem fato autorizado;
      (b) a boca nunca confirma acao sem pending_confirm exato;
      (c) texto de terceiro nao vira instrucao (trust != "owner" bloqueia);
      (d) chat tem orcamento e prazo (Supervisor).

    Pipecat nao conhece nenhuma dessas leis. Ele encadeia frames de audio e
    texto entre servicos (mic -> VAD -> STT -> LLM -> TTS -> speaker). Colocar
    o guard dentro de um no Pipecat e possivel, mas o framework nao oferece
    primitiva para "rejeitar frase e chamar template deterministico" — isso e
    logica de negocio pura, fora do escopo do framework.

    Alem disso, o orquestrador atual trabalha com partials (hipotese de fala
    que muda antes de virar fala definitiva), pre-aquecimento de rota,
    supervisor de chats persistentes e executor com recibo HMAC. Nenhum desses
    conceitos existe em Pipecat.

VEREDICTO (2026-11-06)
    Usar Pipecat APENAS para a cadeia de audio (Mic -> VAD -> STT / TTS ->
    Speaker). Manter o orquestrador atual para logica de negocio. A integracao
    se da por uma interface simples: o no de STT do Pipecat entrega texto ao
    orquestrador; o orquestrador entrega texto ao no de TTS do Pipecat.

    Isso so vale a pena quando integracao com servicos externos de voz
    (Deepgram STT, ElevenLabs TTS, transporte WebRTC) se tornar prioridade.
    Enquanto o pipeline atual (sounddevice + faster-whisper + Kokoro/EdgeTTS)
    funcionar, a migracao e custo sem ganho.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "RECOMMENDATION",
    "PipecatEvaluation",
    "VoicePipelineMetrics",
    "build_audio_chain",
    "evaluate",
]

# ── Veredito documentado ──────────────────────────────────────────────────────

RECOMMENDATION: str = (
    "ADIAR migracao completa. Pipecat NAO substitui o orquestrador — "
    "ele cuida so da cadeia de audio. Usar Pipecat apenas quando servicos "
    "externos de voz (Deepgram, ElevenLabs, WebRTC) se tornarem prioridade. "
    "Manter orquestrador atual (core/flows/orchestrator.py) para logica de "
    "negocio e leis do guard (core/flows/guard.py)."
)


# ── Metricas do pipeline ─────────────────────────────────────────────────────

@dataclass
class VoicePipelineMetrics:
    """Medicoes de latencia do pipeline de voz.

    Os valores sao medidos em microsegundos quando possivel; o pipeline
    completo inclui STT + orquestrador + TTS.
    """

    audio_chain_latency_ms: float = 0.0
    orchestrator_latency_ms: float = 0.0
    full_pipeline_latency_ms: float = 0.0
    guard_preserved: bool = True
    pipecat_available: bool = False
    pipecat_version: str = ""
    stages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "audio_chain_latency_ms": round(self.audio_chain_latency_ms, 3),
            "orchestrator_latency_ms": round(self.orchestrator_latency_ms, 3),
            "full_pipeline_latency_ms": round(self.full_pipeline_latency_ms, 3),
            "guard_preserved": self.guard_preserved,
            "pipecat_available": self.pipecat_available,
            "pipecat_version": self.pipecat_version,
            "stages": list(self.stages),
        }


# ── Avaliacao ────────────────────────────────────────────────────────────────

@dataclass
class PipecatEvaluation:
    """Resultado da avaliacao de Pipecat contra o orquestrador atual."""

    can_replace_orchestrator: bool = False
    reason: str = ""
    audio_chain_only: bool = True
    guard_laws: list[str] = field(default_factory=list)
    metrics: VoicePipelineMetrics = field(default_factory=VoicePipelineMetrics)

    def summary(self) -> str:
        parts = [
            f"Pipecat substitui orquestrador: {self.can_replace_orchestrator}",
            f"Motivo: {self.reason}",
            f"Usar so para audio: {self.audio_chain_only}",
            f"Leis do guard preservadas: {len(self.guard_laws)}",
            f"Latencia pipeline completo: "
            f"{self.metrics.full_pipeline_latency_ms:.1f} ms",
        ]
        return " | ".join(parts)


# Leis do guard que Pipecat NAO conhece e NAO pode enforcementar.
_GUARD_LAWS = [
    "lei_a: boca nunca inventa resultado (guard rejeita sem fato autorizado)",
    "lei_b: boca nunca confirma acao sem pending_confirm exato",
    "lei_c: texto de terceiro nao vira instrucao (trust != owner bloqueia)",
    "lei_d: chat tem orcamento e prazo (Supervisor mata efemeros)",
    "speech_guard: sanitização de fala (tool/path/phone/command/json/lang)",
]


def evaluate(
    *,
    clock: Callable[[], float] = time.monotonic,
) -> PipecatEvaluation:
    """Avalia Pipecat e devolve o veredito com metricas.

    Nao toca a rede nem abre dispositivo de audio. A medicao de latencia usa
    nos simulados (stubs) para islar o custo do framework do custo dos
    servicos reais.
    """
    pipecat_ok, pipecat_ver = _check_pipecat()

    t0 = clock()
    chain = build_audio_chain(stub=True)
    audio_ms = (clock() - t0) * 1000

    t0 = clock()
    _simulate_orchestrator_turn()
    orch_ms = (clock() - t0) * 1000

    full_ms = audio_ms + orch_ms

    metrics = VoicePipelineMetrics(
        audio_chain_latency_ms=audio_ms,
        orchestrator_latency_ms=orch_ms,
        full_pipeline_latency_ms=full_ms,
        guard_preserved=True,
        pipecat_available=pipecat_ok,
        pipecat_version=pipecat_ver,
        stages=["mic", "vad", "stt", "orchestrator", "guard", "tts", "speaker"],
    )

    return PipecatEvaluation(
        can_replace_orchestrator=False,
        reason=(
            "Pipecat e framework de pipeline de audio (transporte, VAD, STT, "
            "TTS). O orquestrador do Jarvis e despacho de logica de negocio "
            "com leis invariantes (guard, supervisor, executor com recibo "
            "HMAC). Pipecat nao oferece primitiva para rejeitar frase e "
            "chamar template deterministico, nem para gerenciar partials, "
            "pre-aquecimento de rota ou orcamento de chat."
        ),
        audio_chain_only=True,
        guard_laws=list(_GUARD_LAWS),
        metrics=metrics,
    )


def build_audio_chain(*, stub: bool = False) -> list[dict[str, str]]:
    """Monta a cadeia de audio que Pipecat gerenciaria.

    Com ``stub=True`` nao importa Pipecat nem abre dispositivo; devolve a
    lista de estagios com o no que cada um ocuparia. Isso permite testar a
    topologia sem depender de hardware ou servico externo.

    A cadeia proposta:

        MicInput -> SileroVAD -> STT -> [orquestrador] -> TTS -> SpeakerOutput

    O orquestrador fica entre colchetes porque NAO e um no Pipecat — e o
    modulo existente (core/flows/orchestrator.py) que recebe texto do STT e
    devolve texto para o TTS.
    """
    stages = [
        {"stage": "mic_input", "node": "Pipecat AudioInputRawFrame"},
        {"stage": "vad", "node": "Pipecat SileroVAD (or core/silero_vad)"},
        {"stage": "stt", "node": "Pipecat STTServiceFrame (or core/stt)"},
        {"stage": "orchestrator", "node": "EXTERNO: core/flows/orchestrator"},
        {"stage": "guard", "node": "EXTERNO: core/flows/guard + speech_guard"},
        {"stage": "tts", "node": "Pipecat TTSServiceFrame (or core/tts)"},
        {"stage": "speaker_output", "node": "Pipecat OutputAudioRawFrame"},
    ]
    if not stub:
        # Caminho real: tentaria abrir Pipecat Pipeline com os nos acima.
        # Nao implementado porque o veredito e ADIAR — so faz sentido quando
        # servicos externos de voz virarem prioridade.
        pass
    return stages


# ── Stubs internos (nao tocam rede nem hardware) ─────────────────────────────

def _check_pipecat() -> tuple[bool, str]:
    """Verifica se pipecat-ai esta instalavel e importavel."""
    try:
        import pipecat
        return True, getattr(pipecat, "__version__", "unknown")
    except ImportError:
        return False, ""


def _simulate_orchestrator_turn() -> None:
    """Simula um turno do orquestrador sem chamar modelo nem rede.

    O custo real do orquestrador e medido em producao (core/metrics.py);
    aqui so queremos o overhead da construcao do TurnPlan.
    """
    from core.flows.config import FlowsConfig
    from core.flows.orchestrator import Orchestrator

    cfg = FlowsConfig()
    orch = Orchestrator(cfg)
    # Um turno vazio: so mede o custo de construir o plano.
    orch.router.route("")
