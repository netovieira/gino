"""Configuração única dos fluxos.

Nenhum módulo do pacote decide por conta própria se uma peça está ligada: o
orquestrador lê esta configuração e é o único ponto de decisão. O arquivo
padrão é ``config/flows.json`` (não contém segredo); ``JARVIS_FLOWS_CONFIG``
permite outro caminho em testes e em medições.

Chaves desconhecidas são ignoradas de propósito: um arquivo antigo não deve
impedir o assistente de subir. A única combinação recusada é fallback local —
o dono vetou o 7b local para qualquer função que não seja falar (ele inventa
ferramenta e erra argumento); nesse caso o valor volta para ``remote``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "flows.json"
ENV_CONFIG_PATH = "JARVIS_FLOWS_CONFIG"

FALLBACK_REMOTE = "remote"
FALLBACK_DISABLED = "disabled"
VALID_FALLBACKS = (FALLBACK_REMOTE, FALLBACK_DISABLED)


@dataclass(frozen=True)
class Budget:
    """Orçamento de um chat persistente.

    ``soft_at`` é a fração do orçamento em que o chat é compactado. Só o chat
    de raciocínio tem direito a compactar; os efêmeros morrem ao estourar.
    """

    tokens: int
    ttl_s: float = 0.0
    soft_at: float = 0.8
    ephemeral: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None,
                  *, default_tokens: int, default_ttl: float,
                  default_ephemeral: bool) -> "Budget":
        data = data or {}
        try:
            tokens = int(data.get("tokens", default_tokens))
        except (TypeError, ValueError):
            tokens = default_tokens
        try:
            ttl_s = float(data.get("ttl_s", default_ttl))
        except (TypeError, ValueError):
            ttl_s = default_ttl
        try:
            soft_at = float(data.get("soft_at", 0.8))
        except (TypeError, ValueError):
            soft_at = 0.8
        ephemeral = bool(data.get("ephemeral", default_ephemeral))
        return cls(tokens=max(0, tokens), ttl_s=max(0.0, ttl_s),
                   soft_at=min(0.99, max(0.1, soft_at)),
                   ephemeral=ephemeral)


@dataclass(frozen=True)
class FlowsConfig:
    enabled: bool = False
    router_enabled: bool = True
    fallback: str = FALLBACK_REMOTE
    fallback_timeout_ms: int = 450

    mouth_enabled: bool = False
    # Fallback do arquivo ausente: LFM3-1.7B, a boca desde 05/11/2026 (mais
    # leve e ~64% mais rápido que o qwen3:4b, ordem do dono).
    mouth_model: str = "lfm3:1.7b"
    mouth_url: str = "http://127.0.0.1:11434"
    mouth_timeout_s: float = 25.0
    mouth_keep_alive: str = "10m"
    mouth_brief_max_tokens: int = 40
    mouth_complement_max_tokens: int = 70
    mouth_chat_max_tokens: int = 60
    # Janela de contexto da boca (fluxo do dono, 05/11/2026, passo 2): os
    # últimos 15 MINUTOS de conversa, configurável. O teto de falas existe
    # para não estourar o prompt: medido em 05/11/2026 na memória real, o
    # bloco formatado fica em ~1,3 KB típico / ~2,5 KB no pior caso com 8
    # falas (cada linha já sai truncada a 180 chars em mouth._recent_lines).
    mouth_context_minutes: float = 15.0
    mouth_context_turns: int = 8
    # Conversa curta na boca (frente local): o roteador decide antes do
    # cérebro; desligável sem código se a medição mostrar regressão.
    local_chat_enabled: bool = True
    mouth_brief_routes: tuple[str, ...] = (
        "musica", "hora", "app", "lembrete", "confirma", "consulta", "livre")

    partial_prewarm: bool = True
    partial_speech: bool = False
    partial_stability_ms: int = 450
    partial_min_chars: int = 1
    partial_min_words: int = 1

    executor_enabled: bool = False
    executor_timeout_ms: int = 4000

    # 40k raciocínio (compacta em 80%), 16k executor, 8k monitores;
    # 10 min executor, monitores sem TTL, 5 min monitor de mídia.
    reasoning: Budget = field(
        default_factory=lambda: Budget(40000, 0.0, 0.8, False))
    executor: Budget = field(
        default_factory=lambda: Budget(16000, 600.0, 0.8, True))
    monitor: Budget = field(
        default_factory=lambda: Budget(8000, 0.0, 0.8, True))
    media_monitor: Budget = field(
        default_factory=lambda: Budget(8000, 300.0, 0.8, True))

    duplicate_window_s: float = 2.0
    log_events: bool = True

    # ── consultas ───────────────────────────────────────────────────────────
    def budget_for(self, kind: str) -> Budget:
        kind = str(kind or "").lower()
        if kind in ("reasoning", "raciocinio", "raciocínio"):
            return self.reasoning
        if kind in ("executor", "tools", "ferramentas"):
            return self.executor
        if kind in ("media_monitor", "monitor_multimidia", "midia", "mídia"):
            return self.media_monitor
        if kind in ("monitor", "whatsapp", "email", "e-mail"):
            return self.monitor
        return Budget(8000, 1800.0, 0.8, True)

    def mouth_speaks_route(self, route: str) -> bool:
        route = str(route or "").lower()
        return any(route.startswith(prefix) for prefix in self.mouth_brief_routes)

    # ── carga ───────────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FlowsConfig":
        data = data or {}
        default = cls()
        router = data.get("router") or {}
        mouth = data.get("mouth") or {}
        partial = data.get("partial") or {}
        executor = data.get("executor") or {}
        supervisor = data.get("supervisor") or {}

        fallback = str(router.get("fallback") or FALLBACK_REMOTE).strip().lower()
        if fallback not in VALID_FALLBACKS:
            # "local" (o 7b da boca) é VETADO como fallback. O padrão é
            # remoto; um valor desconhecido não rebaixa para IA local.
            fallback = FALLBACK_REMOTE

        try:
            fallback_timeout_ms = int(router.get("fallback_timeout_ms") or 450)
        except (TypeError, ValueError):
            fallback_timeout_ms = 450
        try:
            stability_ms = int(partial.get("stability_ms") or 450)
        except (TypeError, ValueError):
            stability_ms = 450
        try:
            min_chars = int(partial.get("min_chars") or 1)
        except (TypeError, ValueError):
            min_chars = 1
        try:
            min_words = int(partial.get("min_words") or 1)
        except (TypeError, ValueError):
            min_words = 1
        try:
            brief_tokens = int(mouth.get("brief_max_tokens") or 40)
        except (TypeError, ValueError):
            brief_tokens = 40
        try:
            complement_tokens = int(mouth.get("complement_max_tokens") or 70)
        except (TypeError, ValueError):
            complement_tokens = 70
        try:
            chat_tokens = int(mouth.get("chat_max_tokens") or 60)
        except (TypeError, ValueError):
            chat_tokens = 60
        try:
            mouth_timeout = float(mouth.get("timeout_s") or 25.0)
        except (TypeError, ValueError):
            mouth_timeout = 25.0
        try:
            context_minutes = float(mouth.get("context_minutes")
                                    or default.mouth_context_minutes)
        except (TypeError, ValueError):
            context_minutes = default.mouth_context_minutes
        try:
            context_turns = int(mouth.get("context_turns")
                                 or default.mouth_context_turns)
        except (TypeError, ValueError):
            context_turns = default.mouth_context_turns
        try:
            executor_timeout = int(executor.get("timeout_ms") or 4000)
        except (TypeError, ValueError):
            executor_timeout = 4000

        routes = mouth.get("brief_routes")
        if isinstance(routes, list):
            brief_routes = tuple(str(r).strip().lower()
                                 for r in routes if str(r).strip())
        else:
            brief_routes = cls.mouth_brief_routes

        def _budget(key: str, default: Budget) -> Budget:
            return Budget.from_dict(
                supervisor.get(key), default_tokens=default.tokens,
                default_ttl=default.ttl_s, default_ephemeral=default.ephemeral)

        return cls(
            enabled=bool(data.get("enabled", False)),
            router_enabled=bool(router.get("enabled", True)),
            fallback=fallback,
            fallback_timeout_ms=max(50, fallback_timeout_ms),
            mouth_enabled=bool(mouth.get("enabled", False)),
            mouth_model=str(mouth.get("model") or default.mouth_model),
            mouth_url=str(mouth.get("url") or default.mouth_url).rstrip("/"),
            mouth_timeout_s=max(1.0, mouth_timeout),
            mouth_keep_alive=str(mouth.get("keep_alive") or default.mouth_keep_alive),
            mouth_brief_max_tokens=max(8, brief_tokens),
            mouth_complement_max_tokens=max(8, complement_tokens),
            mouth_chat_max_tokens=max(16, chat_tokens),
            mouth_context_minutes=min(240.0, max(1.0, context_minutes)),
            mouth_context_turns=min(20, max(1, context_turns)),
            local_chat_enabled=bool(mouth.get("chat_local", True)),
            mouth_brief_routes=brief_routes,
            partial_prewarm=bool(partial.get("prewarm", True)),
            partial_speech=bool(partial.get("speech", False)),
            partial_stability_ms=max(0, stability_ms),
            partial_min_chars=max(1, min_chars),
            partial_min_words=max(1, min_words),
            executor_enabled=bool(executor.get("enabled", False)),
            executor_timeout_ms=max(100, executor_timeout),
            reasoning=_budget("reasoning", default.reasoning),
            executor=_budget("executor", default.executor),
            monitor=_budget("monitor", default.monitor),
            media_monitor=_budget("media_monitor", default.media_monitor),
            duplicate_window_s=float(data.get("duplicate_window_s") or 2.0),
            log_events=bool(data.get("log_events", True)),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "FlowsConfig":
        path = Path(path or os.environ.get(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

    def with_overrides(self, **changes: Any) -> "FlowsConfig":
        """Cópia para testes/medição; produção sempre lê o arquivo."""
        return replace(self, **changes)


def load_config(path: str | os.PathLike[str] | None = None) -> FlowsConfig:
    return FlowsConfig.load(path)
