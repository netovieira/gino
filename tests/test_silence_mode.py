"""Modo silêncio: a boca cala, a orelha e a memória continuam.

Cobre o contrato ditado pelo dono em 02/11/2026:
  1. "silêncio" sozinho emudece tudo, microfone aberto;
  2. "silêncio TheroTV" corta só esse assunto, outro continua falando;
  3. texto digitado nunca liga o modo;
  4. a resposta em silêncio vira texto na tela e a fala fica retida;
  5. a liberação entrega resumo por assunto + as falas retidas em ordem;
  6. a memória continua gravando;
  7. o modo cinema não regride.
"""
from __future__ import annotations

import threading
import time

import pytest

import main
from core import silence_gate


class FakeUI:
    def __init__(self):
        self.muted = False
        self.logs: list[str] = []
        self.state = ""

    def write_log(self, text: str) -> None:
        self.logs.append(str(text))

    def set_state(self, state: str) -> None:
        self.state = str(state)


class FakeGroq:
    def __init__(self):
        self.sleeping = False


class FakeChat:
    def __init__(self):
        self.turns: list[tuple[str, str]] = []

    def append_turn(self, role: str, text: str) -> None:
        self.turns.append((role, text))


@pytest.fixture()
def live(tmp_path, monkeypatch):
    # O índice real não pode influenciar o assunto destes testes.
    monkeypatch.setattr(main._memory_index, "active",
                        lambda: {"project": "", "subject": ""})
    obj = object.__new__(main.JarvisLive)
    obj._asst_name = "Gino"
    obj._awake = True
    obj._turn_ref = 1
    obj.ui = FakeUI()
    obj._groq = FakeGroq()
    obj._chat_memory = None
    obj._loop = None
    obj.audio_in_queue = None
    obj._session_log = []
    obj._speech_audit = {}
    obj._speech_audit_lock = threading.Lock()
    obj._silence_state_file = tmp_path / "silence_mode.json"
    obj._silence_queue_file = tmp_path / "silence_queue.json"
    obj._silence_mode = silence_gate.SilenceGate(
        state_path=obj._silence_state_file,
        queue_path=obj._silence_queue_file)
    obj._owner_voice = lambda pcm, device=None: (True, 0.93, "voz do dono")
    obj._log_heard = lambda *a, **k: None
    obj.interrupt = lambda: None
    obj._note_owner_speech = lambda *a, **k: None
    obj.wa: list[str] = []
    obj.wa_event = threading.Event()

    def _notify(text):
        obj.wa.append(str(text))
        obj.wa_event.set()
        return True

    obj._whatsapp_notify_owner = _notify
    obj.events: list[tuple[str, str]] = []
    yield obj


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_word_alone_mutes_everything_and_the_microphone_stays_open(live):
    handled = main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)

    assert handled is True
    assert live._silence_mode.muted_all is True
    assert live._silence_mode.is_muted() is True
    # Orelha aberta: nada fechou microfone, dormiu ou mudou o estado do app.
    assert live.ui.muted is False
    assert live._awake is True
    assert live._groq.sleeping is False
    assert live.ui.state == ""
    # Estado persistido e recarregado por processo (padrão do cinema).
    reloaded = silence_gate.SilenceGate(state_path=live._silence_state_file,
                                        queue_path=live._silence_queue_file)
    assert reloaded.muted_all is True


def test_named_subject_mutes_only_that_subject(live):
    handled = main.JarvisLive._silence_mode_gate(
        live, "silêncio TheroTV", b"\x00" * 3200)
    assert handled is True
    assert live._silence_mode.muted_all is False
    assert live._silence_mode.is_muted(text="O TheroTV está no ar") is True
    assert live._silence_mode.is_muted(subject="financas",
                                       text="Fechei o caixa do dia") is False
    # A decisão única concorda com o gate.
    assert live._speech_route("O TheroTV está no ar")["muted"] is True
    assert live._speech_route("Fechei o caixa do dia")["muted"] is False
    # Acumula assuntos diferentes.
    main.JarvisLive._silence_mode_gate(live, "silêncio Finanças",
                                       b"\x00" * 3200)
    assert live._silence_mode.is_muted(subject="Finanças") is True
    assert live._speech_route("Fechei o caixa do dia",
                              subject="Finanças")["muted"] is True
    assert live._speech_route("Fechei o caixa do dia",
                              subject="TheroTV")["muted"] is True
    assert live._speech_route("Bom dia", subject="outro assunto")["muted"] is False


def test_parse_order_reads_the_owner_wording():
    assert silence_gate.parse_order("silêncio")["all"] is True
    assert silence_gate.parse_order("Gino, silêncio, por favor")["all"] is True
    named = silence_gate.parse_order("silêncio o projeto X")
    assert named["triggered"] is True and named["all"] is False
    assert named["subject"] == "X"
    assert silence_gate.parse_order("que silêncio é esse?")["triggered"] is False
    assert silence_gate.parse_order("modo cinema")["triggered"] is False


def test_name_releases_only_that_subject_while_all_is_muted(tmp_path):
    gate = silence_gate.SilenceGate(
        state_path=tmp_path / "s.json", queue_path=tmp_path / "q.json")
    assert gate.apply_order("")["action"] == "mute_all"
    assert gate.apply_order("TheroTV")["action"] == "release_subject"
    assert gate.is_muted(text="O TheroTV está no ar") is False
    assert gate.is_muted(text="Finanças fechou o caixa") is True
    # A mesma palavra solta libera o resto.
    assert gate.apply_order("")["action"] == "release_all"
    assert gate.is_muted() is False


