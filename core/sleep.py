"""Nightly sleep: compact the day into one small, durable memory file.

WHAT THIS IS
    The conversation store (core/chat_memory.py) keeps the day in hour files.
    They are excellent for searching and terrible for a warm start: too many
    words, too much repetition. This module writes ONE compact markdown file
    per day under ``memory/chat/sleep/`` — a narrative summary plus the
    deterministic evidence it was built from — and remembers the last night it
    processed in ``state.json``.

    Two entry points:

      bedtime_tick()  called while the app is open. At the configured time it
                      announces "Vou dormir...", compacts today and returns the
                      file path.
      catch_up()      called at boot. If the app was closed for three nights,
                      it silently compacts every missing night (no arbitrary
                      day cap) and returns the paths so the caller can warm the
                      next session with latest_context().

WHAT IT MUST NEVER DO
    Speak during catch-up, redo a night that already has a file, block on a
    broken state file, or put a whole transcript into the compact memory. The
    summary comes from Qoder when available; when Qoder is missing or fails,
    the deterministic compact markdown is the summary — never an exception.

STORAGE
    memory/chat/sleep/YYYY-MM-DD.md   compact days (frontmatter + Resumo)
    memory/chat/sleep/state.json      {"last_sleep": "YYYY-MM-DD", ...}
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from core import memory_index, qoder
from core.chat_memory import ChatMemory
from memory import config_manager

SLEEP_DIR = Path(__file__).resolve().parent.parent / "memory" / "chat" / "sleep"
STATE_PATH = SLEEP_DIR / "state.json"
CHAT_DIR = Path(__file__).resolve().parent.parent / "memory" / "chat"
DEFAULT_TIME = "23:30"
DEFAULT_MODEL = "Qwen3.8-Flash"
MAX_BUILD_CHARS = 6000
MAX_SUMMARY_CHARS = 1200
MAX_CONTEXT_FILES = 3

_LOCK = threading.RLock()
_TICK_LOCK = threading.Lock()

_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_INDEX_HOUR = re.compile(
    r"\[(\d{2})h\]\([^)]*\)\s*[—–-]\s*(\d{2}:\d{2})[–-](\d{2}:\d{2}),"
    r"\s*(\d+)\s*fala"
)
_RE_INDEX_TOTAL = re.compile(r"Total:\s*(\d+)\s+falas em\s+(\d+)\s+hora")
_RE_HOUR_TURN = re.compile(r"^-\s+(\d{2}:\d{2})\s+([^:]{1,40}):\s+(.+)$")
_RE_NOTE = re.compile(
    r"^-\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s+\[([^\]]+)\]\s+(.*)$"
)
_RE_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)
_RE_LAST_SEEN = re.compile(
    r"^-\s+last_seen:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2})",
    re.M,
)
_RE_CURRENT_TOPIC = re.compile(r"^-\s+current_topic:\s*(.*)$", re.M)
_RE_OPEN_THREADS = re.compile(r"^-\s+open_threads:\s*(.*)$", re.M)


# ── small helpers ───────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now()


def _coerce_now(value=None) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, dtime.min)
    if value is not None:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            return _coerce_now(parsed)
        except ValueError:
            pass
    return _now()


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _key(day) -> str:
    return f"{day:%Y-%m-%d}"


def _fold(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _dedupe(values, limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        clean = " ".join(str(value or "").split()).strip()
        if not clean:
            continue
        folded = clean.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(clean)
        if limit is not None and len(out) >= limit:
            break
    return out


def _clip(text, limit: int) -> str:
    text = str(text or "")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MAX_BUILD_CHARS
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _safe_read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _atomic_write(path, text: str) -> Path:
    """Write ``text`` to ``path`` through a same-directory temp file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(text))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return target


def _as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in ("0", "false", "no", "nao", "não", "off", "disabled"):
        return False
    if text in ("1", "true", "yes", "sim", "on", "enabled"):
        return True
    return default


def _valid_time(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour:02d}:{minute:02d}"


def _local_timezone() -> str:
    try:
        return datetime.now().astimezone().tzname() or "local"
    except Exception:
        return "local"


def load_api_keys() -> dict:
    """Thin seam so tests can replace the configuration reader."""
    try:
        data = config_manager.load_api_keys()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── settings / state ────────────────────────────────────────────────────────
