"""Ligação explícita entre a onda 2 e o pipeline ao vivo.

Este módulo **não** se instala sozinho: importá-lo não muda o assistente. Ele
monta as peças, expõe os métodos que o pipeline deve chamar e documenta o ponto
exato de cada encaixe. A integração é uma decisão separada, depois que a onda 1
assentar — e, por enquanto, a única coisa ligada é este contrato de chamada.

Estado em disco (fora do repositório versionado, em ``logs/`` que já é
ignorado)::

    <root>/registry/<flow>.json      batimento/ciclo de vida
    <root>/pending_turns.jsonl       fila durável append-only

O caminho vem de ``JARVIS_FLOW_OPS_DIR``; o padrão é ``logs/flows``.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .cold import ColdCoordinator
from .durable import DurableQueue, ReplayReport
from .hot import HotDecision, HotDispatcher
from .mouth_guard import MouthSpeech, MouthWriter
from .registry import FlowHeartbeat, LifecycleRegistry, LifecycleSnapshot
from .rules import DEFAULT_RULES_PATH, RulesTable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_OPS_DIR = "JARVIS_FLOW_OPS_DIR"
DEFAULT_OPS_DIR = PROJECT_ROOT / "logs" / "flows"

#: Onde cada peça encaixa no pipeline vivo (onda 1). Nomes de função, não de
#: linha, para o relatório não envelhecer a cada edição do outro agente.
ATTACHMENT_POINTS: dict[str, str] = {
    "hot": (
        "core/flows/live.py:FlowsRuntime.start_turn — chamar "
        "wiring.route_and_deliver() ANTES de Orchestrator.begin(); o envelope "
        "versionado é o brief entregue à boca. A entrega (sink) deve ser "
        "fila não bloqueante; o quente nunca espera modelo."
    ),
    "cold": (
        "core/flows/live.py:FlowsRuntime._release e _brief_worker — depois da "
        "decisão, wiring.coordinate(decision). O laço de expiração do runtime "
        "(FlowsRuntime._expiry_loop) passa a chamar wiring.tick_cold() em vez "
        "de Orchestrator.expire(); retry/expire/confirm_expired chegam por "
        "ColdCoordinator.on_action, não pelo orquestrador."
    ),
    "registry": (
        "core/flows/live.py:FlowsRuntime.start_turn/finish — heartbeat próprio "
        "do fluxo (FlowHeartbeat.beat) a cada turno. "
        "core/flows/supervisor.py:Supervisor.expire/register — o supervisor "
        "passa a usar LifecycleRegistry.birth/kill/expire e a receber "
        "LifecycleSnapshot; Orchestrator deixa de ter expire()."
    ),
    "rules": (
        "core/flows/router.py:Router.parse — consultar RulesTable.match() "
        "antes das regras hard-coded (que viram fallback); config/flows.json "
        "ganha o caminho da tabela em config/flows_rules.json. Override do dono "
        "é escrito nesse JSON e recarregado por reload_if_changed()."
    ),
    "durable": (
        "core/flows/orchestrator.py:Orchestrator.begin — DurableQueue.enqueue("
        "turn_id, payload) antes de qualquer trabalho; finish/cancel — ack. No "
        "boot do FlowsRuntime, uma vez, DurableQueue.replay(handler) reconstrói "
        "os pendentes que sobreviveram à queda."
    ),
    "mouth": (
        "core/flows/mouth.py:Mouth.brief/complement — no ponto que devolve a "
        "frase para o TTS, passar a frase por MouthWriter.write() (ou say()) e "
        "falar o texto devolvido; template determinístico substitui a fala "
        "quando o guard rejeita. O orquestrador não valida conteúdo."
    ),
}


@dataclass
class FlowOps:
    """Conjunto montado, mas não instalado, da onda 2."""

    root: Path
    rules: RulesTable
    registry: LifecycleRegistry
    queue: DurableQueue
    hot: HotDispatcher
    cold: ColdCoordinator
    mouth: MouthWriter = field(default_factory=MouthWriter)
    warnings: list[str] = field(default_factory=list)

    # ── peças ───────────────────────────────────────────────────────────────
    def route_and_deliver(self, text: str, *, turn_id: str, route: str = "",
                          trust: str = "owner", source: str = "owner",
                          snapshot: LifecycleSnapshot | None = None
                          ) -> HotDecision:
        return self.hot.dispatch(text, turn_id=turn_id, route=route,
                                 trust=trust, source=source,
                                 snapshot=snapshot)

    def coordinate(self, decision: Any) -> Any:
        return self.cold.track(decision)

    def lifecycle(self) -> LifecycleSnapshot:
        return self.registry.snapshot()

    def heartbeat(self, flow_id: str, *, kind: str = "generic",
                  ttl_s: float = 0.0) -> FlowHeartbeat:
        return FlowHeartbeat(self.registry.root, flow_id, kind=kind,
                             ttl_s=ttl_s)

    def replay_pending(self, handler: Callable[[dict[str, Any]], Any]
                       ) -> ReplayReport:
        return self.queue.replay(handler)

    def speak(self, candidate: str,
              facts: Iterable[Mapping[str, Any]] | None = None, *,
              phase: str = "complement", fallback: str = "") -> MouthSpeech:
        return self.mouth.write(candidate, facts, phase=phase,
                                fallback=fallback)

    # ── laços (quem chama é o supervisor, não o quente) ─────────────────────
    def tick_lifecycle(self, *, now: float | None = None) -> list[Any]:
        return self.registry.expire(now=now)

    def tick_cold(self, *, now: float | None = None) -> list[Any]:
        return self.cold.tick(now=now)

    def tick_rules(self) -> bool:
        return self.rules.reload_if_changed()

    def supervisor_tick(self, *, now: float | None = None) -> dict[str, Any]:
        """Um passo do residente: regras, prazos e ciclo de vida, nesta ordem."""
        reloaded = self.tick_rules()
        cold_actions = self.tick_cold(now=now)
        expiries = self.tick_lifecycle(now=now)
        return {"rules_reloaded": reloaded,
                "cold": [a.as_dict() for a in cold_actions],
                "expiries": [e.as_dict() for e in expiries]}

    def close(self) -> None:
        self.cold.stop()
        self.queue.close()


def build(root: str | Path | None = None, *,
          rules_path: str | Path | None = None,
          sink: Callable[[dict[str, Any]], Any] | None = None,
          clock: Callable[[], float] | None = None,
          hot_clock: Callable[[], float] | None = None,
          cold_clock: Callable[[], float] | None = None,
          on_kill: Callable[[Any], None] | None = None,
          signal_fn: Callable[[int, int], None] | None = None,
          start_cold: bool = False) -> FlowOps:
    """Monta as cinco peças. Nada é ligado ao pipeline até alguém chamar.

    ``clock`` é o relógio de parede (registro e fila, padrão ``time.time``);
    ``hot_clock`` mede latência (padrão ``time.perf_counter``) e ``cold_clock``
    é o monotônico do coordenador frio. Separados de propósito: misturar
    relógio de parede com prazo monotônico faz o TTL saltar com ajuste de hora.
    """
    base = Path(root or os.environ.get(ENV_OPS_DIR) or DEFAULT_OPS_DIR)
    base.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    table = RulesTable.from_file(rules_path or DEFAULT_RULES_PATH,
                                 missing_ok=True)
    if not table.source or not Path(table.source).is_file():
        warnings.append(
            f"tabela de regras ausente em {rules_path or DEFAULT_RULES_PATH}; "
            "o quente cai nos defaults")
    registry = LifecycleRegistry(base / "registry", on_kill=on_kill,
                                 signal_fn=signal_fn,
                                 clock=clock or time.time)
    queue = DurableQueue(base / "pending_turns.jsonl",
                         clock=clock or time.time)
    hot = HotDispatcher(table, sink=sink,
                        clock=hot_clock or time.perf_counter)
    cold = ColdCoordinator(clock=cold_clock or time.monotonic,
                           on_action=None)
    ops = FlowOps(root=base, rules=table, registry=registry, queue=queue,
                  hot=hot, cold=cold, warnings=warnings)
    if start_cold:
        cold.start()
    return ops


def attachment_points() -> dict[str, str]:
    """Cópia do mapa de encaixe para relatório/log."""
    return dict(ATTACHMENT_POINTS)
