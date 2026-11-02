"""The librarian: keeps the conversation memory compact and indexed.

WHAT IT DOES
    `core.chat_memory` already stores every turn in hour files and can rebuild
    the hot index and the day index deterministically. This module runs that
    maintenance in the background and, when a summarizer is available, turns
    the recent turns into a short summary, open threads and topic files.

WHY BACKGROUND
    Summarising ten turns takes a few seconds on Qoder (the local text model
    was retired). Doing it inside a conversation turn would add exactly the
    latency this project is trying to remove. The librarian is single-flight: a
    second trigger while one is running is dropped, not queued.

PROMPT BUDGET
    The model sees only the last few hours of turns, truncated. It must answer
    JSON; if it does not, the raw text becomes a note and no topic is invented.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from typing import Callable, Optional

DEFAULT_EVERY_TURNS = 10
DEFAULT_EVERY_SECONDS = 15 * 60.0
MAX_RECENT_TURNS = 40
MAX_PROMPT_CHARS = 7000
MAX_TOPICS = 3
MAX_SUMMARY = 600

_SUMMARY_PROMPT = """Você é o bibliotecário da memória de uma conversa entre o usuário e o assistente.
Leia as falas recentes e produza um resumo curto e útil para retomar o fio depois.
Responda SOMENTE com um objeto JSON, sem texto fora dele:
{"summary":"2 a 3 frases sobre o que aconteceu e o que ficou decidido",
 "open_threads":["assunto que ficou em aberto"],
 "topics":[{"name":"nome curto do assunto","summary":"1 a 2 frases sobre esse assunto"}]}
Regras:
- Não invente nada que não esteja nas falas.
- No máximo 3 tópicos; se não houver assunto claro, use uma lista vazia.
- open_threads só para o que ficou pendente de verdade.
- Escreva em português do Brasil.