def settings() -> dict:
    """Sleep settings from the root of config/api_keys.json, never raising."""
    try:
        cfg = load_api_keys()
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": _as_bool(cfg.get("sleep_enabled"), True),
        "time": _valid_time(cfg.get("sleep_time")) or DEFAULT_TIME,
        "model": str(cfg.get("sleep_model") or "").strip() or DEFAULT_MODEL,
        "timezone": str(cfg.get("sleep_timezone") or "").strip()
        or _local_timezone(),
    }


def _read_state() -> dict:
    try:
        raw = Path(STATE_PATH).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def last_sleep() -> str | None:
    """Last night already compacted, or None when state is absent/malformed."""
    with _LOCK:
        data = _read_state()
    day = _as_date(data.get("last_sleep"))
    return _key(day) if day else None


def _write_state(last_day: date) -> None:
    data = _read_state()
    previous = _as_date(data.get("last_sleep"))
    chosen = max(d for d in (previous, last_day) if d is not None)
    data["last_sleep"] = _key(chosen)
    data["updated"] = _now().strftime("%Y-%m-%dT%H:%M:%S")
    _atomic_write(
        STATE_PATH,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
    )


# ── dates with conversation ─────────────────────────────────────────────────
def _data_days() -> list[date]:
    """Every day folder present under the chat store's days/hours folders."""
    found: set[date] = set()
    bases = {Path(CHAT_DIR)}
    try:
        base = getattr(_chat_memory(), "base_dir", None)
        if base:
            bases.add(Path(base))
    except Exception:
        pass
    for base in bases:
        for folder_name in ("days", "hours"):
            folder = base / folder_name
            try:
                children = list(folder.iterdir())
            except Exception:
                continue
            for child in children:
                if child.is_dir() and _RE_DATE.match(child.name):
                    day = _as_date(child.name)
                    if day:
                        found.add(day)
    return sorted(found)


def _latest_day() -> date | None:
    days = _data_days()
    return days[-1] if days else None


def missing_nights(now=None) -> list[str]:
    """Nights after the last sleep up to yesterday, oldest first.

    With no usable state the series starts at the earliest day found in the
    chat store, so a first boot does not walk back to the epoch. There is no
    arbitrary cap: a month away is a month of nights.
    """
    current = _coerce_now(now)
    yesterday = current.date() - timedelta(days=1)
    previous = _as_date(last_sleep())
    if previous is not None:
        start = previous + timedelta(days=1)
    else:
        days = _data_days()
        if not days:
            return []
        start = days[0]
    nights: list[str] = []
    cursor = start
    while cursor <= yesterday:
        nights.append(_key(cursor))
        cursor += timedelta(days=1)
    return nights


# ── reading the day ─────────────────────────────────────────────────────────
def _chat_memory():
    try:
        return ChatMemory(CHAT_DIR)
    except TypeError:
        return ChatMemory()


def _mem_read(mem, rel: str) -> str:
    try:
        reader = getattr(mem, "read", None)
        if callable(reader):
            text = reader(rel)
            if text:
                return str(text)
    except Exception:
        pass
    try:
        base = Path(getattr(mem, "base_dir", CHAT_DIR))
        path = base / rel
        if path.is_file():
            return _safe_read(path)
    except Exception:
        pass
    return ""


def _parse_state(mem) -> dict:
    """Structured state from ChatMemory, with a text fallback for fakes."""
    state = {"last_seen": "", "current_topic": "", "open_threads": [],
             "notes": []}
    try:
        reader = getattr(mem, "_read_state", None)
        if callable(reader):
            raw = reader()
            if isinstance(raw, dict):
                state["last_seen"] = str(raw.get("last_seen") or "")
                state["current_topic"] = str(raw.get("current_topic") or "")
                state["open_threads"] = [
                    str(t) for t in (raw.get("open_threads") or []) if str(t).strip()
                ]
                for note in raw.get("notes") or []:
                    if isinstance(note, dict):
                        state["notes"].append({
                            "at": str(note.get("at") or ""),
                            "kind": str(note.get("kind") or "note"),
                            "text": str(note.get("text") or ""),
                        })
                return state
    except Exception:
        pass

    text = _mem_read(mem, "state.md")
    match = _RE_LAST_SEEN.search(text)
    if match:
        state["last_seen"] = match.group(1).strip()
    match = _RE_CURRENT_TOPIC.search(text)
    if match:
        value = match.group(1).strip()
        value = re.sub(r"^\[\[topics/([^\]]+)\]\]\s*—?\s*", r"\1", value)
        state["current_topic"] = value.split("—")[0].strip()
    match = _RE_OPEN_THREADS.search(text)
    if match:
        state["open_threads"] = [
            part.strip()
            for part in re.split(r"[|;]", match.group(1))
            if part.strip()
        ]
    for match in _RE_NOTE.finditer(text):
        state["notes"].append({
            "at": f"{match.group(1)} {match.group(2)}",
            "kind": match.group(3).strip(),
            "text": match.group(4).strip(),
        })
    return state


