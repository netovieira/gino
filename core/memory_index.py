"""The memory spider web: events cross-linked by subject, time, persona, project.

WHY THIS EXISTS
    The chat memory writes prose into ``memory/chat/{state,hot,hours,days,
    topics}``. That answers "what happened", but not "everything about saúde",
    "everything Ana did", or "what was happening at 22h on that Tuesday".
    This module keeps a small graph of *events* with one node per facet and
    renders bounded markdown pages (plus ``spider.md``) that Obsidian can read
    as a native vault mirror.

STORAGE
    ``memory/chat/index/graph.json``::

        {"version": 1,
         "events": {"evt-20261031T221500-000001": {...}},
         "nodes": {"subject:saude": {...}, "time:2026-10-31/22": {...}},
         "edges": [{"from": "event:...", "to": "subject:saude",
                    "kind": "about"}]}

    Human pages live beside it: ``subjects/<slug>.md``, ``personas/<slug>.md``,
    ``projects/<slug>.md``, ``hours/<date>/<HH>.md`` and ``spider.md``. Every
    write is atomic (tmp + ``os.replace``) and serialised by one lock.

FAILURE POLICY
    A malformed graph is repaired to an empty graph in memory and rewritten on
    the next event; a page or Obsidian mirror failure never loses the event.
    No Qt, no ``main.py``, no model imports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import unicodedata
from datetime import date, datetime, time as dtime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "memory" / "chat" / "index"

MAX_EVENTS = 5000
MAX_LINKS = 200
PAGE_EVENT_LIMIT = 50
MAX_TEXT = 1000
MAX_REFS = 50

FACET_KINDS = ("subject", "persona", "project")
NODE_KINDS = ("subject", "persona", "project", "time", "event")

lock = threading.RLock()
_LOCK = lock
_COUNTER = 0
_WIKI_BAD = re.compile(r"[\[\]|#^]")


# ── small helpers ───────────────────────────────────────────────────────────

def _fold(text) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    return raw.encode("ascii", "ignore").decode("ascii")


def slugify(text, default: str = "geral") -> str:
    """Accentless, lowercase hyphen slug; ``default`` when nothing remains."""
    raw = _fold(text).lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:64].strip("-") or default


def _now() -> datetime:
    return datetime.now().astimezone()


def _coerce_dt(value) -> datetime | None:
    """datetime/date/ISO string -> aware local datetime; None if unusable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, dtime.min)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _event_dt(event: dict) -> datetime | None:
    return _coerce_dt((event or {}).get("at"))


