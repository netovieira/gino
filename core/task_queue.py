"""Persistent queue for the owner's explicit tasks, reminders and events.

WHY THIS EXISTS
    When the owner dictates a list ("põe na fila: X, Y e Z"), the assistant
    needs one durable place to keep those items instead of holding them in the
    conversation. This module is that place: a small JSON file, one lock, and
    pure functions. It never starts work by itself — main.py (the scheduler)
    calls ``tick`` and then ``start`` / ``finish`` around the real work.

SEMANTICS
    Only explicitly queued items live here. Background Qoder agents have their
    own queue (``core.qoder_jobs``) and stay independent.
    ``task`` items have no due time by default and are ready immediately;
    ``reminder`` and ``event`` items normally carry an ISO ``due`` and become
    ready when ``tick`` sees ``due <= now``. ``tick`` is read-only; the caller
    decides what to start and when to finish it.

STORAGE
    ``memory/tasks.json``::

        {"version": 1,
         "tasks": [{"id": "a1b2c3d4e5f6", "title": "...", "kind": "task",
                    "status": "queued", "priority": 0, "due": null,
                    "created": "...", "started": null, "finished": null,
                    "persona": null, "project": null, "payload": {},
                    "result": "", "error": ""}]}

    Every write is atomic (tmp + ``os.replace``) and serialised by one lock.
    A malformed file is repaired to an empty queue. History is bounded: the
    last 500 ``done``, 500 ``error`` and 500 ``cancelled`` items are kept;
    every open item is always kept.

FAILURE POLICY
    No Qt, no ``main.py``, no model imports and no background threads. Disk
    write failures are reported and never raise into the conversation turn.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import date, datetime, time as dtime
from pathlib import Path

TASKS_PATH = Path(__file__).parent.parent / "memory" / "tasks.json"
lock = threading.RLock()
_LOCK = lock

KINDS = ("task", "reminder", "event")
STATUSES = ("queued", "running", "done", "error", "cancelled")
OPEN_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("done", "error", "cancelled")
HISTORY_PER_STATUS = 500
_VERSION = 1


# ── date helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_dt(value) -> datetime | None:
    """Coerce datetime/date/ISO-ish text into an aware local datetime."""
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
            dt = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _iso(value) -> str | None:
    """Normalise a datetime/date/string to a stored ISO string (or None).

    Naive input stays naive (only formatting is canonicalised); temporal
    comparisons go through ``_parse_dt``, which attaches the local zone.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, dtime.min).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text


def _timestamp(value) -> float:
    dt = _parse_dt(value)
    return dt.timestamp() if dt is not None else 0.0


# ── task helpers ────────────────────────────────────────────────────────────

def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_kind(value) -> str:
    kind = str(value or "task").strip().lower()
    return kind if kind in KINDS else "task"


def _new_task(title, due=None, kind="task", priority=0, persona=None,
              project=None, payload=None) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "title": str(title or "").strip(),
        "kind": _clean_kind(kind),
        "status": "queued",
        "priority": _to_int(priority),
        "due": _iso(due),
        "created": _iso(_now()),
        "started": None,
        "finished": None,
        "persona": _clean_text(persona),
        "project": _clean_text(project),
        "payload": copy.deepcopy(payload) if isinstance(payload, dict) else {},
        "result": "",
        "error": "",
    }


