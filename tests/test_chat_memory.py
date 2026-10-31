"""Tests for the continuous conversation memory (core/chat_memory.py).

Run from the repo root:
    .venv/bin/python -m unittest tests.test_chat_memory -v

Everything runs inside a TemporaryDirectory; the real memory/chat is never
touched. Time is patched at the module's ``_now`` hook so gaps are simulated
without sleeping.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chat_memory import ChatMemory  # noqa: E402


class ChatMemoryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="chat-memory-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "chat"
        self.now = datetime(2026, 10, 31, 14, 3)
        patcher = mock.patch("core.chat_memory._now",
                             side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mem = ChatMemory(self.base)

    def read(self, *parts) -> str:
        return self.base.joinpath(*parts).read_text(encoding="utf-8")

    # ── layout ──────────────────────────────────────────────────────────────
    def test_fresh_install_creates_layout(self):
        self.assertTrue(self.base.is_dir())
        for folder in ("hours", "days", "topics"):
            self.assertTrue((self.base / folder).is_dir(), folder)
        self.assertTrue((self.base / "state.md").is_file())
        self.assertTrue((self.base / "hot.md").is_file())
        # Seeded state has no last_seen and no turns: nothing to inject.
        self.assertEqual(self.mem.prompt_block(), "")
        gap = self.mem.check_gap()
        self.assertFalse(gap["long"])
        self.assertEqual(gap["last_seen"], "")
        self.assertEqual(gap["prompt"], "")

    # ── turns, compaction, day index ────────────────────────────────────────
    def test_append_and_compact_move_turns_to_hour_file(self):
        self.mem.append_turn("user", "Olá, vamos falar de música", at=self.now)
        self.mem.append_turn("assistant", "Claro, qual banda?",
                             at=self.now + timedelta(minutes=1))

        hour = self.base / "hours" / "2026-10-31" / "14.md"
        self.assertTrue(hour.is_file())
        body = hour.read_text(encoding="utf-8")
        self.assertIn("14:03 Você: Olá, vamos falar de música", body)
        self.assertIn("14:04 Assistente: Claro, qual banda?", body)

        # hot index picks the turns up immediately.
        self.assertIn("Olá, vamos falar de música", self.read("hot.md"))

        result = self.mem.compact_hour(now=self.now + timedelta(minutes=7))
        self.assertEqual(result["window_hours"], 6)
        self.assertGreaterEqual(result["hot_lines"], 1)
        self.assertIn("2026-10-31", result["days"])

        # Hours are still the source of truth after compaction.
        self.assertIn("14:03 Você", hour.read_text(encoding="utf-8"))
        self.assertIn("Olá, vamos falar de música", self.read("hot.md"))
        state = self.read("state.md")
        self.assertIn("last_seen: 2026-10-31 14:04", state)

    def test_day_index_links_hours(self):
        self.mem.append_turn("user", "primeira hora", at=self.now)
        self.mem.append_turn("user", "segunda hora",
                             at=self.now.replace(hour=15, minute=10))
        index = self.mem.build_day_index("2026-10-31")
        self.assertIn("hours/2026-10-31/14.md", index)
        self.assertIn("hours/2026-10-31/15.md", index)
        self.assertIn("segunda hora", index)
        on_disk = self.read("days", "2026-10-31", "index.md")
        self.assertEqual(index, on_disk)
        # date objects are accepted too
        self.assertIn("2026-10-31", self.mem.build_day_index(date(2026, 10, 31)))

    # ── topics ──────────────────────────────────────────────────────────────
    def test_topic_write_read_and_hot_link(self):
        slug = self.mem.write_topic("Música Favorita",
                                    "# Música favorita\n\n- jazz\n- bossa\n")
        self.assertEqual(slug, "musica-favorita")
        self.assertIn("bossa", self.mem.read_topic(slug))
        self.assertEqual(self.mem.read_topic("nao-existe"), "")
        self.assertIn("current_topic: musica-favorita", self.read("state.md"))

        self.mem.compact_hour(now=self.now)
        self.assertIn("[[topics/musica-favorita]]", self.read("hot.md"))
        block = self.mem.prompt_block()
        self.assertIn("[[topics/musica-favorita]]", block)
        self.assertIn("bossa", block)

        found = self.mem.search("bossa")
        self.assertTrue(any("musica-favorita" in r["source"] for r in found))
        self.assertEqual(self.mem.search("   "), [])

    # ── the pause ───────────────────────────────────────────────────────────
    def test_three_day_gap_returns_pausa_longa_without_stale_hot(self):
        start = self.now
        self.mem.append_turn("user", "meu projeto secreto é o foguete azul",
                             at=start)
        self.mem.append_turn("assistant", "entendi o foguete azul",
                             at=start + timedelta(minutes=1))
        self.mem.compact_hour(now=start + timedelta(minutes=2))
        self.assertIn("foguete azul", self.read("hot.md"))

        # A two-hour silence is not a long pause.
        self.now = start + timedelta(hours=2)
        self.assertFalse(self.mem.check_gap()["long"])

        # Three days and two hours later, it is.
        self.now = start + timedelta(days=3, hours=2)
        gap = self.mem.check_gap()
        self.assertTrue(gap["long"])
        self.assertGreaterEqual(gap["gap_hours"], 72)
        self.assertIn("PAUSA LONGA", gap["prompt"])
        self.assertIn("3 dias", gap["prompt"])

        # Rounding must not produce "2 dias e 24 h".
        self.now = start + timedelta(days=3) - timedelta(minutes=1)
        rounded = self.mem.check_gap()["prompt"]
        self.assertIn("3 dias", rounded)
        self.assertNotIn("24 h", rounded)

        self.now = start + timedelta(days=3, hours=2)

        block = self.mem.prompt_block()
        self.assertIn("PAUSA LONGA", block)
        self.assertIn("3 dias", block)
        self.assertIn("Índice do último dia", block)
        self.assertNotIn("Índice quente", block)
        # The hot file was refreshed and no longer serves stale lines; the day
        # index is the only allowed source of old detail.
        self.assertNotIn("foguete azul", self.read("hot.md"))

    # ── bounds ──────────────────────────────────────────────────────────────
    def test_prompt_block_and_hot_are_bounded(self):
        for i in range(80):
            self.mem.append_turn(
                "user", f"linha {i} " + ("x" * 300),
                at=self.now + timedelta(seconds=i),
            )
        block = self.mem.prompt_block(max_chars=200)
        self.assertLessEqual(len(block), 200)
        default = self.mem.prompt_block()
        self.assertLessEqual(len(default), 2000)
        self.assertTrue(default)

        hot = self.read("hot.md")
        self.assertLessEqual(len(hot), 4000)
        self.assertLessEqual(len(hot.splitlines()), 40)
        self.assertEqual(self.mem.prompt_block(max_chars=0), "")

    # ── robustness ──────────────────────────────────────────────────────────
    def test_malformed_json_and_markdown_do_not_crash(self):
        hour_dir = self.base / "hours" / "2026-10-31"
        hour_dir.mkdir(parents=True, exist_ok=True)
        (self.base / "state.md").write_text(
            "{not json at all}\n- last_seen: ???\n- current_topic: [[\n",
            encoding="utf-8",
        )
        (self.base / "hot.md").write_text(
            "\x00[[[ broken\n" * 50, encoding="utf-8")
        (hour_dir / "14.md").write_text(
            "lixo sem formato\n- 99:99 Ninguém: nada\n- 14:10 Você: ok\n",
            encoding="utf-8",
        )
        (self.base / "topics" / "quebrado.md").write_bytes(
            b"\xff\xfe not utf8 \x00")

        gap = self.mem.check_gap()
        self.assertFalse(gap["long"])
        block = self.mem.prompt_block()
        self.assertIsInstance(block, str)
        self.assertLessEqual(len(block), 2000)
        self.assertIsInstance(self.mem.build_day_index("2026-10-31"), str)
        self.mem.compact_hour(now=self.now)
        self.mem.append_turn("user", "depois do lixo", at=self.now)
        self.assertIn("depois do lixo",
                      self.read("hours", "2026-10-31", "14.md"))
        self.assertIsInstance(self.mem.read_topic("quebrado"), str)
        self.assertTrue(self.mem.search("ok"))
        self.mem.note("thread sem dono", kind="thread")
        self.assertIn("thread sem dono", self.read("state.md"))


if __name__ == "__main__":
    unittest.main()