def test_typed_silence_never_arms_the_mode(live):
    assert main.JarvisLive._silence_mode_gate(live, "silêncio", None) is False
    assert live._silence_mode.active is False
    assert live._silence_mode.muted_all is False
    # Um turno digitado que chega em _qoder_turn também não liga o modo.
    # (pcm None é o marcador de texto; o gate sai antes de qualquer estado.)
    assert main.JarvisLive._silence_mode_gate(live, "silêncio total", None) is False
    assert live._silence_mode.active is False


def test_silent_answer_goes_text_and_the_audio_is_retained(live):
    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)

    main.JarvisLive._say_blocking(
        live, "O TheroTV foi atualizado.", log=True, origin="aviso",
        subject="TheroTV")

    assert live._silence_mode.retained_count == 1
    entry = live._silence_mode.pending()[0]
    assert entry["text"] == "O TheroTV foi atualizado."
    assert entry["subject"] == "TheroTV"
    assert entry["destination"] == "room"
    # Texto na tela e no WhatsApp do dono (thread curta).
    assert any("O TheroTV foi atualizado." in line for line in live.ui.logs)
    assert _wait_until(lambda: live.wa_event.is_set())
    assert any("O TheroTV foi atualizado." in text for text in live.wa)
    # Nenhum áudio foi enfileirado: a fala ficou retida.
    assert live.audio_in_queue is None


def test_release_delivers_subject_summary_and_retained_speech(live):
    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)
    live._silence_mode.retain("TheroTV atualizado.", subject="TheroTV",
                              voice_id="voz-1", label="Gino")
    live._silence_mode.retain("Caixa fechado.", subject="Finanças")
    live._silence_mode.retain("Áudio do celular.", subject="Finanças",
                              destination="whatsapp")

    def _say(text, **kwargs):
        live.events.append(("say", text))

    def _wa_audio(text):
        live.events.append(("wa", text))
        return True

    live._say_blocking = _say
    live._whatsapp_send_owner_audio = _wa_audio
    live._silence_pause = lambda: None

    handled = main.JarvisLive._silence_mode_gate(live, "silêncio",
                                                 b"\x00" * 3200)
    assert handled is True
    assert live._silence_mode.active is False
    assert _wait_until(lambda: len(live.events) >= 5)

    kinds = [k for k, _ in live.events]
    texts = [t for _, t in live.events]
    # Resumo curto por assunto primeiro...
    assert texts[0].startswith("Sobre TheroTV:")
    assert texts[1].startswith("Sobre Finanças:")
    # ...depois as falas retidas, na ordem em que foram cortadas.
    assert ("say", "TheroTV atualizado.") in live.events
    assert ("say", "Caixa fechado.") in live.events
    assert ("wa", "Áudio do celular.") in live.events
    assert kinds.count("say") == 4
    # O resumo também vai em texto para o WhatsApp do dono.
    assert _wait_until(lambda: any("Sobre TheroTV:" in t for t in live.wa))
    # Fila consumida e estado liberado.
    assert live._silence_mode.retained_count == 0


def test_memory_keeps_recording_while_the_mouth_is_muted(live):
    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)
    chat = FakeChat()
    live._chat_memory = chat
    live._qoder_lock = threading.RLock()
    live._qoder_ignore = False
    live._qoder_say_events = {}
    live._qoder_turns = {"t1": "pedido do dono"}
    live._qoder_meta = {"t1": {"source": "owner", "speak": True,
                               "input_chan": "voz", "turn_ref": 1}}
    live._qoder_text_buf = {"t1": "Resposta guardada."}
    live._qoder_spoken = {"t1": []}
    live._qoder_say = lambda *a, **k: None
    live._write_answer_line = lambda *a, **k: None
    live._qoder_finish = lambda *a, **k: None
    live._voice_training_hint = lambda: None
    live._memory_librarian = None
    live._index_events = []
    live._index_event = lambda *a, **k: live._index_events.append((a, k))

    main.JarvisLive._qoder_on_done(live, "t1", "Resposta guardada.")

    assert ("assistant", "Resposta guardada.") in chat.turns
    assert live._index_events, "o índice da memória continua recebendo o turno"
    assert live._silence_mode.muted_all is True


def test_cinema_mode_still_toggles(live, tmp_path):
    live._cinema_mode_enabled = True
    live._cinema_state_file = tmp_path / "cinema_mode.json"
    live._cinema_active = False
    live._cinema_loaded = False
    live._cinema_lock = threading.RLock()
    live._cinema_trigger_count = 3
    live._cinema_trigger_window_s = 60.0
    live._cinema_idle_exit_s = 600.0
    live._cinema_prompt_ttl_s = 60.0
    live._cinema_prompt_cooldown_s = 600.0
    live._cinema_since = 0.0
    live._cinema_last_audio = 0.0
    live._cinema_events = []
    live._cinema_counted = {}
    live._cinema_prompted_at = 0.0
    live._cinema_pending = False
    live._cinema_timer = None
    live._cinema_owner_ok = lambda pcm, device=None: True
    live._say_now = lambda *a, **k: live.events.append(("say", a[0]))

    handled = main.JarvisLive._cinema_mode_gate(
        live, "liga o modo cinema", b"\x00" * 3200)

    assert handled is True
    assert live._cinema_active is True
    assert live._cinema_state_file.exists()
    # E o cinema permanece independente do silêncio (estado próprio).
    assert live._silence_mode.active is False
