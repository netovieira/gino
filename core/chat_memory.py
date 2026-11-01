"""Continuous conversation memory kept as small, local markdown files.

WHY THIS EXISTS
    A Live session has its own resumption, but it dies with the process and it
    grows until the provider trims the oldest turns. Silently trimming history
    is how an assistant "forgets" the subject and starts mixing threads. This
    module is the assistant's own notebook for the SAME conversation, written
    to disk after every turn so a restart resumes from facts instead of from
    whatever the model imagines.

    The index has two speeds:

    - ``hot.md``: only the last 6 hours, bounded. It is what the prompt sees
      during an ongoing conversation.
    - ``hours/`` + ``days/``: the full local record, compacted per hour and
      indexed per day. Used on demand, or when a long pause means the hot
      index is no longer true.

    A gap longer than 6 hours is treated as a real pause: the prompt gets a
    ``[PAUSA LONGA: N dias]`` marker plus the last day index, never the stale
    hot index. The prompt then asks whether to resume the old subject or start
    a new one. Nothing here invents continuity — every line comes from what was
    actually stored.

STORAGE
    memory/chat/ (local, git-ignored; base_dir is configurable for tests)
        state.md                     last_seen, current topic, open threads
        hot.md                       last 6 h, max 40 lines / 4000 chars
        hours/YYYY-MM-DD/HH.md       turns of one hour
        days/YYYY-MM-DD/index.md     links to the hour files + last turns
        topics/<slug>.md             subjects written by tools/notes

    All writes are atomic (tmp file + os.replace) and serialised by a lock.
    Malformed JSON/MD on disk never raises: the reader skips what it cannot
    parse and the next write repairs the file. No LLM calls happen here; every
    summary is derived deterministically from stored lines.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BASE_DIR = BASE_DIR / "memory" / "chat"

HOT_HOURS = 6
HOT_MAX_LINES = 40
HOT_MAX_CHARS = 4000
MAX_TURN_CHARS = 1000
MAX_NOTES = 20
MAX_THREADS = 12
MAX_TOPIC_LINKS = 6

_LOCK = threading.RLock()

_ROLE_LABELS = {
    "user": "Você",
    "humano": "Você",
    "human": "Você",
    "assistant": "Assistente",
    "assistente": "Assistente",
    "bot": "Assistente",
    "system": "Sistema",
    "tool": "Ferramenta",
    "note": "Nota",
}

_RE_LAST_SEEN = re.compile(r"last_seen:[ \t]*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})")
_RE_LAST_USER = re.compile(r"last_user_at:[ \t]*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})")
_RE_CURRENT_TOPIC = re.compile(r"current_topic:[ \t]*(.*)$", re.M)
_RE_OPEN_THREADS = re.compile(r"open_threads:[ \t]*(.*)$", re.M)
_RE_STATE_NOTE = re.compile(
    r"^- ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}) \[([^\]]{1,32})\] (.+)$",
    re.M,
)
_RE_HOUR_LINE = re.compile(r"^-\s+([0-9]{2}):([0-9]{2})\s+(.{1,60}?):\s+(.*)$")
_RE_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_RE_WIKI_LINK = re.compile(r"\[\[topics/([^\]]+)\]\]")


def _now() -> datetime:
    return datetime.now()


def _fold(text: str) -> str:
    """Lowercase without accents — for comparison only, never for output."""
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _slugify(text: str, default: str = "") -> str:
    s = _fold(text)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60].strip("-") or default


def _as_dt(value) -> datetime | None:
    """Accept datetime, date, ISO string or 'YYYY-MM-DD HH:MM'; else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, _date):
        return datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _coerce_day(value) -> _date:
    dt = _as_dt(value)
    if dt is not None:
        return dt.date()
    return _now().date()


def _role_label(role) -> str:
    raw = " ".join(str(role or "").replace(":", " ").split()).strip()
    label = _ROLE_LABELS.get(raw.lower())
    if label:
        return label
    return (raw[:40] or "?")


def _truncate(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]


def _tail_lines(text: str, max_chars: int) -> str:
    """Keep the newest complete lines that fit — used to bound prompt pieces."""
    kept: list[str] = []
    used = 0
    for line in reversed(str(text or "").splitlines()):
        cost = len(line) + (1 if kept else 0)
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return "\n".join(kept)


