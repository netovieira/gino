"""NATS bus — POC de comunicacao entre nos via message bus.

FASE4-TASK1: avaliacao de NATS como bus de mensagens entre os 6 nos do sistema
(boca, cerebro, executor, vigilantes, supervisor, memoria).

Esta POC demonstra dois nos (boca e orquestrador) comunicando via NATS com o
padrao request/reply. O objetivo e medir a latencia adicionada pelo bus e
comparar com a comunicacao direta em processo (monolito atual).

ARQUITETURA ATUAL (monolito):
    Orquestrador -> Mouth.brief()      : chamada de funcao direta (~0.01ms)
    Orquestrador -> Executor.run()     : chamada de funcao direta (~0.01ms)
    Orquestrador -> Supervisor.touch() : chamada de funcao direta (~0.01ms)

ARQUITETURA COM NATS:
    Node A -> NATS publish -> NATS server -> NATS subscribe -> Node B
    Node B -> NATS reply     -> NATS server -> NATS receive  -> Node A
    Latencia adicional: round-trip via loopback (~0.1-2ms)

VEREDICTO (documentado no teste):
    A latencia adicionada e desprezivel para mensagens individuais, mas o
    custo operacional (nats-server, serializacao, debug distribuido) nao se
    justifica enquanto o sistema roda em uma unica maquina. A migracao so
    faz sentido quando houver exigencia de multi-maquina ou reinicio isolado
    de nos — cenarios que o plano atual nao exige.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# NATS e dependencia opcional — a POC funciona sem ele (modo simulado).
try:
    import nats
    from nats.aio.client import Client as NatsClient
    HAS_NATS = True
except ImportError:
    HAS_NATS = False

# ── sujeitos do bus ──────────────────────────────────────────────────────────

SUBJECT_BOCA_REQUEST = "gino.boca.request"
SUBJECT_BOCA_REPLY = "gino.boca.reply"
SUBJECT_EXEC_REQUEST = "gino.executor.request"
SUBJECT_EXEC_REPLY = "gino.executor.reply"
SUBJECT_ORCH_EVENT = "gino.orchestrator.event"


@dataclass
class BusMessage:
    """Mensagem padrao trafegada no bus."""
    turn_id: str
    node_from: str
    node_to: str
    kind: str  # "brief", "execute", "cancel", "result"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def to_bytes(self) -> bytes:
        return json.dumps({
            "turn_id": self.turn_id,
            "node_from": self.node_from,
            "node_to": self.node_to,
            "kind": self.kind,
            "payload": self.payload,
            "timestamp": self.timestamp or time.monotonic(),
        }).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> BusMessage:
        d = json.loads(data.decode())
        return cls(
            turn_id=d["turn_id"],
            node_from=d["node_from"],
            node_to=d["node_to"],
            kind=d["kind"],
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", 0.0),
        )


@dataclass
class BusMetrics:
    """Metricas de latencia do bus."""
    messages_sent: int = 0
    messages_received: int = 0
    roundtrip_samples_ms: list[float] = field(default_factory=list)

    @property
    def avg_ms(self) -> float:
        if not self.roundtrip_samples_ms:
            return 0.0
        return sum(self.roundtrip_samples_ms) / len(self.roundtrip_samples_ms)

    @property
    def p50_ms(self) -> float:
        return self._percentile(50)

    @property
    def p99_ms(self) -> float:
        return self._percentile(99)

    @property
    def max_ms(self) -> float:
        return max(self.roundtrip_samples_ms) if self.roundtrip_samples_ms else 0.0

    def _percentile(self, p: int) -> float:
        if not self.roundtrip_samples_ms:
            return 0.0
        s = sorted(self.roundtrip_samples_ms)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s) - 1)]

    def summary(self) -> dict[str, Any]:
        return {
            "sent": self.messages_sent,
            "received": self.messages_received,
            "avg_ms": round(self.avg_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "max_ms": round(self.max_ms, 3),
        }


class NatsBus:
    """Bus de mensagens sobre NATS.

    Suporta dois modos:
    - real: conecta a um nats-server (precisa do servidor rodando)
    - simulado: comunica via asyncio.Queue (sem dependencia externa)

    O modo simulado existe para os testes passarem sem nats-server e para
    isolar o custo do bus do custo da rede.
    """

    def __init__(self, *, url: str = "nats://127.0.0.1:4222",
                 simulated: bool = False):
        self.url = url
        self.simulated = simulated
        self.metrics = BusMetrics()
        self._nc: Any = None  # NatsClient when real
        self._queues: dict[str, asyncio.Queue] = {}  # simulated
        self._subscriptions: dict[str, Callable] = {}
        self._connected = False

    async def connect(self) -> None:
        if self.simulated:
            self._connected = True
            return
        if not HAS_NATS:
            raise RuntimeError("nats-py not installed; use simulated=True")
        self._nc = nats.aio.client.Client()
        await self._nc.connect(self.url, connect_timeout=2.0)
        self._connected = True

    async def disconnect(self) -> None:
        if self.simulated:
            self._connected = False
            return
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def publish(self, subject: str, message: BusMessage) -> None:
        """Publica mensagem no subject."""
        self.metrics.messages_sent += 1
        data = message.to_bytes()
        if self.simulated:
            q = self._queues.get(subject)
            if q is not None:
                await q.put(data)
        else:
            await self._nc.publish(subject, data)

    async def request(self, subject: str, message: BusMessage,
                      *, timeout_s: float = 2.0) -> BusMessage:
        """Request/reply: publica e espera resposta no reply subject."""
        self.metrics.messages_sent += 1
        data = message.to_bytes()
        t0 = time.monotonic()
        if self.simulated:
            # No modo simulado, o handler responde diretamente via queue.
            reply_subject = f"{subject}.reply.{message.turn_id}"
            reply_queue: asyncio.Queue = asyncio.Queue()
            self._queues[reply_subject] = reply_queue
            # Publica a request.
            req_queue = self._queues.get(subject)
            if req_queue is not None:
                await req_queue.put((data, reply_subject))
            # Espera a reply.
            try:
                reply_data = await asyncio.wait_for(
                    reply_queue.get(), timeout=timeout_s)
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.metrics.roundtrip_samples_ms.append(elapsed_ms)
                self.metrics.messages_received += 1
                return BusMessage.from_bytes(reply_data)
            finally:
                self._queues.pop(reply_subject, None)
        else:
            reply = await self._nc.request(subject, data, timeout=timeout_s)
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.metrics.roundtrip_samples_ms.append(elapsed_ms)
            self.metrics.messages_received += 1
            return BusMessage.from_bytes(reply.data)

    async def subscribe(self, subject: str,
                        handler: Callable[[BusMessage], Any]) -> None:
        """Registra handler para um subject."""
        self._subscriptions[subject] = handler
        if self.simulated:
            if subject not in self._queues:
                self._queues[subject] = asyncio.Queue()
            # Inicia task que consome a fila e chama o handler.
            asyncio.ensure_future(self._consume_simulated(subject, handler))
        else:
            await self._nc.subscribe(subject, cb=lambda msg: asyncio.ensure_future(
                self._handle_real(msg, handler)))

    async def _consume_simulated(self, subject: str,
                                  handler: Callable) -> None:
        q = self._queues[subject]
        while self._connected:
            try:
                item = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if isinstance(item, tuple):
                # Request/reply: (data, reply_subject)
                data, reply_subject = item
                msg = BusMessage.from_bytes(data)
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    result = await result
                # Se o handler retornou uma BusMessage, envia como reply.
                if isinstance(result, BusMessage):
                    reply_q = self._queues.get(reply_subject)
                    if reply_q is not None:
                        await reply_q.put(result.to_bytes())
            else:
                # Publish simples.
                msg = BusMessage.from_bytes(item)
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    await result
            self.metrics.messages_received += 1

    async def _handle_real(self, msg, handler: Callable) -> None:
        bus_msg = BusMessage.from_bytes(msg.data)
        result = handler(bus_msg)
        if asyncio.iscoroutine(result):
            await result
        if isinstance(result, BusMessage) and msg.reply:
            await self._nc.publish(msg.reply, result.to_bytes())


# ── nos simulados (POC: boca e orquestrador) ────────────────────────────────

class BocaNode:
    """No 'boca' — recebe pedidos de fala via NATS e devolve frases."""

    def __init__(self, bus: NatsBus):
        self.bus = bus
        self.calls = 0

    async def start(self) -> None:
        await self.bus.subscribe(SUBJECT_BOCA_REQUEST, self._handle)

    async def _handle(self, msg: BusMessage) -> BusMessage:
        self.calls += 1
        text = msg.payload.get("text", "")
        kind = msg.kind
        # Simula o processamento da boca (sem modelo real — so mede o bus).
        if kind == "brief":
            sentence = f"Certo, criador."
        elif kind == "complement":
            facts = msg.payload.get("facts", [])
            spoken = facts[0].get("spoken", "acao concluida") if facts else "feito"
            sentence = spoken
        else:
            sentence = f"Ouvi: {text[:40]}"
        return BusMessage(
            turn_id=msg.turn_id,
            node_from="boca",
            node_to="orchestrator",
            kind="reply",
            payload={"sentence": sentence},
        )


class OrchestratorNode:
    """No 'orquestrador' — envia pedidos para a boca via NATS."""

    def __init__(self, bus: NatsBus):
        self.bus = bus

    async def send_brief(self, turn_id: str, text: str) -> dict[str, Any]:
        msg = BusMessage(
            turn_id=turn_id,
            node_from="orchestrator",
            node_to="boca",
            kind="brief",
            payload={"text": text},
        )
        reply = await self.bus.request(SUBJECT_BOCA_REQUEST, msg)
        return reply.payload

    async def send_complement(self, turn_id: str, text: str,
                               facts: list[dict]) -> dict[str, Any]:
        msg = BusMessage(
            turn_id=turn_id,
            node_from="orchestrator",
            node_to="boca",
            kind="complement",
            payload={"text": text, "facts": facts},
        )
        reply = await self.bus.request(SUBJECT_BOCA_REQUEST, msg)
        return reply.payload


# ── comparador: monolito vs bus ──────────────────────────────────────────────

def monolith_brief(text: str) -> dict[str, str]:
    """Simula a chamada direta (monolito) — sem bus, sem serializacao."""
    return {"sentence": f"Certo, criador."}


def monolith_complement(text: str, facts: list[dict]) -> dict[str, str]:
    """Simula a chamada direta (monolito) — sem bus, sem serializacao."""
    spoken = facts[0].get("spoken", "acao concluida") if facts else "feito"
    return {"sentence": spoken}


async def benchmark_bus(n: int = 100) -> dict[str, Any]:
    """Compara latencia monolito vs NATS (simulado) em n iteracoes."""
    # ── monolito (baseline) ──────────────────────────────────────────────
    mono_times: list[float] = []
    for i in range(n):
        t0 = time.monotonic()
        monolith_brief(f"teste {i}")
        mono_times.append((time.monotonic() - t0) * 1000)

    # ── bus simulado ─────────────────────────────────────────────────────
    bus = NatsBus(simulated=True)
    await bus.connect()
    boca = BocaNode(bus)
    orch = OrchestratorNode(bus)
    await boca.start()

    bus_times: list[float] = []
    for i in range(n):
        t0 = time.monotonic()
        await orch.send_brief(f"turn-{i}", f"teste {i}")
        bus_times.append((time.monotonic() - t0) * 1000)

    await bus.disconnect()

    mono_avg = sum(mono_times) / len(mono_times) if mono_times else 0
    bus_avg = sum(bus_times) / len(bus_times) if bus_times else 0
    overhead = bus_avg - mono_avg

    return {
        "iterations": n,
        "monolith_avg_ms": round(mono_avg, 4),
        "bus_avg_ms": round(bus_avg, 4),
        "overhead_ms": round(overhead, 4),
        "overhead_factor": round(bus_avg / mono_avg, 1) if mono_avg > 0 else 0,
        "within_10ms": overhead < 10.0,
    }
