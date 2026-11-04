"""Contrato JSONL versionado entre as duas metades do orquestrador.

O despachante quente e o coordenador frio falam pelo mesmo contrato. Cada
evento é **uma linha JSON** no formato::

    {"v": 2, "type": "task", "id": "t1", "ts": 1762..., "data": {...}}

``v`` é a versão de schema. O receptor (``decode``) aceita apenas as versões
declaradas em ``accepted`` (por padrão só :data:`SCHEMA_VERSION`) e **recusa**
o resto com :class:`SchemaError`, cuja mensagem diz a versão recebida, a
suportada e o tipo do evento. Não há negociação silenciosa: metade antiga não
consome evento novo, e o erro aparece no log em vez de virar comportamento
estranho.

Regras do contrato (seção 3 de ``PLANO.md``): ``brief`` não carrega fato;
``monitor_event`` não vira ``task``; ``pending_confirm`` não vira execução.
Quem garante as regras é a peça que produz o evento (regras/roteador); este
módulo só garante forma, versão e ida-e-volta.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Versão que esta metade fala. Subir só quando a forma mudar de verdade.
SCHEMA_VERSION = 2
#: Identificador lógico do contrato (para log e erro).
PROTOCOL = "gino.flows"

#: Versões aceitas pelo receptor desta metade.
SUPPORTED_VERSIONS = frozenset({2})

MAX_LINE_BYTES = 64_000
MAX_PAYLOAD_KEYS = 64


class SchemaError(ValueError):
    """Evento recusado por versão/formato incompatível.

    Carrega os campos separados para o chamador (e o teste) não precisarem
    interpretar texto. ``str(exc)`` é a mensagem clara que vai para o log.
    """

    def __init__(self, message: str, *, received: Any = None,
                 supported: Iterable[int] | None = None,
                 event_type: str = "", line_no: int = 0):
        super().__init__(message)
        self.received = received
        self.supported = tuple(sorted(supported or SUPPORTED_VERSIONS))
        self.event_type = event_type
        self.line_no = int(line_no or 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "schema_incompativel",
            "message": str(self),
            "received": self.received,
            "supported": list(self.supported),
            "event_type": self.event_type,
            "line": self.line_no,
        }


def _version_error(*, event_type: str, received: Any,
                   accepted: Iterable[int], line_no: int = 0) -> SchemaError:
    supported = tuple(sorted(accepted))
    if received is None or received == "":
        message = (
            f"evento {event_type or '?'!r} sem versão de schema; "
            f"esta metade fala {list(supported)}")
    else:
        direction = "mais nova" if isinstance(received, int) and \
            received > max(supported) else "mais antiga ou desconhecida"
        message = (
            f"evento {event_type or '?'!r} com schema {received!r} "
            f"incompatível ({direction}); esta metade aceita {list(supported)}")
    return SchemaError(message, received=received, supported=supported,
                       event_type=event_type, line_no=line_no)


@dataclass
class Envelope:
    """Um evento do contrato, já validado."""

    type: str
    id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    v: int = SCHEMA_VERSION
    ts: float = 0.0

    def __post_init__(self) -> None:
        self.type = str(self.type or "").strip()
        self.id = str(self.id or "")
        if not self.type:
            raise SchemaError("evento sem 'type'")
        if not isinstance(self.data, dict):
            raise SchemaError(
                f"evento {self.type!r} com 'data' {type(self.data).__name__}; "
                "esperado objeto")
        if not self.ts:
            self.ts = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {"v": int(self.v), "type": self.type, "id": self.id,
                "ts": float(self.ts), "data": dict(self.data)}

    def encode(self) -> str:
        return encode(self)


def make(event_type: str, *, id: str = "", ts: float | None = None,
         version: int = SCHEMA_VERSION, **data: Any) -> Envelope:
    """Monta um envelope desta metade (ou de uma versão explícita, em teste)."""
    return Envelope(type=event_type, id=id, data=dict(data),
                    v=int(version), ts=float(ts or time.time()))


def encode(event: Envelope | Mapping[str, Any]) -> str:
    """Serializa um evento em **uma** linha JSON (sem newline embutido)."""
    if isinstance(event, Envelope):
        payload = event.to_dict()
    elif isinstance(event, Mapping):
        payload = dict(event)
        if "v" not in payload:
            payload["v"] = SCHEMA_VERSION
        payload.setdefault("type", "")
        payload.setdefault("id", "")
        payload.setdefault("ts", time.time())
        payload.setdefault("data", {})
    else:
        raise SchemaError(f"não sei serializar {type(event).__name__}")
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise SchemaError(f"evento grande demais: {len(line)} bytes")
    return line


def decode(line: str, *, accepted: Iterable[int] | None = None,
           line_no: int = 0) -> Envelope:
    """Recusa versão incompatível com erro claro; devolve o envelope válido."""
    accepted = frozenset(accepted or SUPPORTED_VERSIONS)
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    raw = str(line or "").strip()
    if not raw:
        raise SchemaError("linha vazia no contrato JSONL", line_no=line_no)
    if "\n" in raw or "\r" in raw:
        raise SchemaError("mais de um evento numa linha", line_no=line_no)
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"linha {line_no or '?'} não é JSON: {exc}",
                          line_no=line_no) from None
    if not isinstance(obj, dict):
        raise SchemaError(
            f"evento deve ser objeto JSON, veio {type(obj).__name__}",
            line_no=line_no)
    event_type = str(obj.get("type") or "")
    if not event_type:
        raise SchemaError("evento sem 'type'", line_no=line_no)
    if "v" not in obj:
        raise _version_error(event_type=event_type, received=None,
                             accepted=accepted, line_no=line_no)
    version = obj.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _version_error(event_type=event_type, received=version,
                             accepted=accepted, line_no=line_no)
    if version not in accepted:
        raise _version_error(event_type=event_type, received=version,
                             accepted=accepted, line_no=line_no)
    data = obj.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SchemaError(
            f"evento {event_type!r} com 'data' {type(data).__name__}; "
            "esperado objeto", event_type=event_type, line_no=line_no)
    if len(data) > MAX_PAYLOAD_KEYS:
        raise SchemaError(
            f"evento {event_type!r} com {len(data)} chaves em 'data'",
            event_type=event_type, line_no=line_no)
    try:
        ts = float(obj.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return Envelope(type=event_type, id=str(obj.get("id") or ""),
                    data=dict(data), v=version, ts=ts)


def decode_many(lines: Iterable[str], *,
                accepted: Iterable[int] | None = None) -> list[Envelope]:
    """Decodifica um bloco; o primeiro evento inválido interrompe com erro."""
    out: list[Envelope] = []
    for index, line in enumerate(lines, 1):
        if not str(line or "").strip():
            continue
        out.append(decode(line, accepted=accepted, line_no=index))
    return out
