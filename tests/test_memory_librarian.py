"""Tests for the background memory librarian (no model, no timers)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from core.chat_memory import ChatMemory
from core.memory_librarian import MemoryLibrarian


class MemoryLibrarianTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memory-librarian-")
        # Data fixa: com datetime.now() real, _fill() (now - 10 min) caía no
        # dia anterior quando a suíte rodava entre 00:00 e 00:10, e não havia
        # arquivo de hora do dia "de hoje" para indexar. O relógio do módulo
        # de memória também é congelado (note/hot usam _now).
        self.now = datetime(2026, 10, 31, 14, 3, 0)
        patcher = mock.patch("core.chat_memory._now",
                             side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.memory = ChatMemory(base_dir=self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, at=None):
        at = at or (self.now - timedelta(minutes=10))
        self.memory.append_turn("user", "Coloca uma música para tocar.", at=at)
        self.memory.append_turn("assistant", "Qual estilo você quer?", at=at)
        self.memory.append_turn("user", "Jazz, por favor.", at=at)
        self.memory.append_turn("assistant", "Tocando jazz.", at=at)

    def test_compaction_writes_index_topics_and_state(self):
        self._fill()
        prompts = []

        def summarize(prompt):
            prompts.append(prompt)
            return json.dumps({
                "summary": "O usuário pediu jazz e a música começou a tocar.",
                "open_threads": ["decidir a próxima música"],
                "topics": [{"name": "música", "summary": "Jazz escolhido e tocando."}],
            }, ensure_ascii=False)

        lib = MemoryLibrarian(self.memory, summarize=summarize,
                              every_turns=99, every_seconds=99999,
                              idle=lambda: True)
        lib.tick(5)
        result = lib.compact_now(now=self.now)
        self.assertTrue(result["compacted"])
        self.assertTrue(result["summary"])
        self.assertTrue(prompts and "FALAS RECENTES" in prompts[0])
        hot = self.memory.read("hot.md")
        self.assertIn("Jazz", hot)
        day = self.memory.read(f"days/{self.now:%Y-%m-%d}/index.md")
        self.assertIn(f"Índice do dia {self.now:%Y-%m-%d}", day)
        topic = self.memory.read("topics/musica.md")
        self.assertIn("# música", topic)
        state = self.memory.read("state.md")
        self.assertIn("resumo:", state)
        self.assertIn("decidir a próxima música", state)

    def test_day_rollover_builds_previous_day_index(self):
        self.memory.append_turn("user", "Conversa de ontem.",
                                at=datetime(2026, 10, 30, 23, 50))
        lib = MemoryLibrarian(self.memory, summarize=None)
        lib._day = date(2026, 10, 30)
        result = lib.compact_now(now=datetime(2026, 10, 31, 0, 10))
        self.assertEqual(result["previous_day"], "2026-10-30")
        self.assertIn("Índice do dia 2026-10-30",
                      self.memory.read("days/2026-10-30/index.md"))

    def test_invalid_summary_falls_back_to_note(self):
        self._fill()
        lib = MemoryLibrarian(self.memory, summarize=lambda _p: "resumo simples sem json",
                              every_turns=99, every_seconds=99999)
        lib.tick(5)
        result = lib.compact_now(now=self.now)
        self.assertTrue(result["summary"])
        self.assertIn("resumo simples sem json", self.memory.read("state.md"))

    def test_idle_false_skips_summary_but_compacts(self):
        self._fill()
        calls = []
        lib = MemoryLibrarian(self.memory, summarize=lambda p: calls.append(p) or "",
                              every_turns=99, every_seconds=99999,
                              idle=lambda: False)
        lib.tick(5)
        result = lib.compact_now(now=self.now)
        self.assertTrue(result["compacted"])
        self.assertFalse(result["summary"])
        self.assertEqual(calls, [])
        self.assertTrue(self.memory.read(f"days/{self.now:%Y-%m-%d}/index.md"))


if __name__ == "__main__":
    unittest.main()
