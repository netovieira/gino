"""Ciclo de vida dos fluxos FORA do orquestrador.

Cada fluxo grava o próprio batimento (``<root>/<flow>.json``, escrita atômica).
O supervisor é dono do registro: ele nasce fluxo, aplica TTL/orçamento, marca
morto e chama o gancho de matar. O orquestrador **não** rastreia vivacidade: lê
apenas :class:`LifecycleSnapshot`, uma fotografia imutável montada sob demanda.

Por que arquivo e não memória: se o orquestrador cair, o registro continua no
disco e o supervisor que reiniciar ainda sabe quem estava vivo, desde quando e
com que prazo. O batimento é do fluxo, não de quem o observa — um fluxo travado
para de bater e expira sozinho, sem ninguém precisar notar.

O ``kill`` é uma marcação + gancho (``on_kill``). Este módulo não envia sinal de
processo por conta própria; quem pluga passa ``signal_fn`` (por exemplo
``os.kill``) e o PID do fluxo. Sem ``signal_fn``, o registro é atualizado e o
gancho loga — comportamento de stub declarado, não de assassino silencioso.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

DEFAULT_TTL_S = 0.0  # 0 = sem prazo (só o orçamento ou o dono matam)

_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def safe_flow_id(flow_id: str) -> str:
    value = _ID_RE.sub("_", str(flow_id or "").strip())
    value = value.strip("._")
    if not value or value in (".", ".."):
        raise ValueError(f"id de fluxo inválido: {flow_id!r}")
    return value[:96]


@dataclass
class FlowRecord:
    flow_id: str
    kind: str = "generic"
    pid: int = 0
    ttl_s: float = DEFAULT_TTL_S
    budget_tokens: int = 0
    used_tokens: int = 0
    created_at: float = 0.0
    last_beat: float = 0.0
    seq: int = 0
    killable: bool = True
    killed_at: float = 0.0
    kill_reason: str = ""
    noticed_at: float = 0.0
    state: dict[str, Any] = field(default_factory=dict)

    def age_s(self, now: float) -> float:
        return max(0.0, float(now) - float(self.last_beat or self.created_at))

    def budget_exceeded(self) -> bool:
        return bool(self.budget_tokens) and self.used_tokens > self.budget_tokens

    def ttl_exceeded(self, now: float) -> bool:
        return bool(self.ttl_s) and self.age_s(now) > float(self.ttl_s)

    def alive(self, now: float) -> bool:
        if self.killed_at:
            return False
        if self.ttl_exceeded(now):
            return False
        if self.budget_exceeded():
            return False
        return True

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {
            "flow_id": self.flow_id, "kind": self.kind, "pid": self.pid,
            "ttl_s": self.ttl_s, "budget_tokens": self.budget_tokens,
            "used_tokens": self.used_tokens, "created_at": self.created_at,
            "last_beat": self.last_beat, "age_s": round(self.age_s(now), 3),
            "seq": self.seq, "killable": self.killable,
            "killed_at": self.killed_at, "kill_reason": self.kill_reason,
            "alive": self.alive(now), "state": dict(self.state),
        }


@dataclass(frozen=True)
class Expiry:
    flow_id: str
    reason: str
    action: str = "kill"  # kill | compact
    pid: int = 0
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"flow_id": self.flow_id, "reason": self.reason,
                "action": self.action, "pid": self.pid, "at": self.at}


@dataclass(frozen=True)
class LifecycleSnapshot:
    """Fotografia imutável do registro, tudo que o orquestrador pode ver."""

    flows: Mapping[str, dict[str, Any]]
    taken_at: float

    def get(self, flow_id: str) -> dict[str, Any] | None:
        return self.flows.get(safe_flow_id(flow_id))

    def alive(self, flow_id: str) -> bool:
        record = self.get(flow_id)
        return bool(record and record.get("alive"))

    def expired(self) -> list[str]:
        return [fid for fid, rec in self.flows.items() if not rec.get("alive")]

    def as_dict(self) -> dict[str, Any]:
        return {"taken_at": self.taken_at,
                "flows": {fid: dict(rec) for fid, rec in self.flows.items()}}

    def __len__(self) -> int:
        return len(self.flows)


class LifecycleRegistry:
    """Dono do registro: nasce, expira e mata. Nunca é consultado pelo quente."""

    def __init__(self, root: str | Path, *,
                 clock: Callable[[], float] = time.time,
                 on_kill: Callable[[Expiry], None] | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None,
                 signal_fn: Callable[[int, int], None] | None = None,
                 kill_signal: int = 15):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.on_kill = on_kill
        self.on_event = on_event
        self.signal_fn = signal_fn
        self.kill_signal = int(kill_signal)
        self._lock = threading.RLock()
        self._killed: set[str] = set()
        self._notified: set[str] = set()

    # ── registro ────────────────────────────────────────────────────────────
    def _path(self, flow_id: str) -> Path:
        return self.root / f"{safe_flow_id(flow_id)}.json"

    def _read(self, flow_id: str) -> FlowRecord | None:
        target = self._path(flow_id)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return FlowRecord(
                flow_id=str(raw.get("flow_id") or safe_flow_id(flow_id)),
                kind=str(raw.get("kind") or "generic"),
                pid=int(raw.get("pid") or 0),
                ttl_s=float(raw.get("ttl_s") or 0.0),
                budget_tokens=int(raw.get("budget_tokens") or 0),
                used_tokens=int(raw.get("used_tokens") or 0),
                created_at=float(raw.get("created_at") or 0.0),
                last_beat=float(raw.get("last_beat") or 0.0),
                seq=int(raw.get("seq") or 0),
                killable=bool(raw.get("killable", True)),
                killed_at=float(raw.get("killed_at") or 0.0),
                kill_reason=str(raw.get("kill_reason") or ""),
                noticed_at=float(raw.get("noticed_at") or 0.0),
                state=dict(raw.get("state") or {}))
        except (TypeError, ValueError):
            return None

    def _write(self, record: FlowRecord) -> None:
        target = self._path(record.flow_id)
        payload = json.dumps({
            "flow_id": record.flow_id, "kind": record.kind, "pid": record.pid,
            "ttl_s": record.ttl_s, "budget_tokens": record.budget_tokens,
            "used_tokens": record.used_tokens,
            "created_at": record.created_at, "last_beat": record.last_beat,
            "seq": record.seq, "killable": record.killable,
            "killed_at": record.killed_at, "kill_reason": record.kill_reason,
            "noticed_at": record.noticed_at, "state": record.state,
        }, ensure_ascii=False, separators=(",", ":"))
        fd, tmp = tempfile.mkstemp(prefix=".flow-", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    def birth(self, flow_id: str, *, kind: str = "generic",
              ttl_s: float = DEFAULT_TTL_S, budget_tokens: int = 0,
              pid: int | None = None, killable: bool = True,
              state: dict[str, Any] | None = None) -> FlowRecord:
        now = self.clock()
        fid = safe_flow_id(flow_id)
        with self._lock:
            current = self._read(fid)
            record = FlowRecord(
                flow_id=fid, kind=str(kind), pid=int(pid if pid is not None
                                                  else os.getpid()),
                ttl_s=max(0.0, float(ttl_s)),
                budget_tokens=max(0, int(budget_tokens)),
                created_at=(current.created_at if current and not current.killed_at
                            else now),
                last_beat=now, seq=(current.seq if current else 0),
                killable=bool(killable), state=dict(state or {}))
            self._write(record)
            self._killed.discard(fid)
            self._notified.discard(fid)
            self._event("birth", record.as_dict(now))
            return record

    # ── prazo/orçamento ─────────────────────────────────────────────────────
    def expire(self, *, now: float | None = None) -> list[Expiry]:
        """Expira o que passou do TTL/orçamento; devolve as ações tomadas."""
        now = self.clock() if now is None else float(now)
        out: list[Expiry] = []
        with self._lock:
            for target in sorted(self.root.glob("*.json")):
                fid = target.stem
                if fid in self._killed or fid in self._notified:
                    continue
                record = self._read(fid)
                if record is None or record.killed_at:
                    self._killed.add(fid)
                    continue
                reason = ""
                if record.ttl_exceeded(now):
                    reason = "ttl"
                elif record.budget_exceeded():
                    reason = "orcamento"
                if not reason:
                    continue
                if record.killable:
                    expiry = self._kill_locked(record, reason, now)
                else:
                    record.noticed_at = now
                    self._write(record)
                    expiry = Expiry(flow_id=fid, reason=reason,
                                    action="compact", pid=record.pid, at=now)
                    self._notified.add(fid)
                    self._event("compact", expiry.as_dict())
                out.append(expiry)
        return out

    def _kill_locked(self, record: FlowRecord, reason: str,
                     now: float) -> Expiry:
        record.killed_at = now
        record.kill_reason = str(reason)
        self._write(record)
        self._killed.add(record.flow_id)
        expiry = Expiry(flow_id=record.flow_id, reason=str(reason),
                        action="kill", pid=record.pid, at=now)
        self._event("kill", expiry.as_dict())
        if self.signal_fn is not None and record.pid > 0:
            try:
                self.signal_fn(record.pid, self.kill_signal)
            except (OSError, ProcessLookupError) as exc:
                self._event("kill_falhou", {**expiry.as_dict(),
                                            "error": repr(exc)})
        if self.on_kill is not None:
            try:
                self.on_kill(expiry)
            except Exception as exc:
                self._event("on_kill_falhou", {**expiry.as_dict(),
                                               "error": repr(exc)})
        return expiry

    def kill(self, flow_id: str, reason: str = "pedido_do_dono",
             *, now: float | None = None) -> Expiry | None:
        fid = safe_flow_id(flow_id)
        now = self.clock() if now is None else float(now)
        with self._lock:
            record = self._read(fid)
            if record is None or record.killed_at:
                return None
            return self._kill_locked(record, reason, now)

    def clear_notice(self, flow_id: str) -> bool:
        """Libera o fluxo para ser avaliado de novo (depois de compactar)."""
        fid = safe_flow_id(flow_id)
        with self._lock:
            record = self._read(fid)
            if record is None:
                return False
            record.noticed_at = 0.0
            self._write(record)
            self._notified.discard(fid)
            return True

    def forget(self, flow_id: str) -> bool:
        """Remove o registro (fluxo terminou e ninguém deve ressuscitá-lo)."""
        fid = safe_flow_id(flow_id)
        with self._lock:
            try:
                self._path(fid).unlink()
            except OSError:
                return False
            self._killed.discard(fid)
            self._notified.discard(fid)
            return True

    # ── leitura ─────────────────────────────────────────────────────────────
    def snapshot(self, *, now: float | None = None) -> LifecycleSnapshot:
        now = self.clock() if now is None else float(now)
        flows: dict[str, dict[str, Any]] = {}
        for target in sorted(self.root.glob("*.json")):
            record = self._read(target.stem)
            if record is not None:
                flows[record.flow_id] = record.as_dict(now)
        return LifecycleSnapshot(flows=MappingProxyType(flows), taken_at=now)

    def _event(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception:
                pass


class FlowHeartbeat:
    """Batimento próprio de um fluxo; não decide TTL, só prova que está vivo."""

    def __init__(self, root: str | Path, flow_id: str, *,
                 clock: Callable[[], float] = time.time,
                 kind: str = "generic", ttl_s: float = DEFAULT_TTL_S,
                 pid: int | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.flow_id = safe_flow_id(flow_id)
        self.clock = clock
        self.kind = str(kind)
        self.ttl_s = max(0.0, float(ttl_s))
        self.pid = int(pid if pid is not None else os.getpid())
        self._lock = threading.Lock()
        self.last_seq = 0

    def _path(self) -> Path:
        return self.root / f"{self.flow_id}.json"

    def beat(self, *, used_tokens: int | None = None,
             state: dict[str, Any] | None = None) -> bool:
        """Grava o batimento. Devolve False se o supervisor já matou o fluxo."""
        now = self.clock()
        with self._lock:
            current: dict[str, Any] = {}
            try:
                current = json.loads(self._path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            if current.get("killed_at"):
                return False
            self.last_seq = int(current.get("seq") or 0) + 1
            ttl_raw = current.get("ttl_s")
            ttl_value = float(ttl_raw) if ttl_raw is not None else self.ttl_s
            payload = {
                "flow_id": self.flow_id,
                "kind": str(current.get("kind") or self.kind),
                "pid": int(current.get("pid") or self.pid),
                "ttl_s": ttl_value,
                "budget_tokens": int(current.get("budget_tokens") or 0),
                "used_tokens": int(used_tokens if used_tokens is not None
                                   else current.get("used_tokens") or 0),
                "created_at": float(current.get("created_at") or now),
                "last_beat": now,
                "seq": self.last_seq,
                "killable": bool(current.get("killable", True)),
                "killed_at": 0.0,
                "kill_reason": "",
                "noticed_at": float(current.get("noticed_at") or 0.0),
                "state": dict(state if state is not None
                              else current.get("state") or {}),
            }
            fd, tmp = tempfile.mkstemp(prefix=".beat-", dir=str(self.root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False,
                              separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._path())
            finally:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
            return True

    def alive(self) -> bool:
        try:
            current = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(current, dict) and not current.get("killed_at")