def _state_day(state: dict) -> date | None:
    day = _as_date(state.get("last_seen"))
    if day:
        return day
    notes = [_as_date(n.get("at")) for n in state.get("notes") or []]
    notes = [n for n in notes if n]
    return max(notes) if notes else None


def _parse_index_hours(text: str) -> tuple[list[dict], int, int]:
    hours: list[dict] = []
    for match in _RE_INDEX_HOUR.finditer(text or ""):
        hours.append({
            "hour": match.group(1),
            "start": match.group(2),
            "end": match.group(3),
            "turns": int(match.group(4)),
        })
    total = sum(h["turns"] for h in hours)
    match = _RE_INDEX_TOTAL.search(text or "")
    if match:
        total = max(total, int(match.group(1)))
    return hours, total, len(hours)


def _scan_hour_files(mem, day: date) -> tuple[list[dict], int]:
    folder = Path(getattr(mem, "hours_dir", Path(CHAT_DIR) / "hours")) / _key(day)
    hours: list[dict] = []
    try:
        paths = sorted(folder.glob("*.md"))
    except Exception:
        paths = []
    for path in paths:
        text = _safe_read(path)
        if not text:
            rel = f"hours/{_key(day)}/{path.name}"
            text = _mem_read(mem, rel)
        times: list[str] = []
        turns = 0
        for line in text.splitlines():
            match = _RE_HOUR_TURN.match(line.strip())
            if match:
                turns += 1
                times.append(match.group(1))
        if turns:
            hours.append({
                "hour": path.stem[:2],
                "start": min(times),
                "end": max(times),
                "turns": turns,
            })
    return hours, sum(h["turns"] for h in hours)


def _topic_entries(mem, state: dict, day: date, day_notes: list[dict]) -> list[dict]:
    day_key = _key(day)
    wanted: list[str] = []
    state_day = _state_day(state)
    if state.get("current_topic") and (
        state_day == day or (state_day is None and day == _latest_day())
    ):
        wanted.append(str(state["current_topic"]))
    for note in day_notes:
        if _fold(note.get("kind")).strip() in ("topico", "topic", "assunto"):
            wanted.append(str(note.get("text") or ""))
    wanted = _dedupe(wanted)

    topics_dir = Path(getattr(mem, "topics_dir", Path(CHAT_DIR) / "topics"))
    paths: list[Path] = []
    try:
        paths = sorted(topics_dir.glob("*.md"))[:200]
    except Exception:
        paths = []

    # Explicitly wanted topics first (state/notes), then any topic whose file
    # mentions this date. Keep the order deterministic.
    chosen: dict[str, Path] = {}
    for slug in wanted:
        if slug:
            chosen.setdefault(slug, topics_dir / f"{slug}.md")
    date_token = f"{day:%d/%m/%Y}"
    for path in paths:
        if path.stem in chosen:
            continue
        text = _safe_read(path)
        if day_key in text or date_token in text:
            chosen.setdefault(path.stem, path)

    entries: list[dict] = []
    for slug, path in chosen.items():
        text = _safe_read(path)
        heading = _RE_HEADING.search(text)
        title = " ".join(heading.group(1).split())[:80] if heading else ""
        entries.append({"slug": slug, "title": title})
        if len(entries) >= 8:
            break
    return entries


def _query_events(day: date) -> list[dict]:
    start = datetime.combine(day, dtime.min)
    end = datetime.combine(day, dtime.max)
    try:
        events = memory_index.query(since=start, until=end, limit=300)
    except Exception:
        return []
    if not isinstance(events, (list, tuple)):
        return []
    day_key = _key(day)
    found: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        at = str(event.get("at") or "")
        if at and at[:10] and at[:10] != day_key:
            continue
        found.append(event)
    return found


