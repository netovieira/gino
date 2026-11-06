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
        .index/manifest.json         sha256 per file + dir signatures (below)
        .index/embeddings.json       lazily computed, persisted query vectors

SEARCH INDEX (the owner's ``athena`` recipe, adapted to a markdown store)
    ``reindex()`` keeps a manifest of sha256 hashes with per-directory
    signatures: a file whose directory signature is unchanged is not even
    re-opened, so a normal search does no full-table sweep. Only files whose
    hash changed are re-tokenised (touched set) and files that disappeared
    are pruned from the manifest, the unit cache and the embedding store.
    ``search()`` ranks with smoothed TF-IDF
    (``log(1 + n/(1+df))``) plus a recency term — recency MUST weigh here,
    unlike the reference indexer — and ties break on the stable unit id, so
    the same query always returns the same order. When an ``embed_fn`` is
    wired, cosine over the persisted lazy embeddings is the preferred path;
    any problem (no model, empty vector, dimension mismatch) falls back to
    TF-IDF, never to an empty answer. Sweeps honour a cost gate: at most
    ``max_files`` content hashes per pass, and ``dry_run=True`` reports
    without writing a single byte.

    All writes are atomic (tmp file + os.replace) and serialised by a lock.
    Malformed JSON/MD on disk never raises: the reader skips what it cannot
    parse and the next write repairs the file. No LLM calls happen here; every
    summary is derived deterministically from stored lines.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import posixpath
import re
import threading
import unicodedata
from contextlib import contextmanager
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

# ── search index knobs (athena recipe, adapted) ─────────────────────────────
INDEX_DIR_NAME = ".index"
MANIFEST_VERSION = 1
# Cost gate per sweep: a single reindex() content-hashes at most this many
# files. Unchanged files (fresh directory signature) never reach this counter,
# so normal stores sweep in O(changed), not O(files).
DEFAULT_MAX_FILES = 500
# Recency MUST weigh in the ranking (the reference indexer dropped it).
RECENCY_HALF_LIFE_DAYS = 14.0
RECENCY_WEIGHT = 0.8

_LOCK = threading.RLock()

# ``_LOCK`` only serialises threads of ONE process. Background agents and the
# live app can hold the same store open from different PIDs, so every
# read-modify-write path (turns, notes, topics, state) also takes an advisory
# flock on ``<base_dir>/.store.lock``. The gate is per-thread reentrant, so
# nested public calls do not deadlock themselves.
_gate_tls = threading.local()


@contextmanager
def _store_gate(base_dir):
    stack = getattr(_gate_tls, "stack", None)
    if stack is None:
        stack = _gate_tls.stack = []
    path = str(Path(base_dir) / ".store.lock")
    for entry in stack:
        if entry[1] == path:
            entry[2] += 1
            try:
                yield
            finally:
                entry[2] -= 1
            return
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        os.close(fd)
        raise
    entry = [fd, path, 1]
    stack.append(entry)
    try:
        yield
    finally:
        stack.remove(entry)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
    return _clip_line(text, max_chars)


def _clip_line(text, max_chars: int) -> str:
    """Corta uma linha no último ponto de frase (ou palavra) que caiba no teto.

    O corte puro por caractere devolvia frase no meio e o dono lia memória
    "podada" na tela; aqui a linha sempre termina em frase fechada ou, quando
    não há frase fechada dentro do teto, no fim da última palavra inteira.
    """
    text = str(text or "")
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return text
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = max((head.rfind(e) + 1 for e in (". ", "! ", "? ", ".\n", "!\n",
                                          "?\n", "…")), default=0)
    if cut >= max(1, int(max_chars * 0.5)):
        return head[:cut].rstrip()
    sp = head.rfind(" ")
    if sp > 0:
        return head[:sp]
    return head


def _cost(lines) -> int:
    """Custo aproximado de juntar linhas com "\\n" (1 newline por linha)."""
    return sum(len(line) + 1 for line in lines)


def _fit_lines(lines, max_chars: int) -> list:
    """Do início: mantém linhas inteiras que cabem no teto — nunca fatia."""
    kept, used = [], 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    return kept


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


def _render_section(lines, prefer, quota):
    """Render one section within its quota — whole lines only, or nothing."""
    body = lines[1:]
    room = quota - len(lines[0]) - 1
    if not body or room <= 0:
        return [], 0
    if prefer == "tail":
        kept_text = _tail_lines("\n".join(body), room)
    else:
        kept_text = "\n".join(_fit_lines(body, room))
    if not kept_text:
        return [], 0
    out = [lines[0]] + kept_text.splitlines()
    return out, _cost(out)


def _quota_sections(top, sections, max_chars):
    """Divide the budget across level sections, each with its own quota.

    O corte anterior fatiava o índice no meio da frase e deixava nível 1 com
    uma linha podada no boot. Aqui hot, state e tópicos recebem quota
    proporcional ao próprio tamanho — o que não cabe sai como linha inteira
    removida, nunca como frase pela metade — e a fatia que uma seção não usa
    (cabe inteira, ou não cabe nem uma linha) volta para as demais.
    """
    budget = max_chars - _cost(top) - 1
    full = [_cost(lines) for lines, _ in sections]
    total = sum(full) or 1
    if budget <= 0:
        return list(top)
    if total <= budget:
        out = list(top)
        for lines, _ in sections:
            out.extend(lines)
        return out
    quotas = [max(0, int(budget * f / total)) for f in full]
    rendered = []
    for _ in range(len(sections) + 1):
        rendered = [_render_section(sections[i][0], sections[i][1], quotas[i])
                    for i in range(len(sections))]
        used = sum(cost for _, cost in rendered)
        slack = budget - used
        if slack <= 0:
            break
        need = [(i, full[i] - quotas[i]) for i in range(len(sections))
                if full[i] - quotas[i] > 0]
        if not need:
            break
        need_total = sum(n for _, n in need)
        for i, n in need:
            add = min(n, max(1, int(slack * n / need_total)))
            quotas[i] += add
        while sum(quotas) > budget:  # arredondamento do max(1, ...) estourou
            for i in range(len(quotas) - 1, -1, -1):
                if sum(quotas) <= budget:
                    break
                if quotas[i] > 0:
                    quotas[i] -= 1
    out = list(top)
    for lines, _cost_done in rendered:
        out.extend(lines)
    return out


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


# ── index helpers (sha256 manifest, recency, cosine) ────────────────────────

_RE_SOURCE_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_RE_SOURCE_HOUR = re.compile(r"/(\d{2})\.md$")


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit_ts(rel: str, path: Path) -> float:
    """Epoch of a file's memory date: path first, mtime as fallback.

    The date lives in the layout (``hours/YYYY-MM-DD/HH.md``,
    ``days/YYYY-MM-DD/``, ``sleep/YYYY-MM-DD.md``); ``state.md``/``hot.md``
    carry no date and fall back to mtime, which is what the writer just set.
    """
    m = _RE_SOURCE_DATE.search(rel)
    if m:
        hour = 0
        hm = _RE_SOURCE_HOUR.search(rel)
        if hm and 0 <= int(hm.group(1)) < 24:
            hour = int(hm.group(1))
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            hour).timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return _now().timestamp()


