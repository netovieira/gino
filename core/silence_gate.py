"""Modo silêncio — a boca cala, a orelha e o cérebro continuam.

WHY THIS EXISTS
    The owner dictates, by voice, when the assistant must stop talking about
    something (or about everything) for a while. The microphone never closes:
    the release order arrives through the very same ear. What is muted is the
    MOUTH, never the brain — memory, background work and transcription keep
    running exactly as before.

TRIGGER (voice only)
    "silêncio" alone        → mute everything
    "silêncio TheroTV"      → mute that subject only; other subjects still talk
    repeating with a name accumulates subjects; repeating alone releases all,
    repeating a name toggles that subject. The owner's voiceprint is required
    by main.py (core/groq path); typed text never reaches this gate.

STATE
    ``memory/silence_mode.json``::

        {"all": false,
         "subjects": [{"slug": "therotv", "label": "TheroTV"}],
         "released": [],
         "updated_at": "2026-11-02T19:30:00"}

    ``all`` is the "everything muted" switch. ``released`` exists only while
    ``all`` is on: it holds the subjects the owner explicitly released by name
    ("silêncio TheroTV" said while everything was muted), so a name order can
    release one subject without opening the whole room.

RETENTION
    ``memory/silence_queue.json`` keeps the speech that would have been spoken,
    with its subject, voice (persona voice_id), destination and time. The audio
    is re-synthesised at release from the exact text and voice; storing PCM for
    a queue that can survive a restart would cost disk for voices that may no
    longer exist. The cap is small on purpose: past it the oldest line is
    dropped and the release summary says how many were dropped.

SUBJECT KEYS
    Slugs come from ``core.memory_index.slugify`` — the same keys the memory
    spider web already uses for ``subject:``/``project:``, so nothing new is
    invented here.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from core.memory_index import slugify

BASE = Path(__file__).resolve().parents[1] / "memory"
DEFAULT_STATE_PATH = BASE / "silence_mode.json"
DEFAULT_QUEUE_PATH = BASE / "silence_queue.json"
DEFAULT_MAX_RETAINED = 40
DEFAULT_PAUSE_S = 0.6
MAX_SUMMARY_SUBJECTS = 8
MAX_SNIPPET = 160

_FOLD_RE = re.compile(r"[^a-z0-9]+")


def _fold(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    return raw.encode("ascii", "ignore").decode("ascii").lower()


def _tokens(text: Any) -> set[str]:
    return {t for t in _FOLD_RE.split(_fold(text)) if t}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# ── order parsing ───────────────────────────────────────────────────────────

_LEAD_FILLER_RE = re.compile(
    r"^(?:por\s+favor|pfv|agora|j[aá]|hei|ei)[\s,;:.-]*", re.IGNORECASE)
_LEAD_ARTICLE_RE = re.compile(
    r"^(?:(?:o|a|os|as)\s+)?(?:projeto|assunto|tema|quest[aã]o)\s+",
    re.IGNORECASE)
_LEAD_PREP_RE = re.compile(
    r"^(?:no|na|nos|nas|do|da|dos|das|de|sobre|para|pro|pra)\s+",
    re.IGNORECASE)
_TRAIL_FILLER_RE = re.compile(
    r"[\s,;:.!-]+(?:por\s+favor|pfv|agora|j[aá]|ok|okay|certo|beleza)$",
    re.IGNORECASE)
_ALL_WORDS = {"total", "tudo", "geral", "geralzão", "sempre"}
_NOT_A_NAME = {
    "e", "eh", "é", "esta", "está", "ta", "tá", "aqui", "agora", "por",
    "favor", "nao", "não", "sim", "obrigado", "obrigada", "valeu", "bom",
    "que", "eu", "me", "te", "se", "ele", "ela", "isso", "isto", "estou",
    "to", "tô", "vou", "quero", "pode", "deixa", "pare", "para", "cala",
    "fala", "fique", "fico", "gente", "todo", "mundo",
}


def parse_order(text: str, addresses: Iterable[str] = ()) -> dict:
    """Read the owner's spoken word. Returns a plain dict, never raises.

    ``{"triggered": bool, "subject": str, "slug": str, "all": bool}``.
    The word must open the utterance (optionally after the assistant's name
    or a politeness filler): "que silêncio bonito" is a comment, not an order,
    and any question mark cancels the trigger.
    """
    raw = " ".join(str(text or "").split()).strip()
    out = {"triggered": False, "subject": "", "slug": "", "all": False}
    if not raw or "?" in raw:
        return out
    names = [str(a).strip() for a in addresses if str(a or "").strip()]
    names += ["gino", "jarvis"]
    # Longer names first so "Gino" does not shadow a compound address.
    names = sorted({n for n in names if n}, key=len, reverse=True)
    address = "|".join(re.escape(n) for n in names)
    pattern = re.compile(
        rf"^(?:(?:{address})\s*[,:!.;-]?\s*)?sil[eê]ncio\b(.*)$",
        re.IGNORECASE)
    match = pattern.match(raw)
    if not match:
        return out
    rest = match.group(1).strip()
    rest = _LEAD_FILLER_RE.sub("", rest)
    rest = rest.strip(" \t,;:.-—–\"'«»")
    rest = _LEAD_ARTICLE_RE.sub("", rest)
    rest = _LEAD_PREP_RE.sub("", rest)
    rest = _TRAIL_FILLER_RE.sub("", rest)
    rest = rest.strip(" \t,;:.-—–\"'«»")
    folded = _fold(rest)
    first = folded.split(" ", 1)[0] if folded else ""
    if not rest or folded in _ALL_WORDS or first in _NOT_A_NAME:
        out.update({"triggered": True, "all": True})
        return out
    label = " ".join(rest.split())[:60]
    out.update({"triggered": True, "subject": label,
                "slug": slugify(label, "geral")})
    return out


# ── the gate ────────────────────────────────────────────────────────────────

class SilenceGate:
    """Persisted mute state + retained-speech queue for one subject/room."""

    def __init__(
        self,
        state_path: str | Path | None = None,
        queue_path: str | Path | None = None,
        max_retained: int = DEFAULT_MAX_RETAINED,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._queue_path = Path(queue_path) if queue_path else DEFAULT_QUEUE_PATH
        self._max_retained = max(1, int(max_retained or DEFAULT_MAX_RETAINED))
        self._log_fn = logger or (lambda _m: None)
        self._lock = threading.RLock()
        self._all = False
        self._subjects: list[dict] = []
        self._released: list[dict] = []
        self._entries: list[dict] = []
        self._dropped = 0
        self._load_state()
        self._load_queue()

    # ── state: persistence and queries ────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def queue_path(self) -> Path:
        return self._queue_path

    @property
    def active(self) -> bool:
        """True while anything at all is muted."""
        with self._lock:
            return bool(self._all or self._subjects)

    @property
    def muted_all(self) -> bool:
        with self._lock:
            return self._all

    def muted_subjects(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._subjects]

    def released_subjects(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._released]

    def snapshot(self) -> dict:
        with self._lock:
            return {"all": self._all,
                    "subjects": [dict(i) for i in self._subjects],
                    "released": [dict(i) for i in self._released],
                    "updated_at": self._updated_at}

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._updated_at = str(data.get("updated_at") or "")
        self._all = bool(data.get("all"))
        self._subjects = self._clean_subjects(data.get("subjects"))
        self._released = self._clean_subjects(data.get("released"))
        if not self._all:
            self._released = []

    @staticmethod
    def _clean_subjects(value: Any) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for item in (value or []):
            if isinstance(item, str):
                label, slug = item, slugify(item, "geral")
            elif isinstance(item, dict):
                label = str(item.get("label") or item.get("subject")
                            or item.get("slug") or "").strip()
                slug = str(item.get("slug") or "") or slugify(label, "geral")
            else:
                continue
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append({"slug": slug, "label": label or slug})
        return out

    def _save_state_locked(self) -> None:
        self._updated_at = _now_iso()
        payload = json.dumps({
            "all": bool(self._all),
            "subjects": [dict(i) for i in self._subjects],
            "released": [dict(i) for i in self._released],
            "updated_at": self._updated_at,
        }, ensure_ascii=False)
        try:
            _atomic_write(self._state_path, payload)
        except Exception as e:
            self._log(f"[Silêncio] não consegui salvar o estado: {e}")

    # ── orders ────────────────────────────────────────────────────────────

    def apply_order(self, subject: str = "") -> dict:
        """Toggle mute all / toggle one subject. Returns the action taken.

        ``{"action": "mute_all"|"release_all"|"mute_subject"|
        "release_subject", "subject": {"slug": str, "label": str}}``
        """
        label = " ".join(str(subject or "").split())[:60]
        slug = slugify(label, "geral") if label else ""
        with self._lock:
            if not slug:
                if self._all or self._subjects:
                    self._all = False
                    self._subjects = []
                    self._released = []
                    action = "release_all"
                else:
                    self._all = True
                    self._subjects = []
                    self._released = []
                    action = "mute_all"
                self._save_state_locked()
                return {"action": action, "subject": {}}

            item = {"slug": slug, "label": label}
            if self._all:
                if self._released_has(slug):
                    self._released = [i for i in self._released
                                      if i["slug"] != slug]
                    action = "mute_subject"
                else:
                    self._released.append(item)
                    action = "release_subject"
            elif self._subjects_has(slug):
                self._subjects = [i for i in self._subjects
                                  if i["slug"] != slug]
                action = "release_subject"
            else:
                self._subjects.append(item)
                action = "mute_subject"
            self._save_state_locked()
            return {"action": action, "subject": dict(item)}

    def _released_has(self, slug: str) -> bool:
        return any(i["slug"] == slug for i in self._released)

    def _subjects_has(self, slug: str) -> bool:
        return any(i["slug"] == slug for i in self._subjects)

    def set_muted(self, subjects: Iterable[str] = (), *, all_muted: bool = False) -> dict:
        """Direct state setter (boot recovery/tests). Returns a snapshot."""
        items = self._clean_subjects(list(subjects or []))
        with self._lock:
            self._all = bool(all_muted)
            self._subjects = [] if all_muted else items
            self._released = []
            self._save_state_locked()
            return self.snapshot()

    def release(self, slug: str | None = None) -> dict:
        """Unmute one subject (or everything) and KEEP the queue for taking."""
        with self._lock:
            if slug:
                self._subjects = [i for i in self._subjects
                                  if i["slug"] != slug]
                self._released = [i for i in self._released
                                  if i["slug"] != slug]
                if not self._subjects:
                    self._all = False
            else:
                self._all = False
                self._subjects = []
                self._released = []
            self._save_state_locked()
            return self.snapshot()

    def is_muted(self, subject: str = "", text: str = "") -> bool:
        """Does this line belong to a muted subject right now?

        ``subject`` is the resolved subject/project slug or label when the
        caller knows it; ``text`` is checked against the muted labels so a line
        that names the cut subject is retained even when the active project is
        stale. Never raises.
        """
        with self._lock:
            if self._all:
                for item in self._released:
                    if _matches(item, subject, text):
                        return False
                return True
            for item in self._subjects:
                if _matches(item, subject, text):
                    return True
            return False

    # ── retention ─────────────────────────────────────────────────────────

    @property
    def retained_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def _load_queue(self) -> None:
        try:
            data = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, list):
            data = {"entries": data}
        if not isinstance(data, dict):
            data = {}
        self._dropped = max(0, int(data.get("dropped") or 0))
        entries = [i for i in (data.get("entries") or []) if isinstance(i, dict)]
        self._entries = entries[-self._max_retained:]

    def _save_queue_locked(self) -> None:
        payload = json.dumps({
            "version": 1,
            "dropped": int(self._dropped),
            "entries": list(self._entries),
        }, ensure_ascii=False)
        try:
            _atomic_write(self._queue_path, payload)
        except Exception as e:
            self._log(f"[Silêncio] não consegui salvar a fila: {e}")

    def retain(self, text: str, *, subject: str = "", voice_id: str = "",
               label: str = "", chan: str = "", origin: str = "",
               log_text: str = "", destination: str = "room",
               turn: int | None = None) -> dict:
        """Keep the speech that silence prevented from sounding."""
        text = str(text or "").strip()
        subject_label = " ".join(str(subject or "").split())[:60]
        entry = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "subject": subject_label,
            "slug": slugify(subject_label, "geral") if subject_label else "",
            "voice_id": str(voice_id or ""),
            "label": str(label or ""),
            "chan": str(chan or ""),
            "origin": str(origin or ""),
            "log_text": str(log_text or ""),
            "destination": str(destination or "room"),
            "at": _now_iso(),
            "turn": int(turn) if isinstance(turn, int) else None,
        }
        with self._lock:
            self._entries.append(entry)
            while len(self._entries) > self._max_retained:
                self._entries.pop(0)
                self._dropped += 1
            self._save_queue_locked()
            return dict(entry)

    def take(self, slug: str | None = None) -> dict:
        """Remove and return retained lines (all, or only this subject slug)."""
        with self._lock:
            if slug:
                picked = [i for i in self._entries if i.get("slug") == slug]
                self._entries = [i for i in self._entries
                                 if i.get("slug") != slug]
                dropped = 0
            else:
                picked = list(self._entries)
                self._entries = []
                dropped = self._dropped
                self._dropped = 0
            self._save_queue_locked()
            return {"entries": [dict(i) for i in picked],
                    "dropped": int(dropped)}

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._entries]

    def _log(self, message: str) -> None:
        try:
            self._log_fn(message)
        except Exception:
            pass


# ── matching / summary ──────────────────────────────────────────────────────

def _matches(item: dict, subject: str = "", text: str = "") -> bool:
    label = str(item.get("label") or "")
    slug = str(item.get("slug") or "")
    if not slug:
        return False
    if subject:
        subj_slug = slugify(str(subject), "")
        if subj_slug and (subj_slug == slug
                          or set(slug.split("-")) <= set(subj_slug.split("-"))):
            return True
        if label and _fold(subject) == _fold(label):
            return True
    if text:
        words = _tokens(text)
        wanted = {t for t in slug.split("-") if t}
        if wanted and len(max(wanted, key=len)) >= 2 and wanted <= words:
            return True
    return False


def _first_sentence(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    match = re.match(r"(.+?[.!?…])(?:\s|$)", raw)
    snippet = match.group(1) if match else raw
    if len(snippet) > MAX_SNIPPET:
        snippet = snippet[:MAX_SNIPPET].rstrip() + "…"
    return snippet


def summarize(entries: list[dict]) -> list[dict]:
    """Group retained lines by subject: count + latest sentence (no dump)."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for entry in entries or []:
        slug = str(entry.get("slug") or "")
        key = slug or "-"
        group = groups.get(key)
        if group is None:
            group = {
                "slug": slug,
                "subject": str(entry.get("subject") or entry.get("label")
                               or "sem assunto"),
                "count": 0,
                "latest": "",
            }
            groups[key] = group
            order.append(key)
        group["count"] += 1
        group["latest"] = str(entry.get("text") or group["latest"])
    return [groups[k] for k in order]


def summary_lines(entries: list[dict], dropped: int = 0,
                  max_subjects: int = MAX_SUMMARY_SUBJECTS) -> list[str]:
    """Short, one-line-per-subject summary for the release."""
    groups = summarize(entries)
    lines: list[str] = []
    for group in groups[:max(1, int(max_subjects))]:
        count = int(group.get("count") or 0)
        plural = "s" if count > 1 else ""
        line = (f"Sobre {group['subject']}: {count} fala{plural} guardada"
                f"{plural}.")
        latest = _first_sentence(group.get("latest") or "")
        if latest:
            line += f" A última: {latest}"
        lines.append(line)
    extra = len(groups) - max(1, int(max_subjects))
    if extra > 0:
        lines.append(f"Mais {extra} assunto{'s' if extra > 1 else ''} "
                     "também teve fala guardada.")
    if int(dropped or 0) > 0:
        lines.append(f"Descartei {int(dropped)} fala"
                     f"{'s' if int(dropped) > 1 else ''} antiga"
                     f"{'s' if int(dropped) > 1 else ''} por limite de espaço.")
    return lines
