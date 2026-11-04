"""Conclusão de fundo fica na SALA; o canal de texto do dono é outro.

O dono pediu em 02/11/2026 o aviso de conclusão no WhatsApp e cancelou em
03/11/2026: os avisos chegavam justamente enquanto ele falava pelo PC. A fala
na sala continua igual e nenhuma conclusão de fundo manda texto pelo WhatsApp.
`_completion_notify_owner` e `_completion_whatsapp_line` ficam no lugar (os
testes abaixo cobrem o contrato delas), mas sem chamador nos anúncios de fundo.
Lembretes e modo silêncio usam `_whatsapp_notify_owner` direto — cobertos em
`test_fundo_sem_whatsapp.py`.
"""
from __future__ import annotations

import re
import threading
import time

import pytest

import main
from core import wake_announce


class FakeUI:
    def __init__(self):
        self.muted = False
        self.lines: list[str] = []

    def write_log(self, text) -> None:
        self.lines.append(str(text))


class Completion:
    """Minimal JarvisLive surface for the two completion announcement paths."""

    def __init__(self, muted: bool = False):
        obj = object.__new__(main.JarvisLive)
        obj._awake = True
        obj._qoder_ignore = False
        obj._chat_memory = None
        obj.ui = FakeUI()
        obj.wake_calls: list[str] = []
        obj.said: list[str] = []
        obj.wa: list[str] = []
        obj.wa_event = threading.Event()
        obj.wa_ok = True
        obj.route_muted = muted
        obj.wake = lambda reason="": obj.wake_calls.append(reason)
        obj._say_now = lambda text, **kwargs: obj.said.append(str(text))
        obj._index_event = lambda *a, **k: None
        obj._speech_route = lambda *a, **k: {
            "muted": obj.route_muted, "subject": ""}

        def _notify(text):
            obj.wa.append(str(text))
            obj.wa_event.set()
            return obj.wa_ok

        obj._whatsapp_notify_owner = _notify
        self.obj = obj


def _wait(event: threading.Event, timeout: float = 2.0) -> bool:
    return bool(event.wait(timeout))


@pytest.fixture(autouse=True)
def _fresh_announcements():
    wake_announce.reset()
    yield
    wake_announce.reset()


def test_completion_speaks_once_and_sends_no_whatsapp_text():
    obj = Completion().obj

    assert main.JarvisLive._announce_task_completion(
        obj, "job:1:done", "Terminei em segundo plano: build.") is True

    assert obj.said == ["Terminei em segundo plano: build."]
    time.sleep(0.15)
    assert obj.wa == []

    # O mesmo id chegando de novo (retry do sidecar) não fala nem manda de novo.
    assert main.JarvisLive._announce_task_completion(
        obj, "job:1:done", "Terminei em segundo plano: build.") is False
    time.sleep(0.15)
    assert len(obj.said) == 1
    assert obj.wa == []


def test_two_different_completions_are_two_spoken_announcements():
    obj = Completion().obj

    main.JarvisLive._announce_task_completion(
        obj, "job:1:done", "Terminei em segundo plano: build.")
    main.JarvisLive._announce_task_completion(
        obj, "job:2:done", "Terminei em segundo plano: testes.")

    assert obj.said == ["Terminei em segundo plano: build.",
                        "Terminei em segundo plano: testes."]
    time.sleep(0.15)
    assert obj.wa == []


def test_notify_owner_sanitizes_the_text_before_it_leaves():
    obj = Completion().obj

    assert main.JarvisLive._completion_notify_owner(
        obj, "Terminei em /home/anthero/jarvis/main.py e avisei 5517981792464."
    ) is True

    assert _wait(obj.wa_event)
    text = obj.wa[0]
    assert "/home" not in text
    assert "main.py" not in text
    assert not re.search(r"\d{11,}", text)
    assert "5517981792464" not in text
    assert text.strip() == text
    assert text


def test_tool_name_in_the_result_is_rewritten_for_text():
    obj = Completion().obj

    line = main.JarvisLive._completion_whatsapp_line(
        obj, "Terminei: usei a ferramenta Bash e o arquivo core/whatsapp.py.")

    assert line
    assert "Bash" not in line
    assert "core/whatsapp.py" not in line
    assert "whatsapp.py" not in line


def test_long_completion_is_delivered_whole_without_ellipsis():
    # Regressão do dono (03/11/2026): o aviso de conclusão era cortado em duas
    # frases/240 caracteres e ganhava "…", obrigando-o a abrir o terminal para
    # ler o resto. O texto inteiro tem que chegar ao WhatsApp.
    obj = Completion().obj
    sentences = [f"Frase {i} da conclusão, com conteúdo de verdade."
                 for i in range(1, 12)]
    text = " ".join(sentences)

    line = main.JarvisLive._completion_whatsapp_line(obj, text)

    assert line == text
    assert len(line) > 240
    assert not line.endswith("…")
    assert not line.endswith("...")
    for sentence in sentences:
        assert sentence in line


def test_long_completion_helper_keeps_the_text_whole():
    obj = Completion().obj
    text = " ".join(f"Passo {i} concluído com sucesso." for i in range(1, 20))

    line = main.JarvisLive._completion_whatsapp_line(obj, text)

    assert line == text
    assert not line.endswith("…")


def test_not_connected_whatsapp_drops_the_text_without_crashing():
    obj = Completion().obj
    obj.wa_ok = False

    assert main.JarvisLive._completion_notify_owner(
        obj, "Terminei em segundo plano: build.") is True

    assert _wait(obj.wa_event)          # foi tentado...
    assert obj.wa == ["Terminei em segundo plano: build."]


def test_broken_whatsapp_notify_does_not_affect_the_announcement():
    obj = Completion().obj

    def _boom(_text):
        raise RuntimeError("sessão do WhatsApp caiu")

    obj._whatsapp_notify_owner = _boom

    assert main.JarvisLive._announce_task_completion(
        obj, "job:5:done", "Terminei em segundo plano: build.") is True
    assert obj.said == ["Terminei em segundo plano: build."]


def test_silence_does_not_mute_the_completion_text():
    # A rota diz "mudo" (retenção cuida da fala), mas o texto do dono continua.
    obj = Completion(muted=True).obj

    assert main.JarvisLive._completion_notify_owner(
        obj, "Terminei em segundo plano: build.") is True

    assert _wait(obj.wa_event)
    assert obj.wa == ["Terminei em segundo plano: build."]


def test_silence_does_not_reopen_the_whatsapp_copy():
    # A rota diz "mudo" (retenção cuida da fala): antes era a única exceção
    # em que o anúncio ainda mandava texto. Agora não manda nunca.
    obj = Completion(muted=True).obj

    assert main.JarvisLive._announce_task_completion(
        obj, "job:6:done", "Terminei em segundo plano: build.") is True
    main.JarvisLive._qoder_notice(obj, "O recibo saiu.")

    assert obj.said == ["Terminei em segundo plano: build.", "O recibo saiu."]
    time.sleep(0.15)
    assert obj.wa == []


def test_qoder_notice_speaks_but_does_not_reach_the_owner_whatsapp():
    obj = Completion().obj

    main.JarvisLive._qoder_notice(obj, "A tarefa terminou. O recibo saiu.")

    assert obj.said == ["A tarefa terminou.", "O recibo saiu."]
    time.sleep(0.15)
    assert obj.wa == []

    # O mesmo texto de novo: o claim do core já barra o anúncio inteiro.
    main.JarvisLive._qoder_notice(obj, "A tarefa terminou. O recibo saiu.")
    time.sleep(0.15)
    assert len(obj.said) == 2
    assert obj.wa == []
