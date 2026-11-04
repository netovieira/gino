"""Integração do portão de voz em main.py: motivo agregado e voiceprint.

O `core.groq_stt` já recusa clipe sem prova e conta `clips_dropped`. Aqui
prendemos o lado de main.py:

  * o motivo do descarte entra no Activity Log como `SYS:` e AGREGADO (nunca
    uma linha por bloco), sem atribuir fala ao dono;
  * o clipe que a válvula de energia sustentada marcou `voice_proven=False`
    NÃO alimenta o voiceprint, mas a fala continua no pipeline;
  * o callback é registrado no `GroqListener`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main

from core import groq_stt


class FakeUI:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_log(self, text) -> None:
        self.lines.append(str(text))


def _bare() -> object:
    obj = object.__new__(main.JarvisLive)
    obj.ui = FakeUI()
    return obj


def test_gate_drop_logs_reason_aggregated_with_sys_label():
    obj = _bare()
    for reason in ("banda", "banda", "envelope", "curto", "banda"):
        main.JarvisLive._stt_gate_drop(obj, reason, 0.2)

    assert len(obj.ui.lines) == 1, "um log por bloco seria spam"
    line = obj.ui.lines[0]
    assert line.startswith("SYS: "), "rótulo honesto de sistema"
    assert "banda=3" in line and "envelope=1" in line and "curto=1" in line
    assert "5 clipe(s)" in line
    assert "voiceprint" in line and "STT" in line
    assert "YOU:" not in line and "ordem direta" not in line


def test_gate_drop_does_not_log_every_clip():
    obj = _bare()
    for _ in range(main.JarvisLive._GATE_LOG_EVERY - 1):
        main.JarvisLive._stt_gate_drop(obj, "silencio", 0.1)
    assert obj.ui.lines == []
    main.JarvisLive._stt_gate_drop(obj, "silencio", 0.1)
    assert len(obj.ui.lines) == 1


def test_unproven_voiceprint_skip_is_aggregated_and_honest():
    obj = _bare()
    for _ in range(main.JarvisLive._GATE_LOG_EVERY - 1):
        main.JarvisLive._note_unproven_voiceprint(obj)
    assert obj.ui.lines == []
    main.JarvisLive._note_unproven_voiceprint(obj)
    assert len(obj.ui.lines) == 1
    line = obj.ui.lines[0]
    assert line.startswith("SYS: ")
    assert "sem prova de voz" in line
    assert "voiceprint" in line
    assert "fala seguiu" in line


def test_set_stt_engine_wires_gate_drop_callback(monkeypatch):
    captured: dict = {}

    class FakeListener:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.sleeping = False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(groq_stt, "settings", lambda getter: {
        "api_key": "chave-de-teste", "model": "m", "language": "pt",
        "preroll_s": 1.5, "max_utterance_s": 15.0,
    })
    monkeypatch.setattr(groq_stt, "GroqListener", FakeListener)

    obj = _bare()
    obj._groq = None
    obj._stt_name_hint = lambda language: "Gino."
    obj._series_precheck = lambda pcm: True
    obj._stt_lost_audio = lambda *a, **k: None
    obj._wire_stt_hooks = lambda listener: None
    obj._wake_enabled = False
    obj._awake = False

    assert main.JarvisLive.set_stt_engine(obj, "groq") == "groq"
    assert captured.get("on_gate_drop") == obj._stt_gate_drop


def test_set_stt_engine_whisper_wires_gate_drop_callback(monkeypatch):
    from core import local_stt

    captured: dict = {}

    class FakeLocalListener:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.sleeping = False

        def stop(self) -> None:
            pass

    monkeypatch.setattr(local_stt, "settings", lambda getter: {
        "model": "small", "language": "pt", "device": "cpu",
        "compute_type": "int8", "preroll_s": 1.5, "max_utterance_s": 15.0,
    })
    monkeypatch.setattr(local_stt, "LocalWhisperListener", FakeLocalListener)
    monkeypatch.setattr(groq_stt, "settings", lambda getter: {
        "api_key": "", "model": "m", "language": "pt",
        "preroll_s": 1.5, "max_utterance_s": 15.0,
    })

    obj = _bare()
    obj._groq = None
    obj._stt_name_hint = lambda language: "Gino."
    obj._series_precheck = lambda pcm: True
    obj._stt_lost_audio = lambda *a, **k: None
    obj._wire_stt_hooks = lambda listener: None
    obj._wake_enabled = False
    obj._awake = False

    assert main.JarvisLive.set_stt_engine(obj, "whisper") == "whisper"
    assert captured.get("on_gate_drop") == obj._stt_gate_drop