def _atomic_write(path: Path, text: str) -> bool:
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001 - disk full/race must not kill a turn
        print(f"[MemoryIndex] write failed ({path}): {exc}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _empty_graph() -> dict:
    return {"version": 1, "events": {}, "nodes": {}, "edges": []}


def graph_path() -> Path:
    return Path(BASE_DIR) / "graph.json"


def _plans_dir() -> Path:
    return Path.home() / ".qoder" / "plans"


# ── graph load / repair ─────────────────────────────────────────────────────

def _clean_graph(raw) -> dict:
    """Coerce anything into a usable graph; never raise."""
    graph = _empty_graph()
    if not isinstance(raw, dict):
        return graph
    events = raw.get("events")
    if isinstance(events, dict):
        for eid, event in events.items():
            if isinstance(eid, str) and isinstance(event, dict):
                row = dict(event)
                row.setdefault("id", eid)
                graph["events"][eid] = row
    nodes = raw.get("nodes")
    if isinstance(nodes, dict):
        for nid, node in nodes.items():
            if isinstance(nid, str) and isinstance(node, dict):
                row = dict(node)
                row.setdefault("id", nid)
                row.setdefault("kind", nid.split(":", 1)[0])
                graph["nodes"][nid] = row
    edges = raw.get("edges")
    if isinstance(edges, list):
        seen: set[tuple] = set()
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src, dst = edge.get("from"), edge.get("to")
            if not isinstance(src, str) or not isinstance(dst, str):
                continue
            if not src or not dst or src == dst:
                continue
            kind = str(edge.get("kind") or "related")
            key = (kind, *sorted((src, dst)))
            if key in seen:
                continue
            seen.add(key)
            graph["edges"].append({"from": src, "to": dst, "kind": kind})
    return graph


def load_graph() -> dict:
    path = graph_path()
    try:
        if not path.is_file():
            return _empty_graph()
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - malformed JSON, perms, half mount
        print(f"[MemoryIndex] graph repair (empty): {exc}")
        return _empty_graph()
    return _clean_graph(raw)


def _save_graph(graph: dict) -> None:
    _atomic_write(
        graph_path(),
        json.dumps(graph, ensure_ascii=False, indent=2, sort_keys=True),
    )


def _node_id(kind: str, slug: str) -> str:
    return f"{kind}:{slug}"


def _node_label(node_id: str) -> str:
    kind, _, slug = node_id.partition(":")
    if kind == "time":
        return slug.replace("/", " ") + "h"
    if kind == "event":
        return slug
    return slug.replace("-", " ")


def _ensure_node(graph: dict, node_id: str) -> dict:
    kind = node_id.split(":", 1)[0]
    node = graph["nodes"].get(node_id)
    if not isinstance(node, dict):
        node = {
            "id": node_id,
            "kind": kind,
            "label": _node_label(node_id),
            "created": _now().isoformat(timespec="seconds"),
        }
        graph["nodes"][node_id] = node
    node["updated"] = _now().isoformat(timespec="seconds")
    return node


def _add_edge(graph: dict, seen: set[tuple], src: str, dst: str, kind: str) -> bool:
    if not src or not dst or src == dst:
        return False
    key = (kind, *sorted((src, dst)))
    if key in seen:
        return False
    seen.add(key)
    graph["edges"].append({"from": src, "to": dst, "kind": kind})
    return True


# ── event ingestion ─────────────────────────────────────────────────────────

def record_event(
    kind,
    text,
    at=None,
    persona=None,
    project=None,
    subject=None,
    source="voice",
    refs=None,
) -> dict:
    """Index one event and refresh its pages. Returns the stored event."""
    global _COUNTER

    dt = _coerce_dt(at) or _now()
    at_iso = dt.isoformat(timespec="seconds")
    subj = slugify(subject, "geral")
    pers = slugify(persona, "") if persona else ""
    proj = slugify(project, "") if project else ""
    time_node = _node_id("time", f"{dt:%Y-%m-%d}/{dt:%H}")
    if refs is None:
        refs_list: list[str] = []
    elif isinstance(refs, (str, bytes)):
        refs_list = [str(refs)]
    else:
        try:
            refs_list = [str(r) for r in refs]
        except TypeError:
            refs_list = [str(refs)]
    refs_list = refs_list[:MAX_REFS]

    with _LOCK:
        graph = load_graph()
        _COUNTER += 1
        event_id = f"evt-{dt:%Y%m%dT%H%M%S}-{_COUNTER:06d}"
        event = {
            "id": event_id,
            "kind": str(kind or "note")[:64],
            "text": str(text or "")[:MAX_TEXT],
            "at": at_iso,
            "persona": pers or None,
            "project": proj or None,
            "subject": subj,
            "source": str(source or "voice")[:32],
            "refs": refs_list,
        }

        graph["events"][event_id] = event
        event_node = _node_id("event", event_id)
        _ensure_node(graph, event_node)
        _ensure_node(graph, _node_id("subject", subj))
        facet_nodes = [event_node, _node_id("subject", subj)]
        if pers:
            _ensure_node(graph, _node_id("persona", pers))
            facet_nodes.append(_node_id("persona", pers))
        if proj:
            _ensure_node(graph, _node_id("project", proj))
            facet_nodes.append(_node_id("project", proj))
        _ensure_node(graph, time_node)

        seen = {(e["kind"], *sorted((e["from"], e["to"]))) for e in graph["edges"]}
        _add_edge(graph, seen, event_node, _node_id("subject", subj), "about")
        if pers:
            _add_edge(graph, seen, event_node, _node_id("persona", pers), "by")
        if proj:
            _add_edge(graph, seen, event_node, _node_id("project", proj), "in")
        _add_edge(graph, seen, event_node, time_node, "during")

        if pers:
            _add_edge(graph, seen, _node_id("subject", subj),
                      _node_id("persona", pers), "cooccur")
        if proj:
            _add_edge(graph, seen, _node_id("subject", subj),
                      _node_id("project", proj), "cooccur")
        if pers and proj:
            _add_edge(graph, seen, _node_id("persona", pers),
                      _node_id("project", proj), "cooccur")

        _trim_events(graph)
        _save_graph(graph)
        _write_pages(graph, facet_nodes + [time_node])
        _mirror(graph)
        return dict(event)


def _trim_events(graph: dict) -> None:
    limit = max(0, int(MAX_EVENTS))
    if len(graph["events"]) <= limit:
        return
    ordered = sorted(graph["events"].values(), key=_event_sort_key)
    drop = ordered[: len(ordered) - limit]
    dropped_ids = set()
    for event in drop:
        eid = str(event.get("id") or "")
        graph["events"].pop(eid, None)
        dropped_ids.add(_node_id("event", eid))
    if not dropped_ids:
        return
    for nid in dropped_ids:
        graph["nodes"].pop(nid, None)
    graph["edges"] = [
        e for e in graph["edges"]
        if e.get("from") not in dropped_ids and e.get("to") not in dropped_ids
    ]


def _event_sort_key(event: dict):
    dt = _event_dt(event)
    return (dt.timestamp() if dt else 0.0, str(event.get("id") or ""))


# ── linking and reset ───────────────────────────────────────────────────────

def _split_facet(ref) -> str:
    text = str(ref or "").strip()
    kind, sep, slug = text.partition(":")
    kind = kind.strip().lower()
    if not sep or kind not in FACET_KINDS:
        raise ValueError(f"nó inválido: {text!r} (use subject:/persona:/project:)")
    canonical = slugify(slug, "")
    if not canonical:
        raise ValueError(f"nó inválido: {text!r} (slug vazio)")
    return _node_id(kind, canonical)


def link(a, b, kind="related") -> None:
    """Add a non-event edge between two facet nodes (deduplicated)."""
    src = _split_facet(a)
    dst = _split_facet(b)
    with _LOCK:
        graph = load_graph()
        _ensure_node(graph, src)
        _ensure_node(graph, dst)
        seen = {(e["kind"], *sorted((e["from"], e["to"]))) for e in graph["edges"]}
        _add_edge(graph, seen, src, dst, str(kind or "related"))
        _save_graph(graph)
        _write_pages(graph, [src, dst])
        _mirror(graph)


def reset() -> None:
    """Wipe graph and generated pages (tests / owner request)."""
    with _LOCK:
        shutil.rmtree(BASE_DIR, ignore_errors=True)


# ── active project/subject ─────────────────────────────────────────────────

def _active_path() -> Path:
    return Path(BASE_DIR) / "active.json"


def active() -> dict:
    """The project/subject currently in focus, if the owner tagged one."""
    try:
        data = json.loads(_active_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "project": str(data.get("project") or ""),
                "subject": str(data.get("subject") or ""),
            }
    except Exception:
        pass
    return {"project": "", "subject": ""}


def set_active(project=None, subject=None) -> dict:
    """Remember the current project/subject for later automatic events."""
    current = active()
    if project is not None:
        current["project"] = slugify(project, "")
    if subject is not None:
        current["subject"] = slugify(subject, "")
    try:
        path = _active_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"[MemoryIndex] active write failed: {exc}")
    return current