FALAS RECENTES:
"""


def _parse_json(text: str) -> Optional[dict]:
    body = str(text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(body[start:end + 1])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


class MemoryLibrarian:
    """Background compaction + optional summary of the conversation memory."""

    def __init__(
        self,
        memory,
        summarize: Optional[Callable[[str], str]] = None,
        idle: Optional[Callable[[], bool]] = None,
        every_turns: int = DEFAULT_EVERY_TURNS,
        every_seconds: float = DEFAULT_EVERY_SECONDS,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.memory = memory
        self._summarize = summarize
        self._idle = idle or (lambda: True)
        self._every_turns = max(1, int(every_turns))
        self._every_seconds = max(30.0, float(every_seconds))
        self._log = logger or (lambda _m: None)

        self._lock = threading.RLock()
        self._compact_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._turns = 0
        self._last_compact = 0.0
        self._day = None
        self._summary_ok = 0
        self._summary_fail = 0

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.memory is None:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._day = datetime.now().date()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="memory-librarian")
            self._thread.start()
        # Build the indexes that may have been missed while the app was off.
        self.schedule()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(30.0):
                break
            with self._lock:
                due = (self._turns > 0 and
                       time.monotonic() - self._last_compact >= self._every_seconds)
            if due:
                self.schedule()

    # ── triggers ────────────────────────────────────────────────────────────

    def tick(self, count: int = 1) -> None:
        """Called after a completed exchange."""
        with self._lock:
            self._turns += max(1, int(count))
            due = self._turns >= self._every_turns
        if due:
            self.schedule()

    def schedule(self) -> bool:
        """Run one compaction in the background; drop if one is already running."""
        if not self._compact_lock.acquire(blocking=False):
            return False

        def _run() -> None:
            try:
                self.compact_now()
            finally:
                self._compact_lock.release()

        threading.Thread(target=_run, daemon=True, name="memory-librarian-run").start()
        return True

    # ── one compaction ──────────────────────────────────────────────────────

    def compact_now(self, now=None) -> dict:
        result = {"compacted": False, "summary": False, "previous_day": ""}
        current = now if isinstance(now, datetime) else datetime.now()
        try:
            result["compacted"] = bool(self.memory.compact_hour(current))
        except Exception as e:
            self._log(f"[Librarian] compact failed: {e}")

        day = current.date()
        with self._lock:
            previous = self._day
            self._day = day
        if previous is not None and day != previous:
            try:
                self.memory.build_day_index(previous)
                result["previous_day"] = f"{previous:%Y-%m-%d}"
                self._log(f"[Librarian] índice do dia anterior: {result['previous_day']}")
            except Exception as e:
                self._log(f"[Librarian] previous day index failed: {e}")

        with self._lock:
            turns = self._turns
            self._turns = 0
        if turns and self._summarize is not None:
            try:
                idle = bool(self._idle())
            except Exception:
                idle = True
            if idle:
                recent = []
                try:
                    recent = self.memory.recent_turns(hours=6, now=current)
                except Exception as e:
                    self._log(f"[Librarian] recent turns failed: {e}")
                if len(recent) >= 2:
                    prompt = self._build_prompt(recent)
                    try:
                        text = self._summarize(prompt) or ""
                    except Exception as e:
                        text = ""
                        self._summary_fail += 1
                        self._log(f"[Librarian] summarizer failed: {e}")
                    if text.strip():
                        try:
                            result["summary"] = self._apply(text, current)
                            self._summary_ok += 1
                        except Exception as e:
                            self._summary_fail += 1
                            self._log(f"[Librarian] apply summary failed: {e}")

        with self._lock:
            self._last_compact = time.monotonic()
        if result["compacted"] or result["summary"]:
            self._log(f"[Librarian] compactado={result['compacted']} "
                      f"resumo={result['summary']} dia={day:%Y-%m-%d}")
        return result

    @staticmethod
    def _build_prompt(recent: list[dict]) -> str:
        lines = []
        for turn in recent[-MAX_RECENT_TURNS:]:
            at = str(turn.get("at") or "")
            role = str(turn.get("role") or "")
            text = " ".join(str(turn.get("text") or "").split())
            if not text:
                continue
            lines.append(f"- {at[11:16] if len(at) >= 16 else at} {role}: {text[:300]}")
        body = "\n".join(lines)
        if len(body) > MAX_PROMPT_CHARS:
            body = body[-MAX_PROMPT_CHARS:]
        return _SUMMARY_PROMPT + body

    def _apply(self, text: str, now: datetime) -> bool:
        data = _parse_json(text)
        if not data:
            summary = " ".join(str(text).split())[:MAX_SUMMARY]
            if summary:
                self.memory.note(f"resumo: {summary}", kind="resumo")
                return True
            return False

        summary = " ".join(str(data.get("summary") or "").split())[:MAX_SUMMARY]
        if summary:
            self.memory.note(f"resumo: {summary}", kind="resumo")

        threads = data.get("open_threads") or []
        if isinstance(threads, str):
            threads = [threads]
        for thread in threads[:5]:
            clean = " ".join(str(thread).split())[:120]
            if clean:
                self.memory.note(clean, kind="aberta")

        topics = data.get("topics") or []
        if isinstance(topics, dict):
            topics = [topics]
        written = 0
        for i, topic in enumerate(topics[:MAX_TOPICS]):
            if not isinstance(topic, dict):
                continue
            name = " ".join(str(topic.get("name") or "").split())[:60]
            body = " ".join(str(topic.get("summary") or "").split())[:500]
            if not name:
                continue
            content = (f"# {name}\n\n{body or summary or 'Sem resumo.'}\n\n"
                       f"- Atualizado em {now:%d/%m/%Y %H:%M}\n")
            slug = self.memory.write_topic(name, content, set_current=(i == 0))
            if slug:
                written += 1
        return bool(summary or threads or written)

    def status(self) -> dict:
        with self._lock:
            return {
                "turns_pending": self._turns,
                "last_compact": self._last_compact,
                "day": f"{self._day:%Y-%m-%d}" if self._day else "",
                "summary_ok": self._summary_ok,
                "summary_fail": self._summary_fail,
                "running": self._compact_lock.locked(),
            }