def _normalise_task(raw) -> dict | None:
    """Coerce one stored row into the contract shape; None when unusable."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    row = dict(raw)
    row["id"] = str(row.get("id") or "").strip() or uuid.uuid4().hex[:12]
    row["title"] = title
    row["kind"] = _clean_kind(row.get("kind"))
    status = str(row.get("status") or "queued").strip().lower()
    row["status"] = status if status in STATUSES else "queued"
    row["priority"] = _to_int(row.get("priority"))
    row["due"] = _iso(row.get("due"))
    row["created"] = _iso(row.get("created")) or _iso(_now())
    row["started"] = _iso(row.get("started"))
    row["finished"] = _iso(row.get("finished"))
    row["persona"] = _clean_text(row.get("persona"))
    row["project"] = _clean_text(row.get("project"))
    row["payload"] = copy.deepcopy(row.get("payload")) if isinstance(
        row.get("payload"), dict) else {}
    row["result"] = str(row.get("result") or "")
    row["error"] = str(row.get("error") or "")
    return row


def _prune(tasks: list[dict]) -> list[dict]:
    """Keep every open item and the last HISTORY_PER_STATUS of each terminal."""
    drop: set[int] = set()
    for status in TERMINAL_STATUSES:
        indexes = [i for i, row in enumerate(tasks) if row.get("status") == status]
        if len(indexes) > HISTORY_PER_STATUS:
            drop.update(indexes[:-HISTORY_PER_STATUS])
    return [row for i, row in enumerate(tasks) if i not in drop]


def _order_key(row: dict) -> tuple:
    # Ties on priority/created keep storage order (Python's sort is stable).
    return (-_to_int(row.get("priority")), _timestamp(row.get("created")))


def _next_queued(tasks: list[dict]) -> dict | None:
    queued = [row for row in tasks if row.get("status") == "queued"]
    return min(queued, key=_order_key) if queued else None


def _find(tasks: list[dict], task_id) -> dict | None:
    wanted = str(task_id or "").strip()
    if not wanted:
        return None
    for row in tasks:
        if row.get("id") == wanted:
            return row
    return None


# ── atomic persistence ──────────────────────────────────────────────────────

def _empty_payload() -> dict:
    return {"version": _VERSION, "tasks": []}


def _write_locked(tasks: list[dict]) -> None:
    rows: list[dict] = []
    for raw in tasks:
        row = _normalise_task(raw)
        if row is not None:
            rows.append(row)
    rows = _prune(rows)
    path = Path(TASKS_PATH)
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _VERSION, "tasks": rows}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - disk problems must not kill a turn
        print(f"[TaskQueue] write failed ({path}): {exc}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _read_locked() -> list[dict]:
    path = Path(TASKS_PATH)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - unreadable = unusable
        print(f"[TaskQueue] read failed ({path}): {exc}")
        return []

    try:
        data = json.loads(text)
    except Exception:
        _write_locked([])             # malformed file: repair to an empty queue
        return []

    if isinstance(data, list):
        raw_rows = data
    elif isinstance(data, dict) and isinstance(data.get("tasks"), list):
        raw_rows = data["tasks"]
    else:
        _write_locked([])
        return []

    rows: list[dict] = []
    running_seen = False
    changed = False
    for raw in raw_rows:
        row = _normalise_task(raw)
        if row is None:
            changed = True
            continue
        if row["status"] == "running":
            if running_seen:         # invariant repair: only one task runs
                row["status"] = "queued"
                row["started"] = None
                changed = True
            running_seen = True
        rows.append(row)

    pruned = _prune(rows)
    if len(pruned) != len(rows):
        changed = True
    if changed:
        _write_locked(pruned)
    return copy.deepcopy(pruned)


def load() -> list[dict]:
    """Read every stored task (repairing a malformed file to an empty queue)."""
    with lock:
        return _read_locked()


def save(tasks: list[dict] | None = None) -> None:
    """Atomically persist ``tasks`` (or the current file after pruning)."""
    with lock:
        rows = tasks if tasks is not None else _read_locked()
        _write_locked(rows if isinstance(rows, list) else [])


def reset() -> None:
    """Remove the queue file; tests use this between isolated runs."""
    with lock:
        try:
            Path(TASKS_PATH).unlink(missing_ok=True)
        except OSError as exc:
            print(f"[TaskQueue] reset failed ({TASKS_PATH}): {exc}")


# ── public queue API ────────────────────────────────────────────────────────

def add(title, due=None, kind="task", priority=0, persona=None,
        project=None, payload=None) -> dict:
    """Append one queued item and return its stored representation."""
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    with lock:
        tasks = _read_locked()
        row = _new_task(title, due=due, kind=kind, priority=priority,
                        persona=persona, project=project, payload=payload)
        tasks.append(row)
        _write_locked(tasks)
        return copy.deepcopy(row)


def add_many(items) -> list[dict]:
    """Append strings or dicts in one read/write cycle; return the new rows."""
    if isinstance(items, (str, dict)):
        items = [items]
    added: list[dict] = []
    with lock:
        tasks = _read_locked()
        for item in items or []:
            if isinstance(item, dict):
                title = item.get("title")
                due = item.get("due")
                kind = item.get("kind", "task")
                priority = item.get("priority", 0)
                persona = item.get("persona")
                project = item.get("project")
                payload = item.get("payload")
            else:
                title = item
                due = persona = project = payload = None
                kind, priority = "task", 0
            title = str(title or "").strip()
            if not title:
                continue
            row = _new_task(title, due=due, kind=kind, priority=priority,
                            persona=persona, project=project, payload=payload)
            tasks.append(row)
            added.append(row)
        if added:
            _write_locked(tasks)
    return copy.deepcopy(added)


def list_tasks(status=None) -> list[dict]:
    """Stored tasks, optionally filtered by status (``open`` means queued+running)."""
    rows = load()
    wanted = str(status).strip().lower() if status is not None else ""
    if not wanted or wanted in ("all", "todas", "*"):
        return rows
    if wanted in ("open", "abertas", "aberta", "ativas"):
        return [row for row in rows if row.get("status") in OPEN_STATUSES]
    return [row for row in rows if row.get("status") == wanted]


def next_task(now=None) -> dict | None:
    """Highest priority, then earliest created, among queued items.

    Due times do not hide a task here: readiness is decided by ``tick``. The
    parameter is accepted so callers/tests can pass a clock without changing
    the ordering contract.
    """
    del now
    with lock:
        return copy.deepcopy(_next_queued(_read_locked()))


def start(id, now=None) -> dict | None:
    """Move a queued item to running; refuse while another item is running."""
    with lock:
        tasks = _read_locked()
        row = _find(tasks, id)
        if row is None:
            return None
        if row["status"] == "running":
            return copy.deepcopy(row)
        if row["status"] != "queued":
            return None
        if any(other.get("status") == "running" for other in tasks):
            return None
        row["status"] = "running"
        row["started"] = _iso(now if now is not None else _now())
        row["finished"] = None
        _write_locked(tasks)
        return copy.deepcopy(row)


def finish(id, ok=True, result="", error="") -> dict | None:
    """Close a task as done (``ok``) or error; terminal rows are returned as-is."""
    with lock:
        tasks = _read_locked()
        row = _find(tasks, id)
        if row is None:
            return None
        if row["status"] in TERMINAL_STATUSES:
            return copy.deepcopy(row)
        failed = (not ok) or bool(str(error or "").strip())
        row["status"] = "error" if failed else "done"
        row["result"] = str(result or "")
        row["error"] = str(error or "") if failed else ""
        row["finished"] = _iso(_now())
        _write_locked(tasks)
        return copy.deepcopy(row)


def cancel(id) -> dict | None:
    """Cancel an open task; already finished/error/cancelled rows are refused."""
    with lock:
        tasks = _read_locked()
        row = _find(tasks, id)
        if row is None:
            return None
        if row["status"] == "cancelled":
            return copy.deepcopy(row)
        if row["status"] in TERMINAL_STATUSES:
            return None
        row["status"] = "cancelled"
        row["finished"] = _iso(_now())
        _write_locked(tasks)
        return copy.deepcopy(row)


def tick(now=None) -> list[dict]:
    """Return queued items whose due time has arrived (or that have no due).

    Read-only: the caller starts/finishes them. Reminders/events with a future
    due are skipped; explicit tasks without a due are ready immediately.
    """
    current = _parse_dt(now) if now is not None else _now()
    if current is None:
        current = _now()
    due_rows: list[dict] = []
    with lock:
        tasks = _read_locked()
    for row in tasks:
        if row.get("status") != "queued":
            continue
        raw_due = row.get("due")
        if raw_due is None or not str(raw_due).strip():
            due_rows.append(row)
            continue
        due = _parse_dt(raw_due)
        if due is not None and due <= current:
            due_rows.append(row)
    due_rows.sort(key=_order_key)
    return copy.deepcopy(due_rows)


def status() -> dict:
    """Snapshot for the assistant's spoken line and the status action."""
    with lock:
        tasks = _read_locked()
    current = next((row for row in tasks if row.get("status") == "running"), None)
    queued = [row for row in tasks if row.get("status") == "queued"]
    nxt = _next_queued(tasks)
    if current is not None:
        title = " ".join(str(current.get("title") or "").split()) or "a tarefa atual"
        line = f"Já está na fila; ainda estou fazendo {title}."
    elif queued:
        line = f"{len(queued)} tarefas na fila."
    else:
        line = "A fila está vazia."
    return {
        "current": copy.deepcopy(current) if current is not None else None,
        "queued": len(queued),
        "next": copy.deepcopy(nxt) if nxt is not None else None,
        "line": line,
    }


format_status = status