def _human_gap(hours: float) -> str:
    # Round to whole hours first: 71.98 h must read "3 dias", never "2 dias e 24 h".
    total = int(round(max(0.0, hours)))
    days, rem = divmod(total, 24)
    if days and rem:
        return f"{days} dia{'s' if days != 1 else ''} e {rem} h"
    if days:
        return f"{days} dia{'s' if days != 1 else ''}"
    return f"{total} h"


def _safe_read(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # permissions, race with delete, bad mount...
        print(f"[ChatMemory] read failed ({path}): {e}")
        return ""


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
    except Exception as e:
        print(f"[ChatMemory] write failed ({path}): {e}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _first_heading(text: str) -> str:
    m = _RE_HEADING.search(text or "")
    if not m:
        return ""
    return " ".join(m.group(1).split())[:80]


def _parse_hour_file(path: Path, day: _date) -> list[tuple[datetime, str, str]]:
    turns: list[tuple[datetime, str, str]] = []
    for raw in _safe_read(path).splitlines():
        m = _RE_HOUR_LINE.match(raw.strip())
        if not m:
            continue
        hh, mm, label, text = m.groups()
        try:
            dt = datetime(day.year, day.month, day.day, int(hh), int(mm))
        except ValueError:
            continue
        label = " ".join(label.split())[:40] or "?"
        text = " ".join(text.split())
        if text:
            turns.append((dt, label, text))
    return turns


class ChatMemory:
    """Continuous conversation memory stored as markdown files."""

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir).expanduser() if base_dir else DEFAULT_BASE_DIR
        self.hours_dir = self.base_dir / "hours"
        self.days_dir = self.base_dir / "days"
        self.topics_dir = self.base_dir / "topics"
        self.state_path = self.base_dir / "state.md"
        self.hot_path = self.base_dir / "hot.md"
        self.index_path = self.base_dir / "index.md"
        with _LOCK:
            self._ensure_layout()

    # ── layout / state ───────────────────────────────────────────────────────
    def _ensure_layout(self) -> None:
        try:
            for folder in (self.base_dir, self.hours_dir, self.days_dir,
                           self.topics_dir):
                folder.mkdir(parents=True, exist_ok=True)
            if not self.state_path.exists():
                _atomic_write(self.state_path, self._state_text({
                    "last_seen": "", "current_topic": "",
                    "open_threads": [], "notes": [],
                }))
            if not self.hot_path.exists():
                _atomic_write(self.hot_path, self._hot_header())
        except Exception as e:
            print(f"[ChatMemory] layout failed ({self.base_dir}): {e}")

    @staticmethod
    def _hot_header() -> str:
        return f"# Índice quente — últimas {HOT_HOURS} h\n"

    @staticmethod
    def _state_text(state: dict) -> str:
        lines = [
            "# Estado da conversa",
            "",
            f"- last_seen: {state.get('last_seen') or ''}",
            f"- last_user_at: {state.get('last_user_at') or ''}",
            f"- current_topic: {state.get('current_topic') or ''}",
            "- open_threads: " + " | ".join(state.get("open_threads") or []),
            "",
        ]
        notes = state.get("notes") or []
        if notes:
            lines.append("## Notas")
            for n in notes:
                lines.append(f"- {n.get('at', '')} [{n.get('kind', 'note')}] "
                             f"{n.get('text', '')}")
            lines.append("")
        return "\n".join(lines)

    def _read_state(self) -> dict:
        state = {"last_seen": "", "last_user_at": "", "current_topic": "",
                 "open_threads": [], "notes": []}
        raw = _safe_read(self.state_path)
        if not raw:
            return state
        try:
            m = _RE_LAST_SEEN.search(raw)
            if m:
                state["last_seen"] = m.group(1).strip()
            m = _RE_LAST_USER.search(raw)
            if m:
                state["last_user_at"] = m.group(1).strip()
            m = _RE_CURRENT_TOPIC.search(raw)
            if m:
                value = m.group(1).strip()
                link = _RE_WIKI_LINK.search(value)
                if link:
                    value = link.group(1)
                else:
                    value = value.split("—")[0].strip()
                state["current_topic"] = _slugify(value)
            m = _RE_OPEN_THREADS.search(raw)
            if m:
                parts = [p.strip() for p in re.split(r"[|;]", m.group(1))]
                state["open_threads"] = [p for p in parts if p][:MAX_THREADS]
            for nm in _RE_STATE_NOTE.finditer(raw):
                state["notes"].append({
                    "at": nm.group(1), "kind": nm.group(2),
                    "text": nm.group(3).strip(),
                })
            state["notes"] = state["notes"][-MAX_NOTES:]
        except Exception as e:
            print(f"[ChatMemory] state parse failed: {e}")
        return state

    def _write_state(self, state: dict) -> None:
        try:
            state["open_threads"] = [
                " ".join(str(t).split())[:120]
                for t in (state.get("open_threads") or []) if str(t).strip()
            ][-MAX_THREADS:]
            state["notes"] = (state.get("notes") or [])[-MAX_NOTES:]
            _atomic_write(self.state_path, self._state_text(state))
        except Exception as e:
            print(f"[ChatMemory] state write failed: {e}")

    # ── gap ─────────────────────────────────────────────────────────────────
    def check_gap(self, now=None) -> dict:
        """How long since the last stored turn, and whether it was a pause."""
        result = {"gap_hours": 0.0, "long": False, "last_seen": "", "prompt": ""}
        try:
            current = _as_dt(now) or _now()
            with _LOCK:
                state = self._read_state()
            # The gap that matters for the conversation is since the OWNER last
            # spoke. Assistant/system turns after a silence must not erase it.
            raw = (state.get("last_user_at") or state.get("last_seen") or "")
            result["last_seen"] = raw
            last = _as_dt(raw)
            if last is None:
                return result
            gap = max(0.0, (current - last).total_seconds() / 3600.0)
            result["gap_hours"] = round(gap, 3)
            result["long"] = gap > HOT_HOURS
            if result["long"]:
                result["prompt"] = (
                    f"[PAUSA LONGA: {_human_gap(gap)}] A última vez que o dono "
                    f"falou foi em {last:%d/%m/%Y %H:%M}. A próxima fala abre "
                    "outro assunto: não finja continuidade nem misture com o fio "
                    "anterior; reconheça a pausa em uma frase curta e comece "
                    "pelo novo assunto."
                )
        except Exception as e:
            print(f"[ChatMemory] check_gap failed: {e}")
        return result

    def mark_user_speech(self, at=None) -> str:
        """Record that the OWNER actually spoke (not a reminder/status turn)."""
        try:
            dt = _as_dt(at) or _now()
            with _LOCK:
                self._ensure_layout()
                state = self._read_state()
                stamp = f"{dt:%Y-%m-%d %H:%M}"
                previous = _as_dt(state.get("last_user_at"))
                if previous is None or dt >= previous:
                    state["last_user_at"] = stamp
                last_seen = _as_dt(state.get("last_seen"))
                if last_seen is None or dt >= last_seen:
                    state["last_seen"] = stamp
                self._write_state(state)
            return stamp
        except Exception as e:
            print(f"[ChatMemory] mark_user_speech failed: {e}")
            return ""

    def subject_epoch(self, now=None, gap_hours=HOT_HOURS) -> dict:
        """Is this a new subject after a long silence?

        The Qoder session is NEVER reset here. This only tells the turn to carry
        a context marker and the last day index, so a speech after a pause is
        not mistaken for a continuation.
        """
        result = {"new_subject": False, "gap_hours": 0.0, "last_user_at": "",
                  "prompt": "", "last_day_index": ""}
        try:
            current = _as_dt(now) or _now()
            threshold = max(0.0, float(gap_hours or HOT_HOURS))
            with _LOCK:
                state = self._read_state()
            raw = state.get("last_user_at") or state.get("last_seen") or ""
            result["last_user_at"] = raw
            last = _as_dt(raw)
            if last is None:
                return result
            gap = max(0.0, (current - last).total_seconds() / 3600.0)
            result["gap_hours"] = round(gap, 3)
            if gap < threshold:
                return result
            index = self._last_day_index(raw)
            result["new_subject"] = True
            result["last_day_index"] = index
            result["prompt"] = (
                f"[NOVO ASSUNTO APÓS PAUSA DE {_human_gap(gap)}] O dono não "
                f"falava desde {last:%d/%m/%Y %H:%M}. Trate a fala abaixo como "
                "o começo de outro assunto, não como continuação do fio "
                "anterior. Reconheça a pausa em uma frase curta, se fizer "
                "sentido, e não misture os assuntos. Se precisar de algo do "
                "passado, use o índice do último dia abaixo ou memory_search."
            )
            if index:
                result["prompt"] += ("\nÍndice do último dia:\n"
                                     + _truncate(index, 1100))
            return result
        except Exception as e:
            print(f"[ChatMemory] subject_epoch failed: {e}")
            return result

    def begin_new_subject(self, gap_hours=0.0) -> None:
        """Close the old thread in state and record the pause as a note."""
        try:
            with _LOCK:
                state = self._read_state()
                state["current_topic"] = ""
                note = (f"pausa de {_human_gap(float(gap_hours or 0))}; "
                        "novo assunto após a pausa")
                state.setdefault("notes", []).append({
                    "at": f"{_now():%Y-%m-%d %H:%M}", "kind": "pausa",
                    "text": note,
                })
                self._write_state(state)
        except Exception as e:
            print(f"[ChatMemory] begin_new_subject failed: {e}")

    # ── turns ───────────────────────────────────────────────────────────────
    def append_turn(self, role, text, at=None) -> str:
        """Append one turn to its hour file and refresh the hot index."""
        try:
            dt = _as_dt(at) or _now()
            clean = " ".join(str(text or "").split()).strip()
            if not clean:
                return ""
            line = f"- {dt:%H:%M} {_role_label(role)}: {clean[:MAX_TURN_CHARS]}"
            hour_path = self.hours_dir / f"{dt:%Y-%m-%d}" / f"{dt:%H}.md"
            with _LOCK:
                self._ensure_layout()
                old = _safe_read(hour_path)
                if not old.strip():
                    old = f"# {dt:%Y-%m-%d %H}h\n\n"
                _atomic_write(hour_path, old.rstrip("\n") + "\n" + line + "\n")
                state = self._read_state()
                previous = _as_dt(state.get("last_seen"))
                if previous is None or dt >= previous:
                    state["last_seen"] = f"{dt:%Y-%m-%d %H:%M}"
                self._write_state(state)
                self._ensure_hot(_now())
            return line
        except Exception as e:
            print(f"[ChatMemory] append_turn failed: {e}")
            return ""

    def _turns_between(self, start: datetime,
                       end: datetime) -> list[tuple[datetime, str, str]]:
        turns: list[tuple[datetime, str, str]] = []
        try:
            day = start.date()
            last = end.date()
            guard = 0
            while day <= last and guard < 10:
                folder = self.hours_dir / f"{day:%Y-%m-%d}"
                if folder.is_dir():
                    for path in sorted(folder.glob("*.md")):
                        for dt, label, text in _parse_hour_file(path, day):
                            if start <= dt <= end + timedelta(seconds=60):
                                turns.append((dt, label, text))
                day += timedelta(days=1)
                guard += 1
            turns.sort(key=lambda t: t[0])
        except Exception as e:
            print(f"[ChatMemory] turn scan failed: {e}")
        return turns

    def recent_turns(self, hours: float = HOT_HOURS, now=None) -> list[dict]:
        """Recent turns as plain dicts, for the librarian/summarizer."""
        try:
            current = _as_dt(now) or _now()
            start = current - timedelta(hours=float(hours or HOT_HOURS))
            return [
                {"at": f"{dt:%Y-%m-%d %H:%M}", "role": label, "text": text}
                for dt, label, text in self._turns_between(start, current)
            ]
        except Exception as e:
            print(f"[ChatMemory] recent_turns failed: {e}")
            return []

    # ── hot index ───────────────────────────────────────────────────────────
    def current_topic(self) -> str:
        """The topic slug currently open in state.md ('' when none)."""
        try:
            with _LOCK:
                return str(self._read_state().get("current_topic") or "")
        except Exception:
            return ""

    def _topic_links(self) -> list[str]:
        links: list[str] = []
        slugs: list[str] = []
        try:
            state = self._read_state()
            if state.get("current_topic"):
                slugs.append(state["current_topic"])
            files = sorted(self.topics_dir.glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files:
                if path.stem not in slugs:
                    slugs.append(path.stem)
        except Exception as e:
            print(f"[ChatMemory] topic scan failed: {e}")
        for slug in slugs:
            if len(links) >= MAX_TOPIC_LINKS:
                break
            path = self.topics_dir / f"{slug}.md"
            if not path.is_file():
                continue
            title = _first_heading(_safe_read(path)) or slug
            links.append(f"- [[topics/{slug}]] — {title[:80]}")
        return links

    def _render_hot(self, now: datetime) -> str:
        start = now - timedelta(hours=HOT_HOURS)
        turns = self._turns_between(start, now)
        body = [f"- {dt:%H:%M} {label}: {text[:280]}" for dt, label, text in turns]
        if not body:
            body = [f"- (sem falas nas últimas {HOT_HOURS} h)"]
        topic_lines = self._topic_links()
        header = self._hot_header()
        footer = ""
        if topic_lines:
            footer = "\nTópicos:\n" + "\n".join(topic_lines)
        reserved = 2 + (2 + len(topic_lines) if topic_lines else 0)
        max_body = max(1, HOT_MAX_LINES - reserved)
        body = body[-max_body:]
        while body and len(header) + 1 + len("\n".join(body)) + len(footer) + 1 > HOT_MAX_CHARS:
            if len(body) == 1:
                body[0] = body[0][:max(0, HOT_MAX_CHARS - len(header) - len(footer) - 3)]
                break
            body.pop(0)
        return header + "\n" + "\n".join(body) + footer + "\n"

    def _ensure_hot(self, now: datetime) -> str:
        text = self._render_hot(now)
        if _safe_read(self.hot_path) != text:
            _atomic_write(self.hot_path, text)
        return text

    # ── compaction / day index ──────────────────────────────────────────────
    def compact_hour(self, now=None) -> dict:
        """Rebuild the hot index and the day index for the current window."""
        result = {"window_hours": HOT_HOURS, "hot_lines": 0, "days": []}
        try:
            current = _as_dt(now) or _now()
            with _LOCK:
                self._ensure_layout()
                hot = self._ensure_hot(current)
                result["hot_lines"] = len(hot.splitlines())
                days = sorted({(current - timedelta(hours=HOT_HOURS)).date(),
                               current.date()})
                for day in days:
                    folder = self.hours_dir / f"{day:%Y-%m-%d}"
                    if not folder.is_dir() or not any(folder.glob("*.md")):
                        continue
                    if self.build_day_index(day):
                        result["days"].append(f"{day:%Y-%m-%d}")
        except Exception as e:
            print(f"[ChatMemory] compact_hour failed: {e}")
        return result

    def build_day_index(self, date=None) -> str:
        """Deterministic per-day index: links to hours plus the last turns."""
        try:
            day = _coerce_day(date)
            day_key = f"{day:%Y-%m-%d}"
            with _LOCK:
                self._ensure_layout()
                folder = self.hours_dir / day_key
                files = sorted(folder.glob("*.md")) if folder.is_dir() else []
                lines = [f"# Índice do dia {day_key}", ""]
                total = 0
                indexed = 0
                tail: list[tuple[datetime, str, str]] = []
                for path in files:
                    turns = _parse_hour_file(path, day)
                    if not turns:
                        continue
                    indexed += 1
                    total += len(turns)
                    lines.append(
                        f"- [{path.stem}h](hours/{day_key}/{path.name}) — "
                        f"{turns[0][0]:%H:%M}–{turns[-1][0]:%H:%M}, "
                        f"{len(turns)} fala{'s' if len(turns) != 1 else ''}"
                    )
                    tail.extend(turns[-3:])
                if not total:
                    lines.append("- (sem conversa registrada)")
                else:
                    lines.append("")
                    lines.append(f"Total: {total} falas em {indexed} "
                                 f"hora{'s' if indexed != 1 else ''}.")
                    lines.append("Últimas falas:")
                    for dt, label, text in tail[-6:]:
                        lines.append(f"- {dt:%H:%M} {label}: {text[:180]}")
                text = "\n".join(lines) + "\n"
                _atomic_write(self.days_dir / day_key / "index.md", text)
                return text
        except Exception as e:
            print(f"[ChatMemory] build_day_index failed: {e}")
            return ""

    def _last_day_index(self, last_seen: str) -> str:
        """Index of the day the conversation stopped, or the newest one."""
        try:
            with _LOCK:
                last = _as_dt(last_seen)
                candidates: list[str] = []
                if last is not None:
                    candidates.append(f"{last:%Y-%m-%d}")
                for folder in sorted(self.hours_dir.glob("*"), reverse=True):
                    if folder.is_dir() and folder.name not in candidates:
                        candidates.append(folder.name)
                for day_key in candidates:
                    path = self.days_dir / day_key / "index.md"
                    text = _safe_read(path)
                    if text.strip():
                        return text
                    text = self.build_day_index(day_key)
                    if text.strip():
                        return text
        except Exception as e:
            print(f"[ChatMemory] last day index failed: {e}")
        return ""

    # ── topics / notes ──────────────────────────────────────────────────────
    def read_topic(self, slug) -> str:
        try:
            clean = _slugify(slug)
            if not clean:
                return ""
            return _safe_read(self.topics_dir / f"{clean}.md")
        except Exception as e:
            print(f"[ChatMemory] read_topic failed: {e}")
            return ""

    def read(self, path) -> str:
        """Read a markdown file under the chat store, safely.

        The bridge exposes this as ``memory_read``; the model may ask for
        ``state.md``, ``hot.md``, ``topics/x.md`` or a day/hour file. Paths
        outside the store and non-text files are refused.
        """
        try:
            rel = str(path or "").strip().replace("\\", "/").lstrip("/")
            if not rel or any(part == ".." for part in Path(rel).parts):
                return ""
            base = self.base_dir.resolve()
            target = (base / rel).resolve()
            if target != base and base not in target.parents:
                return ""
            if target.suffix.lower() not in (".md", ".txt"):
                return ""
            return _safe_read(target)
        except Exception as e:
            print(f"[ChatMemory] read failed: {e}")
            return ""

    def topic_read(self, slug) -> str:
        """Alias used by the plan/tools surface (``memory_read``)."""
        return self.read_topic(slug)

    def write_topic(self, slug, content, set_current: bool = True) -> str:
        try:
            clean = _slugify(slug, "topico")
            text = str(content or "")
            if text and not text.endswith("\n"):
                text += "\n"
            with _LOCK:
                self._ensure_layout()
                _atomic_write(self.topics_dir / f"{clean}.md", text)
                state = self._read_state()
                if set_current:
                    state["current_topic"] = clean
                    self._write_state(state)
                self._ensure_hot(_now())
            return clean
        except Exception as e:
            print(f"[ChatMemory] write_topic failed: {e}")
            return ""

    def note(self, text, kind="note") -> str:
        """Record a short mark (also updates topic/threads for some kinds)."""
        try:
            clean = " ".join(str(text or "").split()).strip()[:400]
            if not clean:
                return ""
            label = _slugify(str(kind or "note"), "note")[:24]
            with _LOCK:
                self._ensure_layout()
                state = self._read_state()
                if label in ("topico", "topic", "assunto"):
                    state["current_topic"] = _slugify(clean, "topico")
                elif label in ("thread", "thread-aberta", "aberta"):
                    if clean not in state["open_threads"]:
                        state["open_threads"].append(clean[:120])
                elif label in ("resolve", "resolvida", "fechada", "closed", "done"):
                    wanted = _fold(clean)
                    state["open_threads"] = [
                        t for t in state["open_threads"] if _fold(t) != wanted
                    ]
                state["notes"].append({
                    "at": f"{_now():%Y-%m-%d %H:%M}",
                    "kind": label,
                    "text": clean,
                })
                self._write_state(state)
            return f"Anotado: {clean[:120]}"
        except Exception as e:
            print(f"[ChatMemory] note failed: {e}")
            return ""

    # ── search ──────────────────────────────────────────────────────────────
    def _search_paths(self) -> list[Path]:
        paths: list[Path] = []
        try:
            paths.extend(sorted(self.hours_dir.glob("*/*.md"), reverse=True))
            paths.extend(sorted(self.days_dir.glob("*/index.md"), reverse=True))
            paths.extend(sorted(self.topics_dir.glob("*.md")))
        except Exception as e:
            print(f"[ChatMemory] search scan failed: {e}")
        paths.append(self.state_path)
        return paths

    def search(self, query, limit=10) -> list[dict]:
        """Accent-insensitive line search across hours, days, topics, state."""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 10
        text = " ".join(str(query or "").split()).strip()
        if not text:
            return []
        tokens = [t for t in _fold(text).split() if t]
        if not tokens:
            return []
        results: list[dict] = []
        seen: set[str] = set()
        try:
            with _LOCK:
                for path in self._search_paths():
                    for raw in _safe_read(path).splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        folded = _fold(line)
                        if not all(token in folded for token in tokens):
                            continue
                        if line in seen:
                            continue
                        seen.add(line)
                        try:
                            source = path.relative_to(self.base_dir).as_posix()
                        except ValueError:
                            source = path.name
                        results.append({"source": source,
                                        "text": line[:400]})
                        if len(results) >= limit:
                            return results
        except Exception as e:
            print(f"[ChatMemory] search failed: {e}")
        return results

    # ── prompt ──────────────────────────────────────────────────────────────
    def prompt_block(self, max_chars=2000) -> str:
        """The memory as it goes into the system prompt — bounded and factual."""
        try:
            max_chars = int(max_chars)
        except (TypeError, ValueError):
            max_chars = 2000
        if max_chars <= 0:
            return ""
        try:
            current = _now()
            with _LOCK:
                self._ensure_layout()
                # Refresh hot.md first: after a long pause it must become empty
                # instead of keeping stale lines on disk for the next reader.
                self._ensure_hot(current)
                gap = self.check_gap(now=current)
                state = self._read_state()
                recent = self._turns_between(
                    current - timedelta(hours=HOT_HOURS), current)
                if (not gap["last_seen"] and not state["current_topic"]
                        and not state["open_threads"] and not recent):
                    return ""
                lines = ["[MEMÓRIA DA CONVERSA — arquivos locais]",
                         f"Agora: {current:%d/%m/%Y %H:%M}."]
                if state.get("current_topic"):
                    lines.append(f"Assunto atual: [[topics/{state['current_topic']}]]")
                if state.get("open_threads"):
                    lines.append("Threads abertas: "
                                 + "; ".join(state["open_threads"][:8]) + ".")
                if gap["long"]:
                    lines.append(gap["prompt"])
                    index = self._last_day_index(gap["last_seen"])
                    if index:
                        lines.append("Índice do último dia (fonte para retomar; "
                                     "não invente além dele):")
                        lines.append(_truncate(index, 1100))
                    lines.append("Não houve conversa nas últimas horas. Reconheça "
                                 "a pausa e pergunte se retoma o assunto anterior "
                                 "ou começa outro; não finja continuidade.")
                else:
                    if recent:
                        hot = _tail_lines(self._render_hot(current), 1200)
                        if hot:
                            lines.append(f"Índice quente (últimas {HOT_HOURS} h):")
                            lines.append(hot)
                    if state.get("current_topic"):
                        topic_text = self.read_topic(state["current_topic"]).strip()
                        if topic_text:
                            lines.append(f"Tópico atual "
                                         f"[[topics/{state['current_topic']}]]:")
                            lines.append(_truncate(topic_text, 350))
                    notes = state["notes"][-4:]
                    if notes:
                        lines.append("Notas recentes:")
                        for n in notes:
                            at = n.get("at", "")
                            lines.append(f"- {at[11:16] if len(at) >= 16 else at} "
                                         f"[{n.get('kind', 'note')}] "
                                         f"{n.get('text', '')[:160]}")
                lines.append("Use apenas o que está escrito aqui e nos tópicos. "
                             "Se algo não estiver registrado, diga que não lembra; "
                             "não invente detalhes.")
                text = "\n".join(lines)
                return text if len(text) <= max_chars else text[:max_chars]
        except Exception as e:
            print(f"[ChatMemory] prompt_block failed: {e}")
            return ""

    def index_block(self, max_chars=2600, now=None, resumed=False) -> str:
        """The leveled index written to ``index.md`` and read at every start.

        Level 0 is the live Qoder session itself. Levels 1-2 are injected in
        full; level 3 lists the day/topic files that memory_search can open;
        level 4 is the long-term store; level 5 is honesty — what is not
        recorded is not remembered.
        """
        try:
            max_chars = max(200, int(max_chars))
        except (TypeError, ValueError):
            max_chars = 2600
        try:
            current = _as_dt(now) or _now()
            with _LOCK:
                self._ensure_layout()
                self._ensure_hot(current)
                gap = self.check_gap(now=current)
                state = self._read_state()
                recent = self._turns_between(
                    current - timedelta(hours=HOT_HOURS), current)
                lines = ["# Índice da conversa (níveis)",
                         f"Agora: {current:%d/%m/%Y %H:%M}."]

                lines.append("## Nível 0 — sessão viva")
                if resumed:
                    lines.append("- Sessão Qoder retomada; o histórico acima já "
                                 "está no contexto. Use-o antes de buscar arquivos.")
                else:
                    lines.append("- Sessão nova: use o índice e os últimos turnos "
                                 "abaixo para esquentar a conversa.")
                lines.append(f"- {len(recent)} falas nas últimas {HOT_HOURS} h.")

                lines.append("## Nível 1 — últimas horas (hot.md)")
                if gap["long"]:
                    lines.append(f"- {gap['prompt']}")
                elif recent:
                    hot = _tail_lines(self._render_hot(current), 1200)
                    lines.append(hot)
                else:
                    lines.append("- (sem falas recentes)")

                lines.append("## Nível 2 — fio atual (state.md)")
                if state.get("current_topic"):
                    lines.append(f"- Assunto atual: [[topics/{state['current_topic']}]]")
                if state.get("open_threads"):
                    lines.append("- Threads abertas: "
                                 + "; ".join(state["open_threads"][:8]) + ".")
                notes = state.get("notes") or []
                for note in notes[-4:]:
                    at = str(note.get("at") or "")
                    lines.append(f"- Nota {at[11:16] if len(at) >= 16 else at} "
                                 f"[{note.get('kind', 'note')}] "
                                 f"{str(note.get('text') or '')[:160]}")
                if not (state.get("current_topic") or state.get("open_threads") or notes):
                    lines.append("- (sem assunto aberto registrado)")

                lines.append("## Nível 3 — horas, dias e tópicos (memory_search/read)")
                days = sorted(self.days_dir.glob("*/index.md"),
                              key=lambda p: p.parent.name, reverse=True)[:5]
                for path in days:
                    lines.append(f"- [[days/{path.parent.name}/index]]")
                topics = self._topic_links()
                if topics:
                    lines.extend(topics)
                else:
                    lines.append("- (nenhum tópico gravado ainda)")

                lines.append("## Nível 4 — longo prazo")
                lines.append("- `memory/long_term.json` via recall_memory, só "
                             "quando os níveis acima não bastarem.")
                lines.append("## Nível 5 — não existe")
                lines.append("- Se não estiver registrado, diga que não lembra; "
                             "nunca reconstrua um detalhe por dedução.")

                text = "\n".join(lines)
                if len(text) > max_chars:
                    text = text[:max_chars]
                _atomic_write(self.index_path, text + "\n")
                return text
        except Exception as e:
            print(f"[ChatMemory] index_block failed: {e}")
            return ""

    def warmup_block(self, max_chars=7000, recent_turns=14, now=None,
                     resumed=False) -> str:
        """What a fresh Qoder session reads at boot to warm the same chat.

        The index gives the shape of the memory; the last turns give the exact
        words. Both are bounded so the prompt stays cheap.
        """
        try:
            max_chars = max(500, int(max_chars))
            recent_turns = max(0, int(recent_turns))
        except (TypeError, ValueError):
            max_chars, recent_turns = 7000, 14
        try:
            index = self.index_block(max_chars=2600, now=now, resumed=resumed)
            turns = self.recent_turns(hours=HOT_HOURS, now=now)
            if recent_turns:
                turns = turns[-recent_turns:]
            lines = ["[WARM-UP DA CONVERSA — leia antes de responder]",
                     "Esta é a continuação da MESMA conversa. O índice abaixo "
                     "diz o que existe e em que nível; os últimos turnos são as "
                     "palavras exatas do fio recente. Não comece como se fosse "
                     "a primeira mensagem e não pergunte o que já está aqui."]
            if index:
                lines.append(index)
            if turns:
                lines.append("## Últimos turnos (palavras exatas, nível 0/1)")
                for turn in turns:
                    at = str(turn.get("at") or "")
                    stamp = at[11:16] if len(at) >= 16 else at
                    lines.append(f"- {stamp} {turn.get('role', '?')}: "
                                 f"{str(turn.get('text') or '')[:500]}")
            lines.append("Se precisar de um detalhe que não está aqui, use "
                         "memory_search/memory_read antes de dizer que não lembra.")
            text = "\n".join(lines)
            return text if len(text) <= max_chars else text[:max_chars]
        except Exception as e:
            print(f"[ChatMemory] warmup_block failed: {e}")
            return ""