def _open_threads(state: dict, day: date, day_notes: list[dict]) -> list[str]:
    state_day = _state_day(state)
    if state_day == day or (state_day is None and day == _latest_day()):
        return _dedupe(state.get("open_threads") or [], limit=12)

    threads: list[str] = []
    for note in day_notes:
        kind = _fold(note.get("kind")).strip()
        text = " ".join(str(note.get("text") or "").split()).strip()
        if not text:
            continue
        if kind in ("aberta", "thread", "thread-aberta"):
            if text.casefold() not in {t.casefold() for t in threads}:
                threads.append(text[:160])
        elif kind in ("resolve", "resolvida", "fechada", "closed", "done"):
            wanted = text.casefold()
            threads = [t for t in threads if t.casefold() != wanted]
    return threads[:12]


def build_day(day) -> str:
    """Deterministic compact markdown for one date, capped at 6000 chars.

    Counts, hours, topics, state notes, open threads and indexed facets only:
    never the hour files' text. The output is both the Qoder input and the
    fallback summary when Qoder cannot answer.
    """
    target = _as_date(day)
    if target is None:
        return ""
    day_key = _key(target)
    try:
        mem = _chat_memory()
        index_text = ""
        try:
            index_text = str(mem.build_day_index(target) or "")
        except Exception:
            index_text = ""
        if not index_text:
            index_text = _mem_read(mem, f"days/{day_key}/index.md")

        hours, total, hours_count = _parse_index_hours(index_text)
        if not hours:
            scanned, scanned_total = _scan_hour_files(mem, target)
            if scanned:
                hours, total = scanned, scanned_total
                hours_count = len(scanned)
        if total < sum(h["turns"] for h in hours):
            total = sum(h["turns"] for h in hours)

        state = _parse_state(mem)
        day_notes = [
            note for note in state.get("notes") or []
            if str(note.get("at") or "")[:10] == day_key
        ]
        threads = _open_threads(state, target, day_notes)
        topics = _topic_entries(mem, state, target, day_notes)
        events = _query_events(target)

        personas = _dedupe(e.get("persona") for e in events)
        projects = _dedupe(e.get("project") for e in events)
        subjects = _dedupe(e.get("subject") for e in events)
        kinds = Counter(str(e.get("kind") or "evento") for e in events)

        if hours:
            span = f"{hours[0]['start']}–{hours[-1]['end']}"
        else:
            span = "sem falas"

        lines = [
            f"# Sono — {day_key}",
            "",
            "## Dia",
            f"- Data: {day_key}",
            f"- Falas: {total} em {hours_count} hora"
            f"{'s' if hours_count != 1 else ''} ({span})",
            f"- Índice: [memory/chat/days/{day_key}/index.md]"
            f"(../days/{day_key}/index.md)",
            "",
            "## Horas",
        ]
        if hours:
            for item in hours[:24]:
                lines.append(
                    f"- [{item['hour']}h](../hours/{day_key}/{item['hour']}.md) — "
                    f"{item['start']}–{item['end']}, {item['turns']} fala"
                    f"{'s' if item['turns'] != 1 else ''}"
                )
        else:
            lines.append("- (nenhuma hora registrada)")

        lines.extend(["", "## Tópicos"])
        if topics:
            for topic in topics:
                label = topic["title"] or topic["slug"]
                lines.append(
                    f"- [[topics/{topic['slug']}]] — {label} "
                    f"([arquivo](../topics/{topic['slug']}.md))"
                )
        else:
            lines.append("- (nenhum tópico registrado)")

        lines.extend(["", "## Fios em aberto"])
        if threads:
            lines.extend(f"- {thread}" for thread in threads)
        else:
            lines.append("- (nenhum fio aberto registrado)")

        lines.extend(["", "## Decisões e notas"])
        decisions = [
            note for note in day_notes
            if _fold(note.get("kind")).strip()
            not in ("aberta", "thread", "thread-aberta", "resolve", "resolvida",
                    "fechada", "closed", "done", "topico", "topic", "assunto")
        ]
        if decisions:
            for note in decisions[-8:]:
                at = str(note.get("at") or "")
                stamp = at[11:16] if len(at) >= 16 else at
                text = " ".join(str(note.get("text") or "").split())[:260]
                lines.append(f"- {stamp} [{note.get('kind', 'note')}] {text}")
        else:
            lines.append("- (nenhuma decisão ou nota registrada)")

        lines.extend(["", "## Personas e projetos"])
        lines.append("- Personas: " + (", ".join(personas) or "(nenhuma registrada)"))
        lines.append("- Projetos: " + (", ".join(projects) or "(nenhum registrado)"))
        if subjects:
            lines.append("- Assuntos indexados: " + ", ".join(subjects[:10]))
        if events:
            kinds_text = ", ".join(
                f"{kind} ({count})" for kind, count in kinds.most_common(8)
            )
            lines.append(f"- Eventos indexados: {len(events)} — {kinds_text}")
        else:
            lines.append("- Eventos indexados: (nenhum)")

        return _clip("\n".join(lines), MAX_BUILD_CHARS)
    except Exception as exc:  # noqa: BLE001 - malformed memory must not raise
        print(f"[Sleep] build_day failed ({day_key}): {exc}")
        return _clip(
            f"# Sono — {day_key}\n\n## Dia\n- Data: {day_key}\n"
            f"- Índice: [memory/chat/days/{day_key}/index.md]"
            f"(../days/{day_key}/index.md)\n",
            MAX_BUILD_CHARS,
        )


