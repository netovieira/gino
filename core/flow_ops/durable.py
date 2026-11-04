"""Fila durável de turnos pendentes, com replay idempotente.

O orquestrador deixa de ser o único lugar onde um turno pendente existe. Cada
transição vira uma linha append-only (JSONL) sob o diretório do projeto::

    {"op":"enqueue","turn_id":"t1","payload":{...},"t":...}
    {"op":"claim","turn_id":"t1","t":...}
    {"op":"ack","turn_id":"t1","t":...}
    {"op":"release","turn_id":"t1","error":"...","t":...}

Semântica de replay após queda:

* turno que ficou ``enqueue`` sem ``claim`` volta **uma vez** no próximo replay
  e é marcado ``claim`` antes de o handler rodar — dois replays concorrentes não
  executam o mesmo turno duas vezes;
* turno já ``claim`` e sem ``ack`` **não** volta (at-most-once): quem reivindicou
  pode ter executado a ação; repetir seria pior que perder. Isso é deliberado e
  está coberto por teste;
* ``ack`` encerra o turno para sempre; ``enqueue`` duplicado do mesmo turn_id é
  ignorado (idempotente), inclusive depois do ``ack``;
* handler que levanta exceção gera ``release`` e o turno volta no *próximo*
  replay, registrado em ``last_error`` — não é queda, é falha visível.

A gravação é ``flush`` + ``fsync`` antes de devolver sucesso. O arquivo é
reconstruído inteiro a cada mudança de tamanho, então uma linha truncada por
queda no meio do append não apaga as anteriores (a última linha incompleta é
descartada).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

PENDING = "pending"
CLAIMED = "claimed"
ACKED = "acked"
_STATUSES = (PENDING, CLAIMED, ACKED)


class DurableError(ValueError):
    """Arquivo da fila ilegível ou operação inconsistente."""


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = PENDING
    attempts: int = 0
    enqueued_at: float = 0.0
    claimed_at: float = 0.0
    acked_at: float = 0.0
    last_error: str = ""

    @property
    def pending(self) -> bool:
        return self.status == PENDING

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id, "payload": dict(self.payload),
            "status": self.status, "attempts": self.attempts,
            "enqueued_at": self.enqueued_at, "claimed_at": self.claimed_at,
            "acked_at": self.acked_at, "last_error": self.last_error,
        }


@dataclass
class ReplayReport:
    replayed: int = 0
    acked: int = 0
    failed: int = 0
    skipped: int = 0
    turn_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"replayed": self.replayed, "acked": self.acked,
                "failed": self.failed, "skipped": self.skipped,
                "turn_ids": list(self.turn_ids)}


class DurableQueue:
    """Fila append-only com um escritor e replay sequencial idempotente."""

    def __init__(self, path: str | Path, *,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._turns: dict[str, TurnRecord] = {}
        self._order: list[str] = []
        self._size = -1
        self._fh: TextIO | None = None
        self.corrupt_lines = 0

    # ── leitura/reconstrução ────────────────────────────────────────────────
    def _refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size == self._size:
            return
        self._turns.clear()
        self._order.clear()
        self.corrupt_lines = 0
        if size == 0:
            self._size = 0
            return
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise DurableError(f"não consegui ler a fila {self.path}: {exc}") \
                from None
        lines = raw.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        elif lines and not raw.endswith(b"\n"):
            # linha final truncada por queda: descarta só ela
            lines.pop()
        for index, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                op = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self.corrupt_lines += 1
                continue
            if isinstance(op, dict):
                self._apply(op)
            else:
                self.corrupt_lines += 1
        self._size = size

    def _apply(self, op: Mapping[str, Any]) -> None:
        kind = str(op.get("op") or "")
        turn_id = str(op.get("turn_id") or "")
        if not turn_id:
            self.corrupt_lines += 1
            return
        try:
            ts = float(op.get("t") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        current = self._turns.get(turn_id)
        if kind == "enqueue":
            if current is not None:
                return
            payload = op.get("payload")
            record = TurnRecord(
                turn_id=turn_id,
                payload=dict(payload) if isinstance(payload, dict) else {},
                status=PENDING, enqueued_at=ts)
            self._turns[turn_id] = record
            self._order.append(turn_id)
        elif current is None:
            return
        elif kind == "claim":
            if current.status == PENDING:
                self._turns[turn_id] = _replace(
                    current, status=CLAIMED, attempts=current.attempts + 1,
                    claimed_at=ts)
        elif kind == "ack":
            if current.status != ACKED:
                self._turns[turn_id] = _replace(
                    current, status=ACKED, acked_at=ts)
        elif kind == "release":
            if current.status == CLAIMED:
                self._turns[turn_id] = _replace(
                    current, status=PENDING,
                    last_error=str(op.get("error") or "")[:300])
        # ops desconhecidas: ignoradas de propósito (arquivo de versão futura)

    # ── escrita ─────────────────────────────────────────────────────────────
    def _append(self, op: Mapping[str, Any]) -> None:
        payload = dict(op)
        payload.setdefault("t", self.clock())
        line = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")) + "\n"
        if self._fh is None:
            self._fh = open(self.path, "a", encoding="utf-8")
        # queda no meio de um append deixa a última linha truncada; sem este
        # newline a próxima operação colaria nela e o turno novo se perderia.
        try:
            if self.path.stat().st_size:
                with open(self.path, "rb") as tail:
                    tail.seek(-1, 2)
                    if tail.read(1) != b"\n":
                        self._fh.write("\n")
                        self._fh.flush()
        except OSError:
            pass
        self._fh.write(line)
        self._fh.flush()
        os.fsync(self._fh.fileno())
        try:
            self._size = self.path.stat().st_size
        except OSError:
            self._size = -1
        self._apply(payload)

    # ── API ─────────────────────────────────────────────────────────────────
    def enqueue(self, turn_id: str, payload: Mapping[str, Any] | None = None
                ) -> bool:
        """Enfileira uma vez; duplicata do mesmo turno devolve False."""
        tid = str(turn_id or "").strip()
        if not tid:
            raise DurableError("turno sem id não entra na fila")
        with self._lock:
            self._refresh()
            if tid in self._turns:
                return False
            self._append({"op": "enqueue", "turn_id": tid,
                          "payload": dict(payload or {})})
            return True

    def pending(self) -> list[TurnRecord]:
        with self._lock:
            self._refresh()
            return [_copy(self._turns[tid]) for tid in self._order
                    if self._turns[tid].status == PENDING]

    def get(self, turn_id: str) -> TurnRecord | None:
        with self._lock:
            self._refresh()
            record = self._turns.get(str(turn_id))
            return _copy(record) if record is not None else None

    def claim(self, turn_id: str) -> TurnRecord | None:
        """Marca reivindicado antes do handler; None se não está pendente."""
        tid = str(turn_id or "")
        with self._lock:
            self._refresh()
            record = self._turns.get(tid)
            if record is None or record.status != PENDING:
                return None
            self._append({"op": "claim", "turn_id": tid})
            return _copy(self._turns[tid])

    def ack(self, turn_id: str) -> bool:
        tid = str(turn_id or "")
        with self._lock:
            self._refresh()
            record = self._turns.get(tid)
            if record is None or record.status == ACKED:
                return False
            self._append({"op": "ack", "turn_id": tid})
            return True

    def release(self, turn_id: str, error: str = "") -> bool:
        tid = str(turn_id or "")
        with self._lock:
            self._refresh()
            record = self._turns.get(tid)
            if record is None or record.status != CLAIMED:
                return False
            self._append({"op": "release", "turn_id": tid,
                          "error": str(error or "")})
            return True

    def replay(self, handler: Callable[[dict[str, Any]], Any],
               *, limit: int | None = None) -> ReplayReport:
        """Executa cada turno pendente uma vez, na ordem de chegada."""
        report = ReplayReport()
        with self._lock:
            self._refresh()
            pending_ids = [tid for tid in self._order
                           if self._turns[tid].status == PENDING]
            if limit is not None:
                pending_ids = pending_ids[:max(0, int(limit))]
            for tid in pending_ids:
                claimed = self.claim(tid)
                if claimed is None:
                    report.skipped += 1
                    continue
                report.replayed += 1
                report.turn_ids.append(tid)
                try:
                    handled = handler(dict(claimed.payload))
                except Exception as exc:
                    self.release(tid, f"{type(exc).__name__}: {exc}")
                    report.failed += 1
                    continue
                if handled is False:
                    self.release(tid, "handler devolveu False")
                    report.failed += 1
                    continue
                self.ack(tid)
                report.acked += 1
        return report

    # ── manutenção ──────────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            counts = {status: 0 for status in _STATUSES}
            for record in self._turns.values():
                counts[record.status] = counts.get(record.status, 0) + 1
            return {"path": str(self.path), "turns": len(self._turns),
                    **counts, "corrupt_lines": self.corrupt_lines,
                    "pending_ids": [tid for tid in self._order
                                    if self._turns[tid].status == PENDING]}

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None

    def __enter__(self) -> "DurableQueue":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __len__(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._turns)


def _replace(record: TurnRecord, **changes: Any) -> TurnRecord:
    data = {**record.__dict__, **changes}
    return TurnRecord(**data)


def _copy(record: TurnRecord) -> TurnRecord:
    return TurnRecord(**{**record.__dict__, "payload": dict(record.payload)})