# ── queries ─────────────────────────────────────────────────────────────────

def query(
    persona=None,
    project=None,
    subject=None,
    since=None,
    until=None,
    limit=20,
) -> list[dict]:
    """Filter indexed events by facet/time, newest first."""
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 20
    if cap <= 0:
        return []
    pers = slugify(persona, "") if persona else ""
    proj = slugify(project, "") if project else ""
    subj = slugify(subject, "geral") if subject else ""
    start = _coerce_dt(since)
    end = _coerce_dt(until)

    with _LOCK:
        graph = load_graph()
        found: list[dict] = []
        for event in graph["events"].values():
            if pers and event.get("persona") != pers:
                continue
            if proj and event.get("project") != proj:
                continue
            if subj and event.get("subject") != subj:
                continue
            if start is not None or end is not None:
                dt = _event_dt(event)
                if dt is None:
                    continue
                if start is not None and dt < start:
                    continue
                if end is not None and dt > end:
                    continue
            found.append(dict(event))
    found.sort(key=_event_sort_key, reverse=True)
    return found[:cap]


# ── pages ───────────────────────────────────────────────────────────────────

def _page_path(node_id: str) -> Path | None:
    kind, _, slug = node_id.partition(":")
    base = Path(BASE_DIR)
    if kind == "subject":
        return base / "subjects" / f"{slug}.md"
    if kind == "persona":
        return base / "personas" / f"{slug}.md"
    if kind == "project":
        return base / "projects" / f"{slug}.md"
    if kind == "time":
        day, _, hour = slug.partition("/")
        if not day or not hour:
            return None
        return base / "hours" / day / f"{hour}.md"
    return None


def _wiki(node_id: str) -> str:
    return f"[[{_WIKI_BAD.sub('', node_id)}]]"


def _page_title(node_id: str) -> str:
    kind, _, slug = node_id.partition(":")
    label = _node_label(node_id)
    if kind == "subject":
        return f"Assunto: {label}"
    if kind == "persona":
        return f"Persona: {label}"
    if kind == "project":
        return f"Projeto: {label}"
    return f"Hora: {label}"


def _node_events(graph: dict, node_id: str) -> list[dict]:
    kind, _, slug = node_id.partition(":")
    out: list[dict] = []
    for event in graph["events"].values():
        if kind in FACET_KINDS:
            if event.get(kind) == slug:
                out.append(event)
        elif kind == "time":
            dt = _event_dt(event)
            if dt is not None and f"{dt:%Y-%m-%d}/{dt:%H}" == slug:
                out.append(event)
        elif kind == "event":
            if event.get("id") == slug:
                out.append(event)
    out.sort(key=_event_sort_key, reverse=True)
    return out


