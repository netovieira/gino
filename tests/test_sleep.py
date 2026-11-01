"""Tests for the nightly sleep compaction (core/sleep.py).

Everything runs in a TemporaryDirectory: SLEEP_DIR, STATE_PATH and CHAT_DIR
are monkeypatched before any module function is called, Qoder is always
replaced by a fake, and the memory index query is mocked so no real graph is
read or written.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import sleep  # noqa: E402
from core.chat_memory import ChatMemory  # noqa: E402


class SleepTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="sleep-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.sleep_dir = root / "chat" / "sleep"
        self.state_path = self.sleep_dir / "state.json"
        self.chat_dir = root / "chat"
        self.now = datetime(2026, 10, 31, 23, 35)

        for name, value in (
            ("SLEEP_DIR", self.sleep_dir),
            ("STATE_PATH", self.state_path),
            ("CHAT_DIR", self.chat_dir),
        ):
            patcher = mock.patch.object(sleep, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        now_patcher = mock.patch("core.sleep._now",
                                 side_effect=lambda: self.now)
        now_patcher.start()
        self.addCleanup(now_patcher.stop)

        keys_patcher = mock.patch.object(sleep, "load_api_keys", return_value={})
        keys_patcher.start()
        self.addCleanup(keys_patcher.stop)

        query_patcher = mock.patch.object(sleep.memory_index, "query",
                                          return_value=[])
        self.query = query_patcher.start()
        self.addCleanup(query_patcher.stop)

    # ── helpers ─────────────────────────────────────────────────────────────
    def seed_day(self, day: str, turns: int = 3, text: str = "conversa"):
        mem = ChatMemory(self.chat_dir)
        start = datetime.fromisoformat(f"{day}T09:00:00")
        for i in range(turns):
            role = "user" if i % 2 == 0 else "assistant"
            mem.append_turn(role, f"{text} {i}", at=start + timedelta(minutes=i))
        mem.build_day_index(day)
        return mem

    def write_state(self, last_seen="", topic="", threads=None, notes=None):
        lines = [
            "# Estado da conversa",
            "",
            f"- last_seen: {last_seen}",
            f"- last_user_at: {last_seen}",
            f"- current_topic: {topic}",
            "- open_threads: " + " | ".join(threads or []),
            "",
        ]
        if notes:
            lines.append("## Notas")
            for at, kind, text in notes:
                lines.append(f"- {at} [{kind}] {text}")
            lines.append("")
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        (self.chat_dir / "state.md").write_text("\n".join(lines),
                                                encoding="utf-8")

    def read_sleep(self, day: str) -> str:
        return (self.sleep_dir / f"{day}.md").read_text(encoding="utf-8")

    # ── settings / state ────────────────────────────────────────────────────
    def test_settings_defaults(self):
        cfg = sleep.settings()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["time"], "23:30")
        self.assertEqual(cfg["model"], "Qwen3.8-Flash")
        self.assertIsInstance(cfg["timezone"], str)
        self.assertTrue(cfg["timezone"])

    def test_settings_from_root_keys_and_safe_invalid_values(self):
        with mock.patch.object(sleep, "load_api_keys", return_value={
            "sleep_enabled": "false",
            "sleep_time": "7:05",
            "sleep_model": "Outro-Modelo",
            "sleep_timezone": "UTC",
        }):
            cfg = sleep.settings()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["time"], "07:05")
        self.assertEqual(cfg["model"], "Outro-Modelo")
        self.assertEqual(cfg["timezone"], "UTC")

        with mock.patch.object(sleep, "load_api_keys", return_value={
            "sleep_enabled": "talvez",
            "sleep_time": "99:99",
            "sleep_model": "",
        }):
            cfg = sleep.settings()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["time"], "23:30")
        self.assertEqual(cfg["model"], "Qwen3.8-Flash")

    def test_settings_survives_broken_config(self):
        with mock.patch.object(sleep, "load_api_keys",
                               side_effect=RuntimeError("boom")):
            cfg = sleep.settings()
        self.assertEqual(cfg["time"], "23:30")
        self.assertTrue(cfg["enabled"])

    def test_last_sleep_valid_missing_and_malformed(self):
        self.assertIsNone(sleep.last_sleep())
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"last_sleep": "2026-10-29"}',
                                   encoding="utf-8")
        self.assertEqual(sleep.last_sleep(), "2026-10-29")
        self.state_path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(sleep.last_sleep())
        self.state_path.write_text('{"last_sleep": "ontem"}', encoding="utf-8")
        self.assertIsNone(sleep.last_sleep())

    # ── missing nights ──────────────────────────────────────────────────────
    def test_missing_nights_after_last_sleep_until_yesterday(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"last_sleep": "2026-10-27"}',
                                   encoding="utf-8")
        self.seed_day("2026-10-28")
        self.assertEqual(
            sleep.missing_nights(self.now),
            ["2026-10-28", "2026-10-29", "2026-10-30"],
        )

    def test_missing_nights_bedtime_does_not_include_today(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"last_sleep": "2026-10-29"}',
                                   encoding="utf-8")
        morning = datetime(2026, 10, 31, 6, 0)
        late = datetime(2026, 10, 31, 23, 59)
        self.assertEqual(sleep.missing_nights(morning), ["2026-10-30"])
        self.assertEqual(sleep.missing_nights(late), ["2026-10-30"])

    def test_missing_nights_without_state_starts_at_earliest_day(self):
        self.seed_day("2026-10-27")
        self.seed_day("2026-10-29")
        self.assertEqual(
            sleep.missing_nights(self.now),
            ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"],
        )

    def test_missing_nights_without_state_or_data_is_empty(self):
        self.assertEqual(sleep.missing_nights(self.now), [])

    def test_missing_nights_has_no_arbitrary_day_cap(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"last_sleep": "2026-09-01"}',
                                   encoding="utf-8")
        nights = sleep.missing_nights(self.now)
        self.assertGreater(len(nights), 50)
        self.assertEqual(nights[0], "2026-09-02")
        self.assertEqual(nights[-1], "2026-10-30")

    def test_missing_nights_future_state_is_empty(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"last_sleep": "2026-11-05"}',
                                   encoding="utf-8")
        self.assertEqual(sleep.missing_nights(self.now), [])

    # ── build_day ───────────────────────────────────────────────────────────
    def test_build_day_contains_required_sections_and_no_transcript(self):
        secret = "SEGREDO_QUE_NAO_PODE_ENTRAR_NA_TRANSCRICAO"
        self.seed_day("2026-10-30", turns=3, text=secret)
        self.write_state(
            last_seen="2026-10-30 09:02",
            topic="projeto-x",
            threads=["terminar o relatório", "revisar o WhatsApp"],
            notes=[
                ("2026-10-30 09:05", "preference", "usar o modelo X"),
                ("2026-10-30 09:06", "aberta", "terminar o relatório"),
                ("2026-10-29 21:00", "note", "nota de outro dia"),
            ],
        )
        topics_dir = self.chat_dir / "topics"
        topics_dir.mkdir(parents=True, exist_ok=True)
        (topics_dir / "projeto-x.md").write_text(
            "# Projeto X\n\nAtualizado em 30/10/2026\n", encoding="utf-8"
        )
        self.query.return_value = [
            {"kind": "user", "text": secret, "at": "2026-10-30T09:00:00",
             "persona": "ana", "project": "jarvis", "subject": "sono"},
            {"kind": "assistant", "text": "resposta", "at": "2026-10-30T09:01:00",
             "persona": "ana", "project": "jarvis", "subject": "sono"},
            {"kind": "note", "text": "fora do dia", "at": "2026-10-29T09:00:00",
             "persona": "bia", "project": "outro", "subject": "outro"},
        ]

        text = sleep.build_day("2026-10-30")

        self.assertIn("2026-10-30", text)
        self.assertIn("Falas: 3 em 1 hora", text)
        self.assertIn("09h", text)
        self.assertIn("../days/2026-10-30/index.md", text)
        self.assertIn("projeto-x", text)
        self.assertIn("Projeto X", text)
        self.assertIn("terminar o relatório", text)
        self.assertIn("revisar o WhatsApp", text)
        self.assertIn("usar o modelo X", text)
        self.assertIn("ana", text)
        self.assertIn("jarvis", text)
        self.assertNotIn("bia", text)
        self.assertNotIn("nota de outro dia", text)
        self.assertNotIn(secret, text)
        self.assertLessEqual(len(text), sleep.MAX_BUILD_CHARS)

    def test_build_day_is_capped_and_survives_malformed_state(self):
        self.seed_day("2026-10-30", turns=2)
        self.write_state(last_seen="2026-10-30 09:00")
        notes = [("2026-10-30 09:00", "preference", "x" * 300)
                 for _ in range(40)]
        self.write_state(last_seen="2026-10-30 09:00", notes=notes)
        text = sleep.build_day("2026-10-30")
        self.assertLessEqual(len(text), 6000)

        (self.chat_dir / "state.md").write_text("\x00broken", encoding="utf-8")
        self.assertIsInstance(sleep.build_day("2026-10-30"), str)

    def test_build_day_invalid_date_returns_empty(self):
        self.assertEqual(sleep.build_day("não é dia"), "")

    # ── summarize ───────────────────────────────────────────────────────────
    def test_summarize_uses_injected_ask_with_portuguese_prompt(self):
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return "O dia tratou do projeto X. Ficou pendente o relatório."

        result = sleep.summarize("# base", "2026-10-30", ask=ask)
        self.assertEqual(result, "O dia tratou do projeto X. Ficou pendente "
                                 "o relatório.")
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("2026-10-30", prompt)
        self.assertIn("português", prompt)
        self.assertIn("1200", prompt)
        self.assertIn("fios abertos", prompt)
        self.assertIn("# base", prompt)

    def test_summarize_empty_or_error_falls_back_to_markdown(self):
        base = "# base determinística"
        self.assertEqual(sleep.summarize(base, "2026-10-30",
                                         ask=lambda _p: ""), base)
        self.assertEqual(sleep.summarize(base, "2026-10-30",
                                         ask=lambda _p: None), base)

        def broken(_prompt):
            raise RuntimeError("qoder fora")

        self.assertEqual(sleep.summarize(base, "2026-10-30", ask=broken), base)

    def test_summarize_qoder_uses_configured_model_and_falls_back(self):
        with mock.patch.object(sleep, "load_api_keys",
                               return_value={"sleep_model": "Qwen3.8-Flash"}):
            with mock.patch.object(sleep.qoder, "ask",
                                   return_value="Resumo do Qoder.") as ask:
                result = sleep.summarize("# base", "2026-10-30")
        self.assertEqual(result, "Resumo do Qoder.")
        self.assertEqual(ask.call_args.kwargs["model"], "Qwen3.8-Flash")
        self.assertIn("system", ask.call_args.kwargs)

        with mock.patch.object(sleep.qoder, "ask",
                               side_effect=RuntimeError("sem Qoder")):
            self.assertEqual(sleep.summarize("# base", "2026-10-30"), "# base")

    def test_summarize_qoder_answer_is_bounded(self):
        with mock.patch.object(sleep.qoder, "ask",
                               return_value="a" * 5000):
            result = sleep.summarize("# base", "2026-10-30")
        self.assertLessEqual(len(result), sleep.MAX_SUMMARY_CHARS)

    # ── write_day ───────────────────────────────────────────────────────────
    def test_write_day_frontmatter_summary_and_state(self):
        path = sleep.write_day(
            "2026-10-30", "Resumo narrativo do dia.", deterministic="# base"
        )
        self.assertEqual(path, self.sleep_dir / "2026-10-30.md")
        text = self.read_sleep("2026-10-30")
        self.assertTrue(text.startswith("---"))
        self.assertIn("date: 2026-10-30", text)
        self.assertIn("generated_at: 2026-10-31T23:35:00", text)
        self.assertIn("model: Qwen3.8-Flash", text)
        self.assertIn("source: qoder", text)
        self.assertIn("## Resumo", text)
        self.assertIn("Resumo narrativo do dia.", text)
        self.assertIn("## Base determinística", text)
        self.assertIn("# base", text)
        self.assertEqual(sleep.last_sleep(), "2026-10-30")
        self.assertEqual(list(self.sleep_dir.glob("*.tmp")), [])

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_sleep"], "2026-10-30")

    def test_write_day_deterministic_source_and_never_moves_state_back(self):
        sleep.write_day("2026-10-30", "resumo", deterministic="resumo")
        self.assertIn("source: deterministic", self.read_sleep("2026-10-30"))
        sleep.write_day("2026-10-28", "antigo")
        self.assertEqual(sleep.last_sleep(), "2026-10-30")
        sleep.write_day("2026-10-31", "novo")
        self.assertEqual(sleep.last_sleep(), "2026-10-31")

    def test_write_day_recovers_from_malformed_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("lixo", encoding="utf-8")
        sleep.write_day("2026-10-30", "resumo")
        self.assertEqual(sleep.last_sleep(), "2026-10-30")

    # ── catch_up ────────────────────────────────────────────────────────────
    def test_catch_up_writes_every_missing_night_and_never_duplicates(self):
        self.seed_day("2026-10-27")
        self.seed_day("2026-10-28")
        self.seed_day("2026-10-30")
        sleep.write_day("2026-10-26", "noite anterior")
        done = []

        def ask(prompt):
            day = prompt.split("dia ")[1][:10]
            return f"Resumo de {day}."

        paths = sleep.catch_up(now=self.now, ask=ask, on_done=done.append)

        self.assertEqual(
            [p.name for p in paths],
            ["2026-10-27.md", "2026-10-28.md", "2026-10-29.md",
             "2026-10-30.md"],
        )
        self.assertEqual(done, paths)
        self.assertIn("Resumo de 2026-10-29.", self.read_sleep("2026-10-29"))
        self.assertEqual(sleep.last_sleep(), "2026-10-30")

        self.assertEqual(sleep.catch_up(now=self.now, ask=ask), [])

    def test_catch_up_starts_from_earliest_day_without_state(self):
        self.seed_day("2026-10-28")
        self.seed_day("2026-10-30")
        paths = sleep.catch_up(now=self.now, ask=lambda _p: "resumo")
        self.assertEqual(
            [p.name for p in paths],
            ["2026-10-28.md", "2026-10-29.md", "2026-10-30.md"],
        )

    def test_catch_up_falls_back_when_ask_fails_and_ignores_on_done_errors(self):
        self.seed_day("2026-10-30")

        def broken(_prompt):
            raise RuntimeError("sem modelo")

        def bad_callback(_path):
            raise RuntimeError("callback")

        paths = sleep.catch_up(now=self.now, ask=broken,
                               on_done=bad_callback)
        self.assertEqual(len(paths), 1)
        text = self.read_sleep("2026-10-30")
        self.assertIn("source: deterministic", text)
        self.assertIn("2026-10-30", text)

    # ── bedtime ─────────────────────────────────────────────────────────────
    def test_bedtime_before_time_does_nothing(self):
        announce = mock.Mock()
        path = sleep.bedtime_tick(now=datetime(2026, 10, 31, 23, 29),
                                  ask=lambda _p: "resumo", announce=announce)
        self.assertIsNone(path)
        self.assertFalse((self.sleep_dir / "2026-10-31.md").exists())
        announce.assert_not_called()

    def test_bedtime_announces_and_compacts_today_once(self):
        self.seed_day("2026-10-31", turns=4)
        announce = mock.Mock()
        path = sleep.bedtime_tick(now=datetime(2026, 10, 31, 23, 30),
                                  ask=lambda _p: "O dia foi produtivo.",
                                  announce=announce)
        self.assertEqual(path, self.sleep_dir / "2026-10-31.md")
        self.assertTrue(path.is_file())
        announce.assert_called_once_with("Vou dormir...")
        self.assertIn("O dia foi produtivo.", self.read_sleep("2026-10-31"))

        again = sleep.bedtime_tick(now=datetime(2026, 10, 31, 23, 59),
                                   ask=lambda _p: "outro",
                                   announce=announce)
        self.assertIsNone(again)
        self.assertEqual(announce.call_count, 1)
        self.assertNotIn("outro", self.read_sleep("2026-10-31"))

    def test_bedtime_disabled_or_existing_file_does_nothing(self):
        announce = mock.Mock()
        with mock.patch.object(sleep, "load_api_keys",
                               return_value={"sleep_enabled": False}):
            self.assertIsNone(sleep.bedtime_tick(
                now=datetime(2026, 10, 31, 23, 59), announce=announce))
        announce.assert_not_called()

        sleep.write_day("2026-10-31", "já dormiu")
        announce.reset_mock()
        self.assertIsNone(sleep.bedtime_tick(
            now=datetime(2026, 10, 31, 23, 59),
            ask=lambda _p: "novo", announce=announce))
        announce.assert_not_called()
        self.assertIn("já dormiu", self.read_sleep("2026-10-31"))

    def test_bedtime_custom_time_and_announce_failure(self):
        with mock.patch.object(sleep, "load_api_keys",
                               return_value={"sleep_time": "22:00"}):
            def broken_announce(_text):
                raise RuntimeError("sem voz")

            path = sleep.bedtime_tick(now=datetime(2026, 10, 31, 22, 0),
                                      ask=lambda _p: "resumo",
                                      announce=broken_announce)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())

    # ── latest_context ──────────────────────────────────────────────────────
    def test_latest_context_uses_newest_three_and_strips_evidence(self):
        for day in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"):
            sleep.write_day(day, f"Resumo {day}.", deterministic="x" * 1000)

        text = sleep.latest_context(2500)
        self.assertTrue(text.startswith("[SONO — memória compactada]"))
        self.assertIn("## 2026-10-30", text)
        self.assertIn("## 2026-10-29", text)
        self.assertIn("## 2026-10-28", text)
        self.assertNotIn("2026-10-27", text)
        self.assertNotIn("Base determinística", text)
        self.assertLessEqual(len(text), 2500)

    def test_latest_context_is_bounded_and_empty_without_files(self):
        self.assertEqual(sleep.latest_context(), "")
        self.assertEqual(sleep.latest_context(0), "")
        sleep.write_day("2026-10-30", "Resumo bem comprido. " * 100)
        text = sleep.latest_context(80)
        self.assertLessEqual(len(text), 80)
        self.assertIn("[SONO", text)

    def test_latest_context_ignores_malformed_files(self):
        self.sleep_dir.mkdir(parents=True, exist_ok=True)
        (self.sleep_dir / "lixo.md").write_text("nada", encoding="utf-8")
        (self.sleep_dir / "2026-10-30.md").write_text("", encoding="utf-8")
        self.assertEqual(sleep.latest_context(), "")

    # ── reset ───────────────────────────────────────────────────────────────
    def test_reset_clears_state_and_files(self):
        sleep.write_day("2026-10-30", "resumo")
        self.assertTrue(self.sleep_dir.exists())
        sleep.reset()
        self.assertFalse(self.sleep_dir.exists())
        self.assertIsNone(sleep.last_sleep())


if __name__ == "__main__":
    unittest.main()
