"""Supervisor de chats persistentes — orçamento e prazo (lei d).

Um chat tem tokens, TTL ocioso e tipo. Ao estourar o orçamento ou o prazo:

* chat efêmero (executor, monitores) é MORTO;
* chat de raciocínio é COMPACTADO, nunca morto — ele é a conversa do dono.

O relógio é injetável para os testes (``clock=...``). Em produção o runtime
chama :meth:`expire` periodicamente; o supervisor em si não abre thread.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import Budget

KIND_REASONING = "reasoning"
KIND_EXECUTOR = "executor"
KIND_MONITOR = "monitor"
KIND_MEDIA_MONITOR = "media_monitor"


@dataclass
class ChatRecord:
    name: str
    kind: str
    budget: Budget
    used_tokens: int = 0
    last_activity: float = 0.0
    created_at: float = 0.0
    alive: bool = True
    kills: list[str] = field(default_factory=list)
    compactions: list[str] = field(default_factory=list)

    @property
    def ephemeral(self) -> bool:
        return self.kind != KIND_REASONING and self.budget.ephemeral

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "alive": self.alive,
            "used_tokens": self.used_tokens,
            "budget_tokens": self.budget.tokens,
            "ttl_s": self.budget.ttl_s,
            "last_activity": self.last_activity,
            "kills": list(self.kills),
            "compactions": list(self.compactions),
        }


class Supervisor:
    """Registro de chats com TTL/orçamento; mata apenas os efêmeros."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.clock = clock
        self.on_event = on_event
        self.chats: dict[str, ChatRecord] = {}

    # ── registro ────────────────────────────────────────────────────────────
    def register(self, name: str, kind: str, budget: Budget,
                 *, now: float | None = None) -> ChatRecord:
        now = self.clock() if now is None else now
        record = ChatRecord(name=str(name), kind=str(kind), budget=budget,
                            last_activity=now, created_at=now)
        self.chats[record.name] = record
        self._event("register", record)
        return record

    def ensure(self, name: str, kind: str, budget: Budget,
               *, now: float | None = None) -> ChatRecord:
        record = self.chats.get(str(name))
        if record is None or not record.alive:
            return self.register(name, kind, budget, now=now)
        return record

    def touch(self, name: str, *, now: float | None = None) -> None:
        record = self.chats.get(str(name))
        if record is None or not record.alive:
            return
        record.last_activity = self.clock() if now is None else now

    # ── orçamento ───────────────────────────────────────────────────────────
    def add_usage(self, name: str, tokens: int,
                  *, now: float | None = None) -> str:
        """Contabiliza tokens e devolve ``ok``, ``compact`` ou ``killed``."""
        record = self.chats.get(str(name))
        if record is None or not record.alive:
            return "absent"
        record.used_tokens += max(0, int(tokens))
        now = self.clock() if now is None else now
        record.last_activity = now
        limit = max(1, record.budget.tokens)
        if record.kind == KIND_REASONING:
            # O chat de raciocínio SEMPRE pode ser compactado, nunca morto.
            if record.used_tokens >= int(limit * record.budget.soft_at):
                record.compactions.append("soft_limit")
                self._event("compact", record)
                return "compact"
            return "ok"
        if record.used_tokens > limit:
            self.kill(record.name, "orcamento estourado", now=now)
            return "killed"
        if record.used_tokens >= int(limit * record.budget.soft_at):
            return "compact"
        return "ok"

    # ── prazo ───────────────────────────────────────────────────────────────
    def expire(self, *, now: float | None = None) -> list[tuple[str, str]]:
        """Mata chats efêmeros ociosos. Devolve ``[(nome, motivo)]``."""
        now = self.clock() if now is None else now
        killed: list[tuple[str, str]] = []
        for record in list(self.chats.values()):
            if not record.alive:
                continue
            if record.kind == KIND_REASONING:
                # Prazo do raciocínio é compactação, não morte.
                if (record.budget.ttl_s > 0
                        and now - record.last_activity >= record.budget.ttl_s):
                    record.compactions.append("ttl")
                    self._event("compact", record)
                continue
            if not record.budget.ephemeral:
                continue
            if record.budget.ttl_s <= 0:
                continue
            if now - record.last_activity >= record.budget.ttl_s:
                reason = "silencio dentro do TTL"
                self.kill(record.name, reason, now=now)
                killed.append((record.name, reason))
        return killed

    def kill(self, name: str, reason: str,
             *, now: float | None = None) -> bool:
        record = self.chats.get(str(name))
        if record is None or not record.alive:
            return False
        if record.kind == KIND_REASONING:
            # Nunca mata a conversa principal: registra compactação.
            record.compactions.append(f"kill_recusado:{reason}")
            self._event("kill_refused", record, reason=reason)
            return False
        record.alive = False
        record.kills.append(str(reason))
        record.last_activity = self.clock() if now is None else now
        self._event("kill", record, reason=reason)
        return True

    def prune(self) -> None:
        for name in [n for n, c in self.chats.items() if not c.alive]:
            self.chats.pop(name, None)

    def snapshot(self) -> dict[str, Any]:
        return {name: record.snapshot()
                for name, record in self.chats.items()}

    def _event(self, kind: str, record: ChatRecord, **extra: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, {"chat": record.name, "kind": record.kind,
                                 **extra})
        except Exception:
            pass
