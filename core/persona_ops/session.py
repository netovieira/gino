"""Sessão de raciocínio e memória particionada por persona.

Cada persona tem um diretório próprio em ``memory/personas/<slug>/``. É o
**único** lugar onde ela lê e grava memória: não vê ``memory/chat/`` (a memória
do Gino), não vê o diretório de outra persona e não move nem apaga registro
existente. A partição nasce vazia na primeira escrita; ler um diretório que
ainda não existe devolve texto vazio, não erro.

As cercas de leitura do cadastro (``allowed_dirs``) continuam sendo aplicadas
pelo sidecar, que é quem fala com as ferramentas Read/Glob/Grep. Este módulo é
a partição de MEMÓRIA, uma camada separada: nenhum caminho daqui amplia a
leitura de arquivos do projeto.

A sessão de raciocínio não abre processo nem thread. Ela registra o fluxo no
:class:`~core.flow_ops.registry.LifecycleRegistry` que já existe, bate ponto
com :class:`~core.flow_ops.registry.FlowHeartbeat`, cobra tokens e prazo e
devolve o estado para o orquestrador único — que continua sendo o dono do
relógio. ``pid=0`` de propósito: uma persona não é um processo, e o registro
nunca deve sinalizar o processo principal.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.flow_ops.registry import FlowHeartbeat, LifecycleRegistry, safe_flow_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERSONA_MEMORY_ROOT = PROJECT_ROOT / "memory" / "personas"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_\-\s]?key|access[_\-\s]?key|secret[_\-\s]?key|password|"
    r"passwd|senha|bearer\s+[a-z0-9]|sk-[a-z0-9]{4,})"
)
MAX_NOTE_CHARS = 4_000
MAX_READ_BYTES = 400_000


class MemoryError(ValueError):
    """Slug, caminho ou conteúdo inválido na partição da persona."""


def safe_slug(value: Any) -> str:
    slug = str(value or "").strip().casefold()
    if not _SLUG_RE.match(slug):
        raise MemoryError(f"slug de persona inválido: {value!r}")
    return slug


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("[segredo removido]", str(text or ""))


@dataclass(frozen=True)
class MemoryHit:
    path: str
    line: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "text": self.text}


class PersonaMemory:
    """Diretório de memória de UMA persona, com cerca de caminho."""

    def __init__(self, root: str | Path, slug: str):
        self.slug = safe_slug(slug)
        self.root = Path(root) / self.slug
        self._lock = threading.RLock()

    # ── caminho ─────────────────────────────────────────────────────────────
    def _path(self, rel: str) -> Path:
        raw = str(rel or "").strip().replace("\\", "/")
        if not raw:
            raise MemoryError("caminho de memória vazio")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise MemoryError(f"caminho absoluto não é permitido: {rel!r}")
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise MemoryError(f"caminho com '..' não é permitido: {rel!r}")
        target = (self.root / Path(*parts)).resolve(strict=False)
        base = self.root.resolve(strict=False)
        if target != base and base not in target.parents:
            raise MemoryError(f"caminho fora da partição da persona: {rel!r}")
        return target

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        return self.root

    # ── leitura ─────────────────────────────────────────────────────────────
    def read(self, rel: str = "state.md") -> str:
        target = self._path(rel)
        if not target.is_file():
            return ""
        try:
            data = target.read_bytes()
        except OSError:
            return ""
        return data[:MAX_READ_BYTES].decode("utf-8", "replace")

    def context(self, max_chars: int = 4_000) -> str:
        """Contexto próprio: estado + últimas notas, nunca o do Gino."""
        limit = max(0, int(max_chars))
        if not limit:
            return ""
        state = self.read("state.md").strip()
        notes = self.read("notes.md").strip().splitlines()
        tail = "\n".join(notes[-40:]).strip()
        body = "\n\n".join(part for part in (state, tail) if part)
        if len(body) <= limit:
            return body
        return body[-limit:]

    def search(self, query: str, limit: int = 20) -> list[MemoryHit]:
        needle = _scrub(query).strip().casefold()
        if not needle or not self.root.is_dir():
            return []
        hits: list[MemoryHit] = []
        for target in sorted(self.root.rglob("*")):
            if target.is_symlink() or not target.is_file():
                continue
            if target.suffix.lower() not in (".md", ".jsonl", ".txt"):
                continue
            try:
                data = target.read_bytes()[:MAX_READ_BYTES]
            except OSError:
                continue
            for number, line in enumerate(
                    data.decode("utf-8", "replace").splitlines(), 1):
                if needle in line.casefold():
                    hits.append(MemoryHit(
                        path=str(target.relative_to(self.root)),
                        line=number, text=_scrub(line[:500])))
                    if len(hits) >= max(1, int(limit)):
                        return hits
        return hits

    # ── escrita ─────────────────────────────────────────────────────────────
    def write(self, rel: str, text: str) -> Path:
        target = self._path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = str(text or "")
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                   dir=str(target.parent))
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
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def append(self, rel: str, text: str) -> Path:
        target = self._path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(str(text or ""))
                if not str(text or "").endswith("\n"):
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def note(self, text: str, kind: str = "note") -> Path:
        body = _scrub(" ".join(str(text or "").split()))
        if not body:
            raise MemoryError("nota vazia")
        body = body[:MAX_NOTE_CHARS]
        line = f"- {_now()} [{str(kind or 'note')[:32]}] {body}"
        with self._lock:
            target = self.append("notes.md", line)
        return target

    def record_turn(self, role: str, text: str) -> Path:
        body = _scrub(str(text or "")).strip()
        if not body:
            raise MemoryError("turno vazio")
        payload = json.dumps({
            "at": _now(),
            "persona": self.slug,
            "role": str(role or "assistant")[:32],
            "text": body[:MAX_NOTE_CHARS],
        }, ensure_ascii=False, separators=(",", ":"))
        return self.append("events.jsonl", payload)

    def partition(self) -> dict[str, Any]:
        files: list[str] = []
        if self.root.is_dir():
            for target in sorted(self.root.rglob("*")):
                if target.is_file():
                    files.append(str(target.relative_to(self.root)))
        return {"persona": self.slug, "root": str(self.root), "files": files}


@dataclass(frozen=True)
class BudgetState:
    used_tokens: int
    max_tokens: int
    used_s: float
    ttl_s: float
    alive: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "max_tokens": self.max_tokens,
            "used_s": round(self.used_s, 3),
            "ttl_s": self.ttl_s,
            "alive": self.alive,
            "reason": self.reason,
        }


@dataclass
class PersonaSession:
    """Uma sessão de raciocínio de persona, medida pelo orquestrador único."""

    persona: str
    grant: Any
    registry: LifecycleRegistry
    memory_root: str | Path = DEFAULT_PERSONA_MEMORY_ROOT
    clock: Callable[[], float] = time.time
    used_tokens: int = 0
    started_at: float = 0.0
    _finished_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock,
                                   repr=False)

    def __post_init__(self) -> None:
        self.persona = safe_slug(self.persona)
        self.started_at = float(self.clock())
        self.memory = PersonaMemory(self.memory_root, self.persona)
        self.flow_id = safe_flow_id(f"persona:{self.persona}")
        self.heartbeat = FlowHeartbeat(
            self.registry.root, self.flow_id, kind="persona",
            ttl_s=float(getattr(self.grant, "session_ttl_s", 0.0) or 0.0),
            clock=self.clock, pid=0)
        self.registry.birth(
            self.flow_id, kind="persona", pid=0, killable=False,
            ttl_s=float(getattr(self.grant, "session_ttl_s", 0.0) or 0.0),
            budget_tokens=int(getattr(self.grant, "max_tokens", 0) or 0),
            state={"persona": self.persona, "started_at": self.started_at})
        self.heartbeat.beat(used_tokens=0, state={
            "persona": self.persona, "started_at": self.started_at})

    # ── orçamento ───────────────────────────────────────────────────────────
    def charge(self, tokens: int) -> BudgetState:
        try:
            amount = max(0, int(tokens))
        except (TypeError, ValueError):
            amount = 0
        with self._lock:
            self.used_tokens += amount
            self.heartbeat.beat(used_tokens=self.used_tokens, state={
                "persona": self.persona, "started_at": self.started_at})
            return self.budget_state()

    def budget_state(self) -> BudgetState:
        with self._lock:
            used_s = max(0.0, float(self.clock()) - self.started_at)
            max_tokens = int(getattr(self.grant, "max_tokens", 0) or 0)
            ttl_s = float(getattr(self.grant, "session_ttl_s", 0.0) or 0.0)
            reason = ""
            if max_tokens and self.used_tokens > max_tokens:
                reason = "orcamento_de_tokens"
            elif ttl_s and used_s > ttl_s:
                reason = "prazo_de_sessao"
            return BudgetState(
                used_tokens=self.used_tokens, max_tokens=max_tokens,
                used_s=used_s, ttl_s=ttl_s, alive=not reason, reason=reason)

    def alive(self) -> bool:
        return self.budget_state().alive

    def beat(self, state: Mapping[str, Any] | None = None) -> bool:
        payload = {"persona": self.persona, "started_at": self.started_at}
        payload.update(dict(state or {}))
        return self.heartbeat.beat(used_tokens=self.used_tokens,
                                   state=payload)

    def finish(self, status: str = "done") -> None:
        with self._lock:
            self._finished_at = float(self.clock())
            self.heartbeat.beat(used_tokens=self.used_tokens, state={
                "persona": self.persona,
                "started_at": self.started_at,
                "finished_at": self._finished_at,
                "status": str(status or "done")[:32],
            })

    # ── contexto ────────────────────────────────────────────────────────────
    def context(self, max_chars: int = 4_000) -> str:
        return self.memory.context(max_chars=max_chars)

    def note(self, text: str, kind: str = "note") -> Path:
        return self.memory.note(text, kind=kind)

    def record_turn(self, role: str, text: str) -> Path:
        return self.memory.record_turn(role, text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "persona": self.persona,
            "flow_id": self.flow_id,
            "started_at": self.started_at,
            "finished_at": self._finished_at,
            "memory": self.memory.partition(),
            "budget": self.budget_state().as_dict(),
            "grant": getattr(self.grant, "as_dict", lambda: {})(),
        }


def partition_of(root: str | Path, slug: str) -> dict[str, Any]:
    """Só o mapa da partição, sem abrir sessão nem cobrar orçamento."""
    return PersonaMemory(root, slug).partition()