def _connections(graph: dict, node_id: str) -> list[str]:
    related: set[str] = set()
    for edge in graph["edges"]:
        src, dst = edge.get("from"), edge.get("to")
        if node_id in (src, dst):
            other = dst if src == node_id else src
            if isinstance(other, str) and not other.startswith("event:"):
                related.add(other)
    for event in _node_events(graph, node_id):
        for facet in ("subject", "persona", "project"):
            value = event.get(facet)
            if value:
                nid = _node_id(facet, str(value))
                if nid != node_id:
                    related.add(nid)
        dt = _event_dt(event)
        if dt is not None:
            nid = _node_id("time", f"{dt:%Y-%m-%d}/{dt:%H}")
            if nid != node_id:
                related.add(nid)
    related.discard(node_id)
    non_time = sorted(n for n in related if not n.startswith("time:"))
    times = sorted((n for n in related if n.startswith("time:")), reverse=True)
    return (non_time + times)[: max(0, int(MAX_LINKS))]


def _render_page(graph: dict, node_id: str) -> str:
    lines = [
        f"# {_page_title(node_id)}",
        "",
        f"> Nó: `{node_id}`",
        "",
        "## Conexões",
        "",
    ]
    connections = _connections(graph, node_id)
    if connections:
        lines.extend(f"- {_wiki(nid)}" for nid in connections)
    else:
        lines.append("_Sem conexões ainda._")
    lines.extend(["", "## Eventos recentes", ""])
    events = _node_events(graph, node_id)[: max(0, int(PAGE_EVENT_LIMIT))]
    if events:
        for event in events:
            when = str(event.get("at") or "")[:16].replace("T", " ")
            text = str(event.get("text") or "").replace("\n", " ")[:240]
            lines.append(f"- {when} — {text}")
    else:
        lines.append("_Nenhum evento._")
    lines.append("")
    return "\n".join(lines)


def _write_pages(graph: dict, node_ids) -> None:
    for node_id in dict.fromkeys(node_ids):
        path = _page_path(node_id)
        if path is None:
            continue
        try:
            _atomic_write(path, _render_page(graph, node_id))
        except Exception as exc:  # noqa: BLE001
            print(f"[MemoryIndex] page failed ({node_id}): {exc}")
    try:
        _atomic_write(Path(BASE_DIR) / "spider.md", _render_spider(graph))
    except Exception as exc:  # noqa: BLE001
        print(f"[MemoryIndex] spider failed: {exc}")


def _facet_counts(graph: dict, kind: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node_id, node in graph["nodes"].items():
        if node.get("kind") == kind:
            counts[node_id] = 0
    for event in graph["events"].values():
        if kind == "time":
            dt = _event_dt(event)
            if dt is None:
                continue
            nid = _node_id("time", f"{dt:%Y-%m-%d}/{dt:%H}")
        else:
            value = event.get(kind)
            if not value:
                continue
            nid = _node_id(kind, str(value))
        counts[nid] = counts.get(nid, 0) + 1
    return counts


def _spider_section(lines: list[str], title: str, counts: dict[str, int],
                    newest_first: bool = False) -> None:
    lines.extend([f"## {title}", ""])
    items = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]),
                   reverse=True)
    if newest_first:
        items = sorted(counts.items(), key=lambda kv: kv[0], reverse=True)
    if not items:
        lines.extend(["_Nenhum registro._", ""])
        return
    for node_id, count in items[: max(0, int(MAX_LINKS))]:
        label = "evento" if count == 1 else "eventos"
        lines.append(f"- {_wiki(node_id)} — {count} {label}")
    lines.append("")


def _render_spider(graph: dict) -> str:
    lines = [
        "# Teia de memória",
        "",
        f"_Eventos indexados: {len(graph['events'])}_",
        "",
    ]
    _spider_section(lines, "Por assunto", _facet_counts(graph, "subject"))
    _spider_section(lines, "Por persona", _facet_counts(graph, "persona"))
    _spider_section(lines, "Por projeto", _facet_counts(graph, "project"))
    _spider_section(lines, "Por hora", _facet_counts(graph, "time"),
                    newest_first=True)
    return "\n".join(lines)


def render_spider() -> str:
    with _LOCK:
        return _render_spider(load_graph())


def _mirror(graph: dict) -> None:
    """Best-effort vault mirror; a missing/broken Obsidian never loses data."""
    try:
        from core import obsidian  # local import: avoids import cycles/Qt

        obsidian.mirror_index(_render_spider(graph), graph)
    except Exception as exc:  # noqa: BLE001
        print(f"[MemoryIndex] Obsidian mirror skipped: {exc}")
