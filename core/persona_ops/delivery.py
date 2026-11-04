"""Arbítrio de entrega do recado de uma persona.

O alto-falante desta máquina é **recurso único**. Quem fala na sala é decidido
aqui, em código, e não por convenção de prompt:

* recado PARA O DONO -> a boca da persona assume, no canal dela, e a fala entra
  na fila única do :class:`SpeakerArbiter` (o orquestrador da casa arbitra);
* recado PARA O GINO -> evento interno mudo: ``spoken=False``, sem TTS;
* recado interno (monitor, log, memória) -> também mudo.

A decisão é um :class:`DeliveryDecision` serializável e auditável; o
``SpeakerArbiter`` guarda a ordem, a origem e o resultado de cada fala. Ele não
abre processo, não conhece modelo e não decide conteúdo: só entrega o texto que
a persona já produziu.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class Addressee(str, Enum):
    OWNER = "owner"
    GINO = "gino"
    INTERNAL = "internal"


def parse_addressee(value: Any, *, default: Addressee = Addressee.GINO
                    ) -> Addressee:
    if isinstance(value, Addressee):
        return value
    key = str(value or "").strip().casefold()
    for item in Addressee:
        if item.value == key:
            return item
    aliases = {
        "dono": Addressee.OWNER, "criador": Addressee.OWNER,
        "owner": Addressee.OWNER,
        "gino": Addressee.GINO, "assistente": Addressee.GINO,
        "interno": Addressee.INTERNAL, "internal": Addressee.INTERNAL,
    }
    return aliases.get(key, default)


@dataclass(frozen=True)
class DeliveryDecision:
    persona: str
    addressee: Addressee
    channel: str
    spoken: bool
    speaker: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "addressee": self.addressee.value,
            "channel": self.channel,
            "spoken": self.spoken,
            "speaker": self.speaker,
            "reason": self.reason,
        }


def decide_delivery(persona: str, addressee: Any, *,
                    channel: str = "voice") -> DeliveryDecision:
    """Decisão pura: dono ouve a persona; Gino recebe evento mudo."""
    who = parse_addressee(addressee)
    slug = str(persona or "").strip()
    chan = str(channel or "voice").strip().casefold() or "voice"
    if who is Addressee.OWNER:
        if chan == "whatsapp":
            # O canal dela é a conta de WhatsApp da persona; não é o alto-falante
            # da sala. A entrega de texto/áudio é feita pelo canal, não pelo TTS.
            return DeliveryDecision(slug, who, "persona_whatsapp", False,
                                    "persona", "recado_para_o_dono_no_canal_dela")
        return DeliveryDecision(slug, who, "persona_voice", True, "persona",
                                "recado_para_o_dono_na_voz_dela")
    return DeliveryDecision(
        slug, who, "internal", False, "none",
        "evento_interno_mudo_para_o_gino" if who is Addressee.GINO
        else "evento_interno_mudo")


@dataclass(frozen=True)
class SpeechTicket:
    seq: int
    owner: str
    persona: str
    text: str
    priority: int
    submitted_at: float
    decision: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "owner": self.owner, "persona": self.persona,
            "text": self.text, "priority": self.priority,
            "submitted_at": self.submitted_at,
            "decision": dict(self.decision),
        }


class SpeakerArbiter:
    """Fila única de fala da casa. Uma boca por vez, nesta ordem de prioridade.

    ``speak`` recebe o :class:`SpeechTicket` e faz o trabalho real (no pipeline
    vivo, ``_say_now``). O arbiter nunca fala sozinho: sem ``speak`` a fala é
    descartada e auditada. ``submit`` já drena por padrão; ``drain`` sozinho
    existe para testes e para quem quiser segurar a fila (ex.: modo silêncio).
    """

    def __init__(self, speak: Callable[[SpeechTicket], Any],
                 *, clock: Callable[[], float] = time.monotonic,
                 audit: Callable[[dict[str, Any]], None] | None = None,
                 max_pending: int = 64,
                 log: Callable[[str], None] | None = None):
        self.speak = speak
        self.clock = clock
        self.audit = audit
        self.max_pending = max(1, int(max_pending))
        self.log = log or (lambda _msg: None)
        self._lock = threading.RLock()
        self._drain_lock = threading.Lock()
        self._pending: list[SpeechTicket] = []
        self._seq = 0
        self.submitted = 0
        self.spoken = 0
        self.failed = 0
        self.dropped = 0
        self.last_spoken: str = ""

    def _event(self, payload: dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit(payload)
        except Exception:
            pass

    def submit(self, text: str, *, owner: str, persona: str = "",
               priority: int = 0, decision: Mapping[str, Any] | None = None,
               drain: bool = True) -> SpeechTicket | None:
        body = str(text or "").strip()
        if not body:
            return None
        with self._lock:
            self._seq += 1
            ticket = SpeechTicket(
                seq=self._seq, owner=str(owner or "persona"),
                persona=str(persona or ""), text=body,
                priority=int(priority), submitted_at=float(self.clock()),
                decision=dict(decision or {}))
            if len(self._pending) >= self.max_pending:
                self.dropped += 1
                self._event({"event": "speech_dropped", **ticket.as_dict(),
                             "reason": "fila_cheia"})
                return None
            self._pending.append(ticket)
            self._pending.sort(key=lambda item: (-item.priority, item.seq))
            self.submitted += 1
            self._event({"event": "speech_queued", **ticket.as_dict()})
        if drain:
            self.drain()
        return ticket

    def drain(self, limit: int | None = None) -> list[SpeechTicket]:
        out: list[SpeechTicket] = []
        remaining = None if limit is None else max(0, int(limit))
        # Uma boca por vez: dois chamadores não podem sintetizar em paralelo.
        with self._drain_lock:
            while remaining is None or len(out) < remaining:
                with self._lock:
                    if not self._pending:
                        break
                    ticket = self._pending.pop(0)
                try:
                    self.speak(ticket)
                    self.spoken += 1
                    self.last_spoken = ticket.text
                    self._event({"event": "speech_spoken",
                                 **ticket.as_dict()})
                except Exception as exc:
                    self.failed += 1
                    self._event({"event": "speech_failed",
                                 **ticket.as_dict(),
                                 "error": f"{type(exc).__name__}: {exc}"})
                    self.log(f"[Persona] fala falhou: {exc!r}")
                out.append(ticket)
        return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pending": len(self._pending),
                "submitted": self.submitted,
                "spoken": self.spoken,
                "failed": self.failed,
                "dropped": self.dropped,
                "last_spoken": self.last_spoken,
                "next": [t.as_dict() for t in self._pending],
            }


class AuditLog:
    """JSONL append-only para a decisão de entrega; nunca guarda segredo."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def __call__(self, payload: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        line = json.dumps(dict(payload), ensure_ascii=False,
                          separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
