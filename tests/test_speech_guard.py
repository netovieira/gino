"""Camada de fala: ferramenta não vira fala — intenção humana.

Cobre os cinco tipos de vazamento que o dono ouviu no log (ferramenta,
caminho, comando, JSON/marcação e idioma), garante que frase limpa passa
byte a byte e que a esteira do main.py grava `sanitized=`/`cut=` no sufixo
``[audio=...]`` sem quebrar `strip()`.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main
from core import activity, speech_guard


def _sanitize(text: str, lang: str = "pt"):
    return speech_guard.sanitize(text, lang=lang)


class LeakTypesTest(unittest.TestCase):
    def test_tool_names_become_human_intention(self):
        result = _sanitize(
            "ferramenta — Bash grep /home/anthero/jarvis/core/main.py "
            '{"cmd": "ls -la"}')
        self.assertIn("tool", result.kinds)
        self.assertNotIn("Bash", result.text)
        self.assertNotIn("grep", result.text)
        self.assertNotIn("/home", result.text)
        self.assertNotIn("{", result.text)
        self.assertTrue(result.text)
        self.assertTrue(result.changes)

    def test_file_path_is_not_spoken(self):
        result = _sanitize(
            "O arquivo /home/anthero/jarvis/logs/activity.log mostra o erro.")
        self.assertEqual(result.kinds, ("path",))
        self.assertNotIn("/home", result.text)
        self.assertIn("no log", result.text)
        self.assertIn("mostra o erro", result.text)

    def test_command_line_is_replaced_by_intention(self):
        result = _sanitize("$ grep -n audio_generated main.py")
        self.assertEqual(result.kinds, ("command",))
        self.assertNotIn("grep", result.text)
        self.assertNotIn("main.py", result.text)
        self.assertIn("log", result.text.lower())

    def test_command_inside_a_sentence_keeps_the_readable_part(self):
        result = _sanitize("Rodei pytest e passou.")
        self.assertEqual(result.kinds, ("command",))
        self.assertNotIn("pytest", result.text)
        self.assertIn("passou", result.text)

    def test_json_and_xml_are_removed(self):
        result = _sanitize('O resultado é {"ok": true} agora.')
        self.assertEqual(result.kinds, ("json",))
        self.assertNotIn("{", result.text)
        self.assertIn("resultado", result.text)
        result = _sanitize("<result>ok</result>")
        self.assertEqual(result.kinds, ("json",))
        self.assertNotIn("<", result.text)
        self.assertTrue(result.text)


class LanguageTest(unittest.TestCase):
    def test_english_sentence_is_caught_when_the_owner_speaks_portuguese(self):
        result = _sanitize("The tool failed")
        self.assertEqual(result.kinds, ("lang",))
        self.assertNotIn("tool", result.text.lower())
        self.assertNotIn("failed", result.text.lower())
        self.assertTrue(result.text)

    def test_english_sentence_is_left_alone_when_english_is_the_target(self):
        result = _sanitize("The tool failed", lang="en")
        self.assertEqual(result.kinds, ())
        self.assertEqual(result.text, "The tool failed")

    def test_clean_portuguese_sentences_pass_byte_for_byte(self):
        phrases = [
            "Bom dia, chefe. Tudo pronto por aqui.",
            "Não consegui abrir o log agora.",
            "O teste passou e o resultado é bom.",
            "Deixa eu ver no log.",
            "Vou verificar o arquivo e te aviso.",
            "Encontrei o problema e já corrigi.",
            "Não achei nada, mas sigo procurando.",
            "Pronto, terminei a análise.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = _sanitize(phrase)
                self.assertEqual(result.text, phrase)
                self.assertEqual(result.kinds, ())
                self.assertEqual(result.changes, ())

    def test_accented_portuguese_is_never_mistaken_for_english(self):
        result = _sanitize("Você já terminou a verificação?")
        self.assertEqual(result.text, "Você já terminou a verificação?")
        self.assertEqual(result.kinds, ())


class ActivityFlagTest(unittest.TestCase):
    def test_sanitized_is_canonical_and_strip_keeps_the_old_line(self):
        line = "[2026-11-01 17:10:02] Gino: Pronto."
        meta = activity.make_meta(turn=4, chan="voz", requested=True,
                                  generated=True, shown="Pronto.",
                                  sanitized="path+tool")
        flagged = activity.with_meta(line, meta)
        data = activity.parse(flagged)
        self.assertEqual(data["sanitized"], "tool+path")
        self.assertEqual(activity.strip(flagged), line)

    def test_cut_is_only_written_when_true(self):
        self.assertIn("cut", activity.make_meta(cut=True))
        self.assertNotIn("cut", activity.make_meta(cut=False))

    def test_unknown_sanitized_name_is_ignored(self):
        meta = activity.make_meta(sanitized="telepatia+lang")
        self.assertEqual(meta["sanitized"], "lang")


class _UI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)

    def holo_transcript(self, _text):
        pass


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class _Loop:
    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class SayPipelineTest(unittest.TestCase):
    """A esteira de fala usa a camada e carimba o log."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj._turn_ref = 7
        obj._turn_input_chan = "voz"
        obj._speech_audit = {}
        obj._speech_audit_lock = threading.RLock()
        obj.audio_in_queue = _Queue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        return obj

    def test_tts_receives_sanitized_text_and_log_marks_the_leak(self):
        obj = self._obj()
        seen = []
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=lambda text, st: seen.append(text) or b"\x00" * 2400):
            obj._say_blocking("ferramenta — Bash grep /home/anthero/x.py",
                              label="Gino")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("Bash", seen[0])
        self.assertNotIn("/home", seen[0])
        data = activity.parse(obj.ui.logs[-1])
        self.assertIn("tool", data["sanitized"])
        self.assertIn("path", data["sanitized"])
        self.assertNotIn("Bash", data["tts"])
        # O texto exibido continua sendo o original; a troca está no tts.
        self.assertIn("Bash", activity.strip(obj.ui.logs[-1]))

    def test_cut_speech_is_honest_and_conclusion_is_reannounced(self):
        obj = self._obj()
        obj._turn_ref = 7
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=RuntimeError("sem voz")):
            obj._say_blocking("Terminei a análise. O resultado está bom.",
                              label="Gino", origin="qoder", log=False)
        meta = obj._speech_meta(chan="voz", requested=True,
                                shown="Terminei a análise. O resultado está bom.",
                                turn=7, only_origin="qoder")
        self.assertIs(meta["audio_generated"], False)
        self.assertIs(meta["cut"], True)
        notes = list(getattr(obj, "_speech_cut_notes", []))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "O resultado está bom.")

        # Turno seguinte: só a conclusão, não a frase inteira.
        spoken = []
        obj._turn_ref = 8
        obj._say_now = lambda text, **kw: spoken.append(text)
        obj._flush_speech_cut_note()
        self.assertEqual(len(spoken), 1)
        self.assertIn("Só para fechar o assunto anterior", spoken[0])
        self.assertIn("O resultado está bom", spoken[0])
        self.assertNotIn("Terminei a análise", spoken[0])
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])