def _recency_bonus(ts: float, now_ts: float) -> float:
    """Half-life decay quantised to whole days — deterministic per day.

    Quantisation matters: without it two runs minutes apart could break a
    tie differently, and the owner's contract is same query, same order.
    """
    age_days = int(max(0.0, now_ts - float(ts)) // 86400)
    return RECENCY_WEIGHT * (0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS))


def _is_vector(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) > 0
            and all(isinstance(x, (int, float)) for x in value))


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class ChatMemory:
    """Continuous conversation memory stored as markdown files."""

    def __init__(self, base_dir=None, embed_fn=None):
        self.base_dir = Path(base_dir).expanduser() if base_dir else DEFAULT_BASE_DIR
        self.hours_dir = self.base_dir / "hours"
        self.days_dir = self.base_dir / "days"
        self.topics_dir = self.base_dir / "topics"
        self.state_path = self.base_dir / "state.md"
        self.hot_path = self.base_dir / "hot.md"
        self.index_path = self.base_dir / "index.md"
        # Optional vectoriser (callable text -> list[float] | None). Never
        # called by default: production runs the deterministic TF-IDF path,
        # and the embedding path must degrade to it whenever the model is
        # absent, silent or of a different dimension.
        self.embed_fn = embed_fn
        self.index_dir = self.base_dir / INDEX_DIR_NAME
        self.manifest_path = self.index_dir / "manifest.json"
        self.embeddings_path = self.index_dir / "embeddings.json"
        self._units_cache: dict[str, list[tuple]] = {}
        self._embeddings: dict | None = None
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
            with _store_gate(self.base_dir), _LOCK:
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
            with _store_gate(self.base_dir), _LOCK:
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
            with _store_gate(self.base_dir), _LOCK:
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
        body = [f"- {dt:%H:%M} {label}: {_clip_line(text, 280)}"
                for dt, label, text in turns]
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
            with _store_gate(self.base_dir), _LOCK:
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
            with _store_gate(self.base_dir), _LOCK:
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
            with _store_gate(self.base_dir), _LOCK:
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
        """Record a short mark (also updates topic/threads for some kinds).

        The note is written twice, on purpose: into ``state.md`` (the live
        thread the prompt reads) and into the hour file (the permanent corpus
        ``search`` walks). Without the hour copy a note vanished as soon as
        ``MAX_NOTES`` rotated it out of state — decisions recorded by tools
        became unrecoverable while their timestamp proved they had been saved.
        """
        try:
            clean = " ".join(str(text or "").split()).strip()[:400]
            if not clean:
                return ""
            label = _slugify(str(kind or "note"), "note")[:24]
            dt = _now()
            with _store_gate(self.base_dir), _LOCK:
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
                    "at": f"{dt:%Y-%m-%d %H:%M}",
                    "kind": label,
                    "text": clean,
                })
                self._write_state(state)
                hour_path = self.hours_dir / f"{dt:%Y-%m-%d}" / f"{dt:%H}.md"
                old = _safe_read(hour_path)
                if not old.strip():
                    old = f"# {dt:%Y-%m-%d %H}h\n\n"
                note_line = f"- {dt:%H:%M} Nota [{label}]: {clean}"
                _atomic_write(hour_path, old.rstrip("\n") + "\n" + note_line + "\n")
                self._ensure_hot(dt)
            return f"Anotado: {clean[:120]}"
        except Exception as e:
            print(f"[ChatMemory] note failed: {e}")
            return ""

    # ── search ──────────────────────────────────────────────────────────────
    def _search_paths(self) -> list[Path]:
        paths: list[Path] = []
        try:
            paths.extend(sorted(self.hours_dir.glob("*/*.md"), reverse=True))
            # The spider pages (``index/hours``) carry event text recorded by
            # other agents; without them memory_search cannot see what the
            # teia indexed.
            index_hours = self.base_dir / "index" / "hours"
            if index_hours.is_dir():
                paths.extend(sorted(index_hours.glob("*/*.md"), reverse=True))
            paths.extend(sorted(self.days_dir.glob("*/index.md"), reverse=True))
            paths.extend(sorted(self.topics_dir.glob("*.md")))
            # ``sleep/`` is the compacted-night level of the owner's policy.
            sleep_dir = self.base_dir / "sleep"
            if sleep_dir.is_dir():
                paths.extend(sorted(sleep_dir.glob("*.md"), reverse=True))
        except Exception as e:
            print(f"[ChatMemory] search scan failed: {e}")
        paths.append(self.hot_path)
        paths.append(self.state_path)
        return paths

    @staticmethod
    def _source_of(path: Path, base_dir: Path) -> str:
        try:
            return path.relative_to(base_dir).as_posix()
        except ValueError:
            return path.name

    def _build_units(self, rel: str, path: Path) -> list[tuple]:
        """Tokenise one file into searchable units: (uid, source, line,
        folded, ts). Heading lines are indexed with the ``#`` stripped — the
        title of a topic page is the strongest evidence of what it is about
        ("latência do turno de voz" must find ``topics/latencia-...md``)."""
        ts = _unit_ts(rel, path)
        units: list[tuple] = []
        for idx, raw in enumerate(_safe_read(path).splitlines()):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("#").strip()
                if not line:
                    continue
            units.append((f"{rel}#{idx}", rel, line, _fold(line), ts))
        return units

    def _load_manifest(self) -> dict:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                files = raw.get("files")
                dirs = raw.get("dirs")
                return {
                    "files": files if isinstance(files, dict) else {},
                    "dirs": dirs if isinstance(dirs, dict) else {},
                }
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[ChatMemory] manifest repair ({self.manifest_path}): {e}")
        return {"files": {}, "dirs": {}}

    def _save_manifest(self, files: dict, dirs: dict) -> None:
        try:
            payload = json.dumps(
                {"version": MANIFEST_VERSION, "files": files, "dirs": dirs},
                ensure_ascii=False, indent=2, sort_keys=True,
            ) + "\n"
            _atomic_write(self.manifest_path, payload)
        except Exception as e:
            print(f"[ChatMemory] manifest write failed: {e}")

    def reindex(self, max_files=DEFAULT_MAX_FILES, dry_run=False) -> dict:
        """Incremental manifest sweep with the athena cost gate.

        - sha256 per file, persisted in ``.index/manifest.json``;
        - a signature per directory (name|size|mtime_ns of its children):
          while the signature holds, the files inside are not even opened,
          so there is no full-table hash on every search;
        - touched-set pruning: only files whose hash changed are
          re-tokenised; files that vanished are dropped from the manifest,
          the unit cache and the embedding store;
        - ``max_files`` caps how many files get content-hashed per sweep
          (the rest keep their previous state and the sweep reports
          ``capped``); ``dry_run=True`` only reports — nothing is written.
        """
        stats = {"scanned": 0, "unchanged": 0, "changed": 0, "added": 0,
                 "removed": 0, "skipped": 0, "capped": False,
                 "dry_run": bool(dry_run)}
        try:
            with _store_gate(self.base_dir), _LOCK:
                self._reindex_locked(stats, max_files, dry_run)
        except Exception as e:
            print(f"[ChatMemory] reindex failed: {e}")
        return stats

    def _reindex_locked(self, stats: dict, max_files, dry_run: bool) -> None:
        base = self.base_dir
        present: dict[str, Path] = {}
        for path in self._search_paths():
            present[self._source_of(path, base)] = path
        manifest = self._load_manifest()
        meta, old_dirs = manifest["files"], manifest["dirs"]
        stats["scanned"] = len(present)

        # Directory signatures: one hash per parent dir over the stat
        # fingerprint of its children. Unchanged signature == nobody
        # created, deleted or rewrote a file in that dir since last sweep.
        dir_items: dict[str, list[str]] = {}
        for rel, path in present.items():
            try:
                st = path.stat()
            except OSError:
                continue
            fp = f"{path.name}|{st.st_size}|{st.st_mtime_ns}"
            dir_items.setdefault(posixpath.dirname(rel), []).append(fp)
        dirs_new = {parent: _hash_text("\n".join(sorted(items)))
                    for parent, items in dir_items.items()}

        tainted: set[str] = set()  # dirs with a file skipped by the gate
        mutated = False
        hashed = 0
        for rel in sorted(present):
            path = present[rel]
            parent = posixpath.dirname(rel)
            cached = self._units_cache.get(rel)
            entry = meta.get(rel) if isinstance(meta.get(rel), dict) else None
            if (cached is not None and entry is not None and entry.get("hash")
                    and old_dirs.get(parent) == dirs_new.get(parent)):
                stats["unchanged"] += 1  # fresh signature: no re-hash at all
                continue
            if max_files is not None and hashed >= max_files:
                stats["skipped"] += 1
                stats["capped"] = True
                tainted.add(parent)
                continue
            hashed += 1
            try:
                digest = _hash_file(path)
            except OSError:
                stats["skipped"] += 1
                tainted.add(parent)
                continue
            digest_same = entry is not None and entry.get("hash") == digest
            if digest_same:
                # Content unchanged since the manifest was written. A fresh
                # process still needs the tokenised units in memory, but the
                # manifest entry itself is untouched — no prune, no rewrite.
                stats["unchanged"] += 1
                if cached is None and not dry_run:
                    self._units_cache[rel] = self._build_units(rel, path)
            else:
                stats["changed" if (entry and entry.get("hash"))
                      else "added"] += 1
                mutated = True
                if not dry_run:
                    self._units_cache[rel] = self._build_units(rel, path)
                    meta[rel] = {"hash": digest,
                                 "units": len(self._units_cache[rel])}

        removed = [rel for rel in tuple(meta) if rel not in present]
        if dry_run:
            stats["removed"] = len(removed)
            return
        for rel in removed:
            meta.pop(rel, None)
            self._units_cache.pop(rel, None)
        stats["removed"] = len(removed)
        if removed:
            mutated = True
            self._prune_embeddings(set(meta))
        # A directory whose sweep was cut by the gate keeps its OLD
        # signature, so the next sweep re-checks its files instead of
        # trusting a signature produced from an unfinished pass.
        for parent in tainted:
            dirs_new[parent] = old_dirs.get(parent)
        if mutated or not self.manifest_path.exists():
            self._save_manifest(meta, dirs_new)

    def _corpus_units(self) -> list[tuple]:
        """All indexed units in sweep order, de-duplicated by line text
        (first source wins — same rule as the bc8baa6 baseline)."""
        units: list[tuple] = []
        seen: set[str] = set()
        for path in self._search_paths():
            for unit in self._units_cache.get(
                    self._source_of(path, self.base_dir), ()):
                if unit[2] in seen:
                    continue
                seen.add(unit[2])
                units.append(unit)
        return units

    def _reload_corpus(self) -> list[tuple]:
        """Self-heal: reindex failed/was gated away — read the store once
        directly so a search never dies of an empty cache."""
        for path in self._search_paths():
            rel = self._source_of(path, self.base_dir)
            self._units_cache[rel] = self._build_units(rel, path)
        return self._corpus_units()

    def _search_tfidf(self, units: list[tuple], tokens: list[str],
                      phrase: str, limit: int) -> list[dict]:
        """Smoothed TF-IDF over the indexed units, recency-weighted.

        ``idf = log(1 + n/(1+df))`` never divides by zero and never zeroes
        out a term seen everywhere. TF stays binary per line (presence of
        the token), exactly the bc8baa6 semantics the owner reapproved —
        lines are short and repetition there is emphasis, not topic. The
        phrase bonus doubles the rarest token, and ties break on the
        stable unit id, so the order is a pure function of
        (corpus, query, day).
        """
        n = len(units)
        if not n or not tokens:
            return []
        df = {token: 0 for token in tokens}
        for _uid, _src, _line, folded, _ts in units:
            for token in tokens:
                if token in folded:
                    df[token] += 1
        idf = {token: math.log(1.0 + n / (1.0 + count))
               for token, count in df.items()}
        max_idf = max(idf.values())
        now_ts = _now().timestamp()
        scored: list[tuple] = []
        for uid, source, line, folded, ts in units:
            matched = [t for t in tokens if t in folded]
            if not matched:
                continue
            score = sum(idf[t] for t in matched)
            if len(tokens) > 1 and phrase in folded:
                score += max_idf
            score += _recency_bonus(ts, now_ts)
            scored.append((-score, uid, source, line))
        scored.sort()
        return [{"source": source, "text": line[:400]}
                for _s, _u, source, line in scored[:limit]]

    def _load_embeddings(self) -> dict:
        if self._embeddings is None:
            store: dict = {}
            try:
                raw = json.loads(self.embeddings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key, vec in raw.get("vectors", {}).items():
                        if _is_vector(vec):
                            store[str(key)] = [float(x) for x in vec]
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[ChatMemory] embeddings repair ({self.embeddings_path}): {e}")
            self._embeddings = store
        return self._embeddings

    def _save_embeddings(self) -> None:
        try:
            payload = json.dumps({"version": MANIFEST_VERSION,
                                  "vectors": self._embeddings or {}},
                                 ensure_ascii=False, sort_keys=True)
            _atomic_write(self.embeddings_path, payload + "\n")
        except Exception as e:
            print(f"[ChatMemory] embeddings write failed: {e}")

    def _prune_embeddings(self, valid_sources: set) -> None:
        """Touched-set pruning for the vector store: drop every vector whose
        source file is gone (keys start with the relative source path)."""
        if not self._embeddings:
            return
        kept = {k: v for k, v in self._embeddings.items()
                if k.partition("#")[0] in valid_sources}
        if len(kept) != len(self._embeddings):
            self._embeddings = kept
            self._save_embeddings()

    def _embed_key(self, source: str, line: str) -> str:
        # Content-addressed: an unchanged line keeps its vector across
        # reindexes; only its content changing invalidates it.
        return f"{source}#{_hash_text(line)[:16]}"

    def _search_vector(self, query: str, units: list[tuple],
                       limit: int) -> list[dict] | None:
        """Preferred path when an embed_fn is wired: cosine over lazily
        computed, persisted vectors. Returns ``None`` — forcing the TF-IDF
        fallback — on ANY inconsistency: model down, empty vector, or a
        stored vector of a different dimension (corrupt/foreign index)."""
        fn = self.embed_fn
        if fn is None or not units:
            return None
        try:
            qv = fn(query)
        except Exception:
            return None
        if not _is_vector(qv):
            return None
        dim = len(qv)
        vectors = self._load_embeddings()
        fresh = False
        now_ts = _now().timestamp()
        scored: list[tuple] = []
        for uid, source, line, _folded, ts in units:
            key = self._embed_key(source, line)
            vec = vectors.get(key)
            if vec is None:
                try:
                    vec = fn(line)
                except Exception:
                    vec = None
                if not _is_vector(vec):
                    return None  # model died mid-sweep -> mandatory fallback
                if len(vec) != dim:
                    return None  # dimension guard -> mandatory fallback
                vectors[key] = [float(x) for x in vec]
                fresh = True
            elif len(vec) != dim:
                return None
            sim = _cosine(qv, vec)
            scored.append((-(sim + _recency_bonus(ts, now_ts)),
                           uid, source, line))
        if fresh:
            self._save_embeddings()
        scored.sort()
        return [{"source": source, "text": line[:400]}
                for _s, _u, source, line in scored[:limit]]

    def search(self, query, limit=10) -> list[dict]:
        """Accent-insensitive partial search over the manifest-indexed corpus.

        Kept from the bc8baa6 base: a line matches when it holds at least
        one token (no more AND-exata), rare tokens outrank generic ones and
        an exact phrase doubles the rarest weight. Transplanted from the
        owner's athena indexer: the corpus is a sha256 manifest with
        touched-set pruning instead of a per-call full scan; IDF is the
        smoothed ``log(1 + n/(1+df))``; ranking ties break on the stable
        unit id so the same query always returns the same order; and an
        optional embedder ranks by cosine first but ALWAYS degrades to
        TF-IDF when there is no model, no vector or a dimension mismatch.
        Recency weighs in the ranking here (athena forgot it): a line gets
        a half-life decay bonus from its memory date, quantised per day.
        """
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
        phrase = _fold(text)
        try:
            # reindex() takes the flock gate first and the RLock inside; the
            # writer path takes the same order (gate -> _LOCK). Never invert
            # it here or a search and an append_turn could deadlock.
            self.reindex()
            with _LOCK:
                units = self._corpus_units()
                if not units:
                    units = self._reload_corpus()
                if self.embed_fn is not None:
                    found = self._search_vector(query, units, limit)
                    if found is not None:
                        return found
                return self._search_tfidf(units, tokens, phrase, limit)
        except Exception as e:
            print(f"[ChatMemory] search failed: {e}")
            return []

    def index_status(self) -> dict:
        """Small introspection for tools/tests: what the index holds."""
        with _LOCK:
            return {
                "files": len(self._units_cache),
                "units": sum(len(u) for u in self._units_cache.values()),
                "embeddings": len(self._embeddings or {}),
                "embedder": self.embed_fn is not None,
            }

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
                                         f"{_clip_line(n.get('text', ''), 160)}")
                lines.append("Use apenas o que está escrito aqui e nos tópicos. "
                             "Se algo não estiver registrado, diga que não lembra; "
                             "não invente detalhes.")
                text = "\n".join(lines)
                if len(text) > max_chars:
                    # a linha de fechamento (regra dura) tem quota reservada;
                    # o corpo sai em linhas inteiras, nunca fatiado no meio.
                    tail_line = lines[-1]
                    if len(tail_line) + 1 > max_chars:
                        text = _clip_line(tail_line, max_chars)
                    else:
                        body = _fit_lines(lines[:-1],
                                          max_chars - len(tail_line) - 1)
                        text = "\n".join(body + [tail_line])
                return text
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

                sec0 = ["## Nível 0 — sessão viva"]
                if resumed:
                    sec0.append("- Sessão Qoder retomada; o histórico acima já "
                                "está no contexto. Use-o antes de buscar arquivos.")
                else:
                    sec0.append("- Sessão nova: use o índice e os últimos turnos "
                                "abaixo para esquentar a conversa.")
                sec0.append(f"- {len(recent)} falas nas últimas {HOT_HOURS} h.")

                sec1 = ["## Nível 1 — últimas horas (hot.md)"]
                if gap["long"]:
                    sec1.append(f"- {gap['prompt']}")
                elif recent:
                    hot = _tail_lines(self._render_hot(current), 1200)
                    sec1.extend(hot.splitlines())
                else:
                    sec1.append("- (sem falas recentes)")

                sec2 = ["## Nível 2 — fio atual (state.md)"]
                if state.get("current_topic"):
                    sec2.append(f"- Assunto atual: [[topics/{state['current_topic']}]]")
                if state.get("open_threads"):
                    sec2.append("- Threads abertas: "
                                + "; ".join(state["open_threads"][:8]) + ".")
                notes = state.get("notes") or []
                for note in notes[-4:]:
                    at = str(note.get("at") or "")
                    sec2.append(f"- Nota {at[11:16] if len(at) >= 16 else at} "
                                f"[{note.get('kind', 'note')}] "
                                f"{_clip_line(str(note.get('text') or ''), 160)}")
                if not (state.get("current_topic") or state.get("open_threads") or notes):
                    sec2.append("- (sem assunto aberto registrado)")

                sec3 = ["## Nível 3 — horas, dias e tópicos (memory_search/read)"]
                days = sorted(self.days_dir.glob("*/index.md"),
                              key=lambda p: p.parent.name, reverse=True)[:5]
                for path in days:
                    sec3.append(f"- [[days/{path.parent.name}/index]]")
                topics = self._topic_links()
                sec3.extend(topics)
                if not topics and not days:
                    sec3.append("- (nenhum tópico gravado ainda)")

                sec4 = ["## Nível 4 — longo prazo",
                        "- `memory/long_term.json` via recall_memory, só "
                        "quando os níveis acima não bastarem."]
                sec5 = ["## Nível 5 — não existe",
                        "- Se não estiver registrado, diga que não lembra; "
                        "nunca reconstrua um detalhe por dedução."]

                sections = [(sec0, "head"), (sec1, "tail"), (sec2, "head"),
                            (sec3, "head"), (sec4, "head"), (sec5, "head")]
                text = "\n".join(_quota_sections(lines, sections, max_chars))
                if len(text) > max_chars:
                    # emergência: teto apertado demais até para as seções
                    # montadas; corta em linha inteira, nunca na metade da frase.
                    text = "\n".join(_fit_lines(text.splitlines(), max_chars))
                _atomic_write(self.index_path, text + "\n")
                return text
        except Exception as e:
            print(f"[ChatMemory] index_block failed: {e}")
            return ""

    def warmup_block(self, max_chars=7000, recent_turns=14, now=None,
                     resumed=False, index_chars=None) -> str:
        """What a fresh Qoder session reads at boot to warm the same chat.

        The index gives the shape of the memory; the last turns give the exact
        words. Both are bounded so the prompt stays cheap. ``index_chars``
        caps the level index on its own (the voice turn sends a much smaller
        prefill than the session's default); ``None`` keeps the historical
        cap so existing callers are untouched.
        """
        try:
            max_chars = max(500, int(max_chars))
            recent_turns = max(0, int(recent_turns))
        except (TypeError, ValueError):
            max_chars, recent_turns = 7000, 14
        try:
            index_max = (int(index_chars) if index_chars is not None
                         else 2600)
            index_max = max(200, index_max)
        except (TypeError, ValueError):
            index_max = 2600
        try:
            if max_chars >= 2000:
                head = ["[WARM-UP DA CONVERSA — leia antes de responder]",
                        "Esta é a continuação da MESMA conversa. O índice abaixo "
                        "diz o que existe e em que nível; os últimos turnos são as "
                        "palavras exatas do fio recente. Não comece como se fosse "
                        "a primeira mensagem e não pergunte o que já está aqui."]
                footer = ("Se precisar de um detalhe que não está aqui, use "
                          "memory_search/memory_read antes de dizer que não lembra.")
            else:
                # No teto curto do boot cada caractere dos fixos sai da conta
                # dos turnos: cabeçalho e rodapé viram uma linha e o resto
                # vira quota de memória, não de prosa.
                head = ["[WARM-UP DA CONVERSA — leia antes de responder]",
                        "Continuação da MESMA conversa: o índice dá os níveis, "
                        "os turnos abaixo dão as palavras exatas. Não pergunte "
                        "o que já está aqui."]
                footer = ("Detalhe ausente? Use memory_search/memory_read antes "
                          "de dizer que não lembra.")
            turns = self.recent_turns(hours=HOT_HOURS, now=now)
            if recent_turns:
                turns = turns[-recent_turns:]
            turn_lines = []
            for turn in turns:
                at = str(turn.get("at") or "")
                stamp = at[11:16] if len(at) >= 16 else at
                turn_lines.append(f"- {stamp} {turn.get('role', '?')}: "
                                  f"{_clip_line(str(turn.get('text') or ''), 500)}")
            # Quota própria: no teto curto do boot o índice não pode engolir os
            # turnos exatos — era isso que deixava "Últimos turnos" com uma
            # linha fatiada no meio da frase. Metade da sala para o índice, o
            # resto para as palavras exatas; o que o índice não usa volta.
            room = max_chars - _cost(head) - len(footer) - 2
            index_cap = min(index_max, max(200, room // 2))
            index = ""
            if room > 0:
                index = self.index_block(max_chars=index_cap, now=now,
                                         resumed=resumed)
            lines = list(head)
            if index:
                idx_room = room - _cost(index.splitlines()) - 1
                if idx_room < 0:
                    index = "\n".join(_fit_lines(index.splitlines(),
                                                 room - 1))
                idx_lines = [l for l in index.splitlines() if l]
                if idx_lines:
                    lines.extend(idx_lines)
                    room = max_chars - _cost(lines) - len(footer) - 2
            if turn_lines and room > 0:
                hdr = "## Últimos turnos (palavras exatas, nível 0/1)"
                body_room = room - len(hdr) - 1
                kept = _tail_lines("\n".join(turn_lines), body_room)
                if not kept and body_room > 0:
                    # O turno mais novo sozinho não cabe na sala: entra podado
                    # em frase/palavra inteira — seção vazia no boot é o
                    # sintoma que o dono chamou de "memória não carrega".
                    kept = _clip_line(turn_lines[-1], body_room)
                if kept:
                    lines.append(hdr)
                    lines.extend(kept.splitlines())
            lines.append(footer)
            text = "\n".join(lines)
            if len(text) > max_chars:
                # emergência: teto menor que os fixos; linha inteira, nunca frase.
                text = "\n".join(_fit_lines(lines, max_chars))
            return text
        except Exception as e:
            print(f"[ChatMemory] warmup_block failed: {e}")
            return ""
