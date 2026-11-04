"""Coordenador frio: prazo, retentativa, retrabalho, confirmação e timer.

Tudo o que pode atrasar mora aqui, fora do caminho da primeira frase. O quente
entrega e esquece; o frio acompanha:

* **prazo** — cada turno tem ``deadline_ms`` vindo da regra. Vencido, o turno
  vira ``retry`` (até ``retry_limit``) ou ``expire``;
* **retentativa** — com backoff linear (``backoff_ms * tentativa``);
* **retrabalho** — :meth:`ColdCoordinator.rework` devolve o turno à fila com
  prazo novo e historico;
* **confirmação pendente** — turno marcado ``requires_confirm`` nunca executa
  sozinho. Ele espera a frase exata, confirmada pelo gate de voz
  (``verified_by_gate=True``). O frio só devolve a autorização; quem executa é
  o executor, depois do gate. Confirmação vencida vira ``confirm_expired``;
* **timer** — :meth:`tick` é chamado por um laço (thread opcional) e devolve as
  ações; o frio não chama ferramenta nem fala.

O relógio é injetável: o teste usa :meth:`tick` com tempo falso e não dorme.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .envelope import Envelope, decode, encode
from .mouth_guard import norm

PENDING = "pending"
DISPATCHED = "dispatched"
AWAITING_CONFIRM = "awaiting_confirm"
RETRYING = "retrying"
CONFIRMED = "confirmed"
DONE = "done"
CANCELLED = "cancelled"
EXPIRED = "expired"
CONFIRM_EXPIRED = "confirm_expired"

_DEFAULT_BACKOFF_MS = 400
_DEFAULT_CONFIRM_TTL_MS = 60_000


@dataclass
class ColdTask:
    turn_id: str
    flow: str = "reasoning"
    action: str = "reason"
    deadline_ms: int = 8000
    requires_confirm: bool = False
    state: str = DISPATCHED
    attempts: int = 0
    max_attempts: int = 3
    created_at: float = 0.0
    deadline_at: float = 0.0
    retry_at: float = 0.0
    confirm_at: float = 0.0
    confirm_ttl_ms: int = _DEFAULT_CONFIRM_TTL_MS
    phrase: str = ""
    last_reason: str = ""
    history: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id, "flow": self.flow, "action": self.action,
            "deadline_ms": self.deadline_ms,
            "requires_confirm": self.requires_confirm, "state": self.state,
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "created_at": self.created_at, "deadline_at": self.deadline_at,
            "retry_at": self.retry_at, "confirm_at": self.confirm_at,
            "phrase": self.phrase,
            "last_reason": self.last_reason,
            "history": list(self.history),
        }


@dataclass(frozen=True)
class ColdAction:
    kind: str  # retry | expire | confirm_expired
    turn_id: str
    reason: str = ""
    attempt: int = 0
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "turn_id": self.turn_id,
                "reason": self.reason, "attempt": self.attempt, "at": self.at}


class ColdCoordinator:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 retry_limit: int = 2, backoff_ms: int = _DEFAULT_BACKOFF_MS,
                 confirm_ttl_ms: int = _DEFAULT_CONFIRM_TTL_MS,
                 on_action: Callable[[ColdAction], None] | None = None):
        self.clock = clock
        self.retry_limit = max(0, int(retry_limit))
        self.backoff_ms = max(0, int(backoff_ms))
        self.confirm_ttl_ms = max(0, int(confirm_ttl_ms))
        self.on_action = on_action
        self._tasks: dict[str, ColdTask] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.retries = 0
        self.expired = 0
        self.confirms = 0

    # ── entrada ─────────────────────────────────────────────────────────────
    def track(self, decision: Any, *, now: float | None = None) -> ColdTask:
        """Acompanha uma decisão do quente. Não bloqueia e não executa."""
        now = self.clock() if now is None else float(now)
        data = _as_mapping(decision)
        turn_id = str(data["turn_id"])
        deadline_ms = int(data.get("deadline_ms") or 0)
        requires_confirm = bool(data.get("requires_confirm"))
        task = ColdTask(
            turn_id=turn_id, flow=str(data.get("flow") or "reasoning"),
            action=str(data.get("action") or "reason"),
            deadline_ms=deadline_ms, requires_confirm=requires_confirm,
            state=AWAITING_CONFIRM if requires_confirm else DISPATCHED,
            attempts=0, max_attempts=self.retry_limit + 1, created_at=now,
            deadline_at=now + deadline_ms / 1000.0,
            confirm_at=now if requires_confirm else 0.0,
            confirm_ttl_ms=self.confirm_ttl_ms,
            phrase=str(data.get("phrase") or ""),
            history=["track"])
        with self._lock:
            self._tasks[turn_id] = task
        return task

    def track_envelope(self, event: str | Mapping[str, Any] | Envelope,
                       *, now: float | None = None) -> ColdTask:
        """Recebe o contrato versionado do quente e acompanha o turno.

        É a fronteira entre as duas metades: versão incompatível levanta
        :class:`~core.flow_ops.envelope.SchemaError` com a mensagem clara do
        receptor, em vez de virar turno meio interpretado.
        """
        if isinstance(event, str):
            envelope = decode(event)
        elif isinstance(event, Envelope):
            envelope = event
        elif isinstance(event, Mapping):
            envelope = decode(encode(event))
        else:
            raise TypeError(f"evento não suportado: {type(event).__name__}")
        if envelope.type == "monitor_event":
            raise ValueError("monitor_event nunca vira turno do coordenador")
        data = dict(envelope.data)
        data["turn_id"] = envelope.id
        data.setdefault("flow", "reasoning")
        data.setdefault("action", "reason")
        return self.track(data, now=now)

    def await_confirm(self, turn_id: str, phrase: str,
                      *, now: float | None = None) -> bool:
        """Registra a frase exata que o gate de voz vai exigir."""
        now = self.clock() if now is None else float(now)
        with self._lock:
            task = self._tasks.get(str(turn_id))
            if task is None or task.state in (DONE, CANCELLED, EXPIRED):
                return False
            task.state = AWAITING_CONFIRM
            task.requires_confirm = True
            task.phrase = str(phrase or "").strip()
            task.confirm_at = now
            task.confirm_ttl_ms = self.confirm_ttl_ms
            task.history.append("await_confirm")
            return True

    def confirm(self, turn_id: str, phrase: str, *,
                verified_by_gate: bool = False,
                now: float | None = None) -> bool:
        """Só autoriza com frase exata E gate de voz. Nunca executa."""
        now = self.clock() if now is None else float(now)
        with self._lock:
            task = self._tasks.get(str(turn_id))
            if task is None or task.state != AWAITING_CONFIRM:
                return False
            if not verified_by_gate:
                return False
            if not task.phrase or norm(phrase) != norm(task.phrase):
                return False
            task.state = CONFIRMED
            task.history.append("confirm")
            self.confirms += 1
            return True

    # ── andamento ───────────────────────────────────────────────────────────
    def ack(self, turn_id: str, *, now: float | None = None) -> bool:
        with self._lock:
            task = self._tasks.get(str(turn_id))
            if task is None or task.state in (DONE, CANCELLED, EXPIRED):
                return False
            task.state = DONE
            task.history.append("ack")
            return True

    def rework(self, turn_id: str, reason: str = "retrabalho", *,
               now: float | None = None) -> bool:
        now = self.clock() if now is None else float(now)
        with self._lock:
            task = self._tasks.get(str(turn_id))
            if task is None or task.state in (DONE, CANCELLED, EXPIRED):
                return False
            task.state = DISPATCHED
            task.last_reason = str(reason)
            task.deadline_at = now + task.deadline_ms / 1000.0
            task.history.append(f"rework:{reason}")
            return True

    def cancel(self, turn_id: str, reason: str = "cancelado") -> bool:
        with self._lock:
            task = self._tasks.get(str(turn_id))
            if task is None or task.state in (DONE, CANCELLED, EXPIRED):
                return False
            task.state = CANCELLED
            task.last_reason = str(reason)
            task.history.append(f"cancel:{reason}")
            return True

    def forget(self, turn_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(str(turn_id), None) is not None

    # ── timer ───────────────────────────────────────────────────────────────
    def tick(self, *, now: float | None = None) -> list[ColdAction]:
        """Vencimentos do instante. Devolve ações; quem as aplica é o chamador."""
        now = self.clock() if now is None else float(now)
        actions: list[ColdAction] = []
        with self._lock:
            for task in list(self._tasks.values()):
                action = self._tick_one(task, now)
                if action is not None:
                    actions.append(action)
        for action in actions:
            if self.on_action is not None:
                try:
                    self.on_action(action)
                except Exception:
                    pass
        return actions

    def _tick_one(self, task: ColdTask, now: float) -> ColdAction | None:
        if task.state in (DONE, CANCELLED, EXPIRED, CONFIRM_EXPIRED,
                          CONFIRMED, PENDING):
            return None
        if task.state == AWAITING_CONFIRM:
            if self.confirm_ttl_ms and now - task.confirm_at > \
                    self.confirm_ttl_ms / 1000.0:
                task.state = CONFIRM_EXPIRED
                task.last_reason = "confirmacao vencida"
                task.history.append("confirm_expired")
                self.expired += 1
                return ColdAction(kind="confirm_expired",
                                  turn_id=task.turn_id,
                                  reason=task.last_reason, at=now)
            return None
        if task.state == RETRYING:
            if now < task.retry_at:
                return None
            task.state = DISPATCHED
            task.deadline_at = now + task.deadline_ms / 1000.0
            task.history.append("retry_sent")
            return ColdAction(kind="retry", turn_id=task.turn_id,
                              reason=task.last_reason, attempt=task.attempts,
                              at=now)
        if task.state == DISPATCHED and now >= task.deadline_at:
            task.last_reason = "prazo vencido"
            if task.attempts < self.retry_limit:
                task.attempts += 1
                task.state = RETRYING
                task.retry_at = now + (self.backoff_ms * task.attempts) / 1000.0
                task.deadline_at = task.retry_at + task.deadline_ms / 1000.0
                task.history.append(f"retry:{task.attempts}")
                self.retries += 1
                return None
            task.state = EXPIRED
            task.history.append("expire")
            self.expired += 1
            return ColdAction(kind="expire", turn_id=task.turn_id,
                              reason=task.last_reason, attempt=task.attempts,
                              at=now)
        return None

    def start(self, interval_s: float = 0.05) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(max(0.005, float(interval_s)),),
            name="flow-cold", daemon=True)
        self._thread.start()

    def _loop(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.tick()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None

    # ── leitura ─────────────────────────────────────────────────────────────
    def get(self, turn_id: str) -> ColdTask | None:
        with self._lock:
            return self._tasks.get(str(turn_id))

    def active(self) -> list[ColdTask]:
        with self._lock:
            return [t for t in self._tasks.values()
                    if t.state not in (DONE, CANCELLED, EXPIRED,
                                       CONFIRM_EXPIRED)]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"tasks": {tid: t.as_dict() for tid, t in
                              self._tasks.items()},
                    "retries": self.retries, "expired": self.expired,
                    "confirms": self.confirms}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {"tracked": len(self._tasks),
                    "active": len(self.active()),
                    "retries": self.retries, "expired": self.expired,
                    "confirms": self.confirms}


def _as_mapping(decision: Any) -> Mapping[str, Any]:
    if isinstance(decision, Mapping):
        return decision
    if hasattr(decision, "__dict__"):
        return vars(decision)
    if hasattr(decision, "as_dict"):
        return decision.as_dict()
    raise TypeError(f"decisão não suportada: {type(decision).__name__}")