class _DrainQueue(_Queue):
    def empty(self):
        return True


class BackgroundAvisoCutNoteTest(unittest.TestCase):
    """Aviso de segundo plano cortado: a conclusão volta uma vez, calma."""

    def _obj(self):
        obj = object.__new__(main.JarvisLive)
        obj._speech_epoch = 0
        obj._asst_name = "Gino"
        obj._session_log = []
        obj._turn_done_event = None
        obj._turn_ref = 7
        obj._turn_input_chan = "voz"
        obj._speech_audit = {}
        obj._speech_audit_lock = threading.RLock()
        obj.audio_in_queue = _DrainQueue()
        obj._loop = _Loop()
        obj.ui = _UI()
        obj._wait_for_user_quiet = lambda: True
        obj._user_is_speaking = lambda: False
        obj._is_speaking = False
        obj._brain_busy = False
        obj._owner_has_floor = lambda: False
        # O teste não precisa do vigia real: a sentinela impede que
        # `_start_cut_note_watch_locked` abra a thread, que correria com as
        # asserções. O flush é chamado explicitamente.
        obj._speech_cut_watch = threading.current_thread()
        return obj

    def _age(self, seconds=10.0):
        return lambda obj: [note.update(at=note["at"] - seconds)
                            for note in obj._speech_cut_notes]

    def test_cut_aviso_is_queued_for_the_quiet_moment(self):
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.numwords.speakable",
                           side_effect=lambda text, style="words": text), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=RuntimeError("sem voz")):
            obj._say_blocking("Terminei em segundo plano: rodar os testes.",
                              log=True, origin="aviso")
        notes = list(getattr(obj, "_speech_cut_notes", []))
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["announce"])
        self.assertEqual(notes[0]["text"],
                         "Terminei em segundo plano: rodar os testes.")

    def test_answer_cut_note_keeps_its_old_shape(self):
        obj = self._obj()
        with mock.patch("core.brain.settings", return_value={}), \
                mock.patch("core.brain.synth_pcm24k",
                           side_effect=RuntimeError("sem voz")):
            obj._say_blocking("Terminei a análise. O resultado está bom.",
                              label="Gino", origin="qoder", log=False)
        notes = list(getattr(obj, "_speech_cut_notes", []))
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0]["announce"])

    def test_quiet_flush_reannounces_once_without_duplicates(self):
        obj = self._obj()
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=7, reason="tts", announce=True)
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=8, reason="tts", announce=True)
        self._age()(obj)
        calls = []
        obj._say_now = lambda text, **kw: calls.append((text, kw))
        obj._flush_speech_cut_note_quiet()
        self.assertEqual(len(calls), 1)
        line, kw = calls[0]
        self.assertIn("Só para fechar o assunto anterior", line)
        self.assertIn("Terminei em segundo plano: build.", line)
        self.assertTrue(kw.get("log"))
        self.assertEqual(kw.get("origin"), "conclusão")
        self.assertIn("Gino:", kw.get("log_text", ""))
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

        # A mesma conclusão cortada de novo não é reanunciada outra vez.
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=9, reason="meio", announce=True)
        self._age()(obj)
        obj._flush_speech_cut_note_quiet()
        self.assertEqual(len(calls), 1)
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

    def test_quiet_flush_leaves_answer_notes_for_the_turn_flush(self):
        obj = self._obj()
        obj._queue_speech_cut_note("O resultado está bom.", turn=7,
                                   reason="meio")
        obj._flush_speech_cut_note_quiet()
        self.assertEqual([n["text"] for n in obj._speech_cut_notes],
                         ["O resultado está bom."])
        spoken = []
        obj._say_now = lambda text, **kw: spoken.append(text)
        obj._turn_ref = 8
        obj._flush_speech_cut_note()
        self.assertEqual(len(spoken), 1)
        self.assertIn("O resultado está bom", spoken[0])
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

    def test_turn_flush_does_not_repeat_a_queued_aviso(self):
        obj = self._obj()
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=7, reason="tts", announce=True)
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=8, reason="meio", announce=True)
        spoken = []
        obj._say_now = lambda text, **kw: spoken.append(text)
        obj._turn_ref = 9
        obj._flush_speech_cut_note()
        self.assertEqual(len(spoken), 1)
        self.assertEqual(getattr(obj, "_speech_cut_notes"), [])

    def test_watch_only_flushes_when_owner_is_quiet(self):
        obj = self._obj()
        obj._queue_speech_cut_note("Terminei em segundo plano: build.",
                                   turn=7, reason="tts", announce=True)
        self._age()(obj)
        spoken = []
        obj._say_now = lambda text, **kw: spoken.append(text)
        obj._user_is_speaking = lambda: True
        self.assertFalse(obj._cut_note_watch_step())
        self.assertEqual(spoken, [])
        self.assertEqual(len(obj._speech_cut_notes), 1)

        obj._user_is_speaking = lambda: False
        self.assertFalse(obj._cut_note_watch_step())
        self.assertEqual(len(spoken), 1)
        self.assertTrue(obj._cut_note_watch_step())
        self.assertEqual(len(spoken), 1)


if __name__ == "__main__":
    unittest.main()