# ── summarization ───────────────────────────────────────────────────────────
def _summary_prompt(markdown: str, day_key: str) -> str:
    return (
        f"Você está compactando o dia {day_key} da conversa entre o dono e o "
        "JARVIS. Leia a base determinística abaixo (contagens, horas, tópicos, "
        "notas, fios abertos, personas e projetos) e escreva um resumo "
        "narrativo em português do Brasil.\n\n"
        "Regras:\n"
        "- 1 a 3 parágrafos corridos, no máximo 1200 caracteres.\n"
        "- Preserve decisões tomadas, preferências do dono, pendências/fios "
        "abertos e nomes de personas e projetos.\n"
        "- Não invente nada que não esteja na base; se algo não estiver "
        "registrado, não mencione.\n"
        "- Não copie transcrições, não use listas, títulos, marcadores nem "
        "blocos de código.\n"
        "- Responda apenas com o resumo, sem preâmbulo.\n\n"
        "--- BASE DETERMINÍSTICA ---\n"
        f"{markdown}"
    )


def _clean_summary(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _clip(text, MAX_SUMMARY_CHARS).strip()


def summarize(markdown, day, ask=None) -> str:
    """A short daily narrative, or ``markdown`` when no model can provide one.

    ``ask`` is injected by tests and callers (one positional prompt). When it
    is None the module asks Qoder with the configured sleep model. Any error,
    empty answer or non-string answer falls back to the deterministic text.
    """
    base = str(markdown or "")
    target = _as_date(day)
    day_key = _key(target) if target else str(day or "")
    prompt = _summary_prompt(base, day_key)

    result = None
    if callable(ask):
        try:
            result = ask(prompt)
        except Exception:
            result = None
    else:
        try:
            cfg = settings()
            result = qoder.ask(
                prompt,
                model=cfg["model"],
                system=(
                    "Você é o JARVIS, assistente pessoal do dono. Sua tarefa é "
                    "resumir um dia de conversa de forma fiel, curta e útil "
                    "para o próximo dia. Nunca invente detalhes."
                ),
                max_tokens=MAX_SUMMARY_CHARS,
            )
        except Exception:
            result = None

    cleaned = _clean_summary(result)
    return cleaned or base


# ── writing a night ─────────────────────────────────────────────────────────
def write_day(day, summary, deterministic="") -> Path:
    """Atomically write one sleep file and advance ``state.json``."""
    target = _as_date(day)
    if target is None:
        raise ValueError(f"dia inválido: {day!r}")
    day_key = _key(target)
    summary_text = str(summary or "").strip()
    base_text = str(deterministic or "").strip()
    cfg = settings()

    if not summary_text or (base_text and summary_text == base_text):
        source = "deterministic"
    else:
        source = "qoder"

    body = [
        "---",
        f"date: {day_key}",
        f"generated_at: {_now():%Y-%m-%dT%H:%M:%S}",
        f"model: {cfg['model']}",
        f"source: {source}",
        "---",
        "",
        "## Resumo",
        "",
        summary_text or "Sem conversa registrada nesse dia.",
        "",
    ]
    if base_text:
        body.extend([
            "## Base determinística",
            "",
            "<details>",
            f"<summary>Compactação determinística — {day_key}</summary>",
            "",
            base_text,
            "",
            "</details>",
            "",
        ])

    with _LOCK:
        path = _atomic_write(Path(SLEEP_DIR) / f"{day_key}.md", "\n".join(body))
        _write_state(target)
    return path


def catch_up(now=None, ask=None, on_done=None) -> list[Path]:
    """Silently compact every missing night; returns the written paths."""
    written: list[Path] = []
    for day_key in missing_nights(now):
        try:
            markdown = build_day(day_key)
            summary = summarize(markdown, day_key, ask=ask)
            path = write_day(day_key, summary, deterministic=markdown)
        except Exception as exc:  # noqa: BLE001 - one bad night must not stop
            print(f"[Sleep] catch_up failed ({day_key}): {exc}")
            continue
        written.append(path)
        if callable(on_done):
            try:
                on_done(path)
            except Exception:
                pass
    return written


def bedtime_tick(now=None, ask=None, announce=None) -> Path | None:
    """Compact today once the configured bedtime has passed.

    Does nothing when sleep is disabled, before bedtime, or when today's file
    already exists. Announcement happens before the (possibly slow) summary.
    """
    current = _coerce_now(now)
    cfg = settings()
    if not cfg["enabled"]:
        return None
    try:
        hour, minute = (int(part) for part in cfg["time"].split(":"))
        due = dtime(hour, minute)
    except (TypeError, ValueError):
        due = dtime(23, 30)
    if current.time().replace(tzinfo=None) < due:
        return None

    day = current.date()
    path = Path(SLEEP_DIR) / f"{_key(day)}.md"
    if path.exists():
        return None

    if not _TICK_LOCK.acquire(blocking=False):
        return None
    try:
        if path.exists():
            return None
        if callable(announce):
            try:
                announce("Vou dormir...")
            except Exception:
                pass
        markdown = build_day(day)
        summary = summarize(markdown, day, ask=ask)
        return write_day(day, summary, deterministic=markdown)
    except Exception as exc:  # noqa: BLE001
        print(f"[Sleep] bedtime_tick failed ({_key(day)}): {exc}")
        return None
    finally:
        _TICK_LOCK.release()


# ── context for a warm start ────────────────────────────────────────────────
def _summary_body(text: str) -> str:
    """The Resumo section when present, else the file without frontmatter."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(r"^##\s+Resumo\s*$", raw, re.M)
    if match:
        tail = raw[match.end():]
        stop = re.search(r"^##\s+", tail, re.M)
        if stop:
            tail = tail[: stop.start()]
        return tail.strip()
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            raw = parts[2].strip()
    return raw


def latest_context(max_chars=2500) -> str:
    """The newest sleep files as a bounded prompt block, or "" when none."""
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = 2500
    if limit <= 0:
        return ""

    files: list[tuple[date, Path]] = []
    try:
        for path in Path(SLEEP_DIR).glob("*.md"):
            day = _as_date(path.stem)
            if day:
                files.append((day, path))
    except Exception:
        files = []
    if not files:
        return ""
    files.sort(key=lambda item: item[0], reverse=True)

    header = "[SONO — memória compactada]"
    remaining = limit - len(header) - 2
    segments: list[str] = []
    for day, path in files[:MAX_CONTEXT_FILES]:
        raw_file = _safe_read(path)
        body = _summary_body(raw_file)
        if len(body.strip()) < 200:
            # Deterministic summaries contain their own subheadings; the
            # Resumo-only extraction stops at the first "##". Fall back to the
            # full body so the first boot after a catch-up still carries the
            # day's compact context.
            full = raw_file
            if full.startswith("---"):
                parts = full.split("---", 2)
                if len(parts) == 3:
                    full = parts[2]
            full = full.split("## Base determinística", 1)[0]
            full = re.sub(r"^##\s+Resumo\s*$", "", full, count=1,
                          flags=re.M).strip()
            body = full
        if not body:
            continue
        segment = f"## {_key(day)}\n{body}"
        if remaining <= 0:
            break
        if len(segment) > remaining:
            segment = _clip(segment, remaining)
        if not segment.strip():
            break
        segments.append(segment)
        remaining -= len(segment) + 2

    if not segments:
        return ""
    segments.reverse()  # oldest first; the newest is still complete
    text = header + "\n\n" + "\n\n".join(segments)
    return text[:limit].rstrip()


def reset() -> None:
    """Wipe sleep files and state (tests / owner request)."""
    with _LOCK:
        try:
            shutil.rmtree(Path(SLEEP_DIR), ignore_errors=True)
        except Exception:
            pass
        try:
            state = Path(STATE_PATH)
            if state.exists():
                state.unlink()
        except Exception:
            pass
