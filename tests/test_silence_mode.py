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

import hashlib
import json
import threading
import time

import pytest

import main
from core import silence_gate
from core.memory_index import slugify


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
    # NENHUM estado real do dono entra aqui: nem o do gate, nem o da
    # palavra-código (UC-4), nem o config que a espelha. Sem o _silence_code_state_file
    # e sem o API_CONFIG_PATH, o teste enxergava memory/silence_code.json do
    # dono e a palavra gravada nele mudava a rota da ordem no meio do teste.
    monkeypatch.setattr(main, "API_CONFIG_PATH", tmp_path / "api_keys.json")
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
    obj._silence_code_state_file = tmp_path / "silence_code.json"
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


@pytest.fixture()
def live_com_palavra(live):
    """O estado do dono reproduzido em arquivo TEMPORÁRIO: «silêncio» gravado
    como palavra-código (a ordem literal dele, 09/11/2026 16:40).

    É o caso que colide com o gate — exatamente o que quebrava o teste quando
    o fixture lia o memory/silence_code.json real.
    """
    _gravar_palavra(live, "silêncio")
    return live


def _gravar_palavra(obj, palavra: str, projetos: tuple[str, ...] = ()) -> None:
    """Escreve a palavra-código no estado UC-4 temporário e refaz o objeto."""
    obj._silence_code_state_file.write_text(json.dumps({
        "version": 1,
        "code_word": palavra,
        "active_projects": [{"slug": slugify(p, ""), "label": p}
                            for p in projetos],
    }, ensure_ascii=False), encoding="utf-8")
    obj._silence_code_mode = None  # força o próximo acesso a reler o arquivo


def _md5_estado_real() -> str | None:
    caminho = main.SILENCE_CODE_STATE_PATH
    try:
        return hashlib.md5(caminho.read_bytes()).hexdigest()
    except Exception:
        return None


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


# ── a regra única: palavra isolada = global, palavra + nome = escopado ───────
# Buraco medido (09/11/2026): com «silêncio» gravada como palavra-código em
# memory/silence_code.json, «silêncio TheroTV» era tratada pelo SilenceCode
# antes do gate e o escopado caía num segundo estado — o teste acima falhava
# só porque o fixture lia o arquivo do dono.

def _boca_muda(live) -> None:
    """Silêncio que libera fala threads de TTS reais: a boca é stub."""
    live._say_blocking = lambda text, **k: live.events.append(("say", text))
    live._whatsapp_send_owner_audio = lambda text: (
        live.events.append(("wa", text)) or True)
    live._silence_pause = lambda: None


def test_a_palavra_gravada_sozinha_liga_o_global_e_um_estado_so(live_com_palavra):
    live = live_com_palavra
    antes = live._silence_code_state_file.read_text(encoding="utf-8")

    handled = main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)

    assert handled is True
    assert live._silence_mode.muted_all is True
    # O global é o gate. O SilenceCode não mexeu no próprio estado: a regra
    # é uma só, com um estado só.
    assert (live._silence_code_state_file.read_text(encoding="utf-8") == antes)
    assert live._silence_code().active_projects() == []


def test_a_palavra_gravada_seguida_de_nome_e_escopada_so_aquele_projeto(
        live_com_palavra):
    live = live_com_palavra
    antes = live._silence_code_state_file.read_text(encoding="utf-8")

    # A letra do dono («silêncio terotv») e a letra do projeto (TheroTV)
    # viram a mesma coisa: um assunto escopado. Como o nome chega grafado, o
    # slug acompanha — a decisão de rota é a regra, não a grafia.
    handled = main.JarvisLive._silence_mode_gate(
        live, "silêncio therotv", b"\x00" * 3200)

    assert handled is True
    assert live._silence_mode.muted_all is False
    assert live._silence_mode.is_muted(text="O TheroTV está no ar") is True
    assert live._silence_mode.is_muted(subject="financas",
                                       text="Fechei o caixa do dia") is False
    assert live._speech_route("O TheroTV está no ar")["muted"] is True
    assert live._speech_route("Fechei o caixa do dia")["muted"] is False
    # Escopado é do gate, não do SilenceCode: nenhum segundo estado, nenhum
    # segundo resumo esperando liberação por «libera silêncio».
    assert (live._silence_code_state_file.read_text(encoding="utf-8") == antes)
    assert live._silence_code().active_projects() == []


@pytest.mark.parametrize("fala", [
    "o silêncio caiu na conversa",
    "silêncio caiu na conversa",
    "silêncio é uma virtude",
    "que silêncio bom hoje",
    "o silêncio do projeto therotv me ajudou",
])
def test_fala_corrida_com_a_palavra_dentro_nao_e_comando(live_com_palavra, fala):
    live = live_com_palavra

    handled = main.JarvisLive._silence_mode_gate(live, fala, b"\x00" * 3200)

    assert handled is False, "conversa virou comando"
    assert live._silence_mode.active is False
    assert live._silence_code().active_projects() == []


@pytest.mark.parametrize("fala", [
    "silêncio", "Silêncio", "SILENCIO", "silencio", "silêncio.",
    "silêncio!", "Gino, silêncio", "silêncio, por favor", "silêncio total",
])
def test_caixa_pontuacao_e_acento_na_palavra_isolada_sao_global(
        live_com_palavra, fala):
    live = live_com_palavra

    assert main.JarvisLive._silence_mode_gate(live, fala, b"\x00" * 3200) is True
    assert live._silence_mode.muted_all is True, f"{fala!r} não ligou o global"


@pytest.mark.parametrize("fala", [
    "silêncio TheroTV", "Silêncio, therotv.", "SILENCIO THEROTV",
    "silencio therotv", "Gino, silêncio TheroTV", "silêncio o projeto TheroTV",
])
def test_caixa_pontuacao_e_acento_com_nome_sao_escopadas(live_com_palavra, fala):
    live = live_com_palavra

    assert main.JarvisLive._silence_mode_gate(live, fala, b"\x00" * 3200) is True
    assert live._silence_mode.muted_all is False, f"{fala!r} ligou o global"
    subjects = live._silence_mode.muted_subjects()
    assert [i["slug"] for i in subjects] == ["therotv"], f"{fala!r} -> {subjects}"
    assert live._silence_mode.is_muted(text="O TheroTV está no ar") is True
    assert live._silence_mode.is_muted(subject="financas") is False


def test_repetir_a_palavra_desliga_o_global(live_com_palavra):
    live = live_com_palavra
    _boca_muda(live)

    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)
    assert live._silence_mode.muted_all is True
    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)

    assert live._silence_mode.active is False
    assert live._silence_mode.muted_all is False
    assert _wait_until(lambda: live._silence_mode.retained_count == 0)


def test_repetir_o_nome_desliga_so_aquele_assunto(live_com_palavra):
    live = live_com_palavra
    _boca_muda(live)

    main.JarvisLive._silence_mode_gate(live, "silêncio therotv", b"\x00" * 3200)
    main.JarvisLive._silence_mode_gate(live, "silêncio financas", b"\x00" * 3200)
    assert live._silence_mode.is_muted(subject="Finanças") is True
    main.JarvisLive._silence_mode_gate(live, "silêncio therotv", b"\x00" * 3200)

    assert live._silence_mode.is_muted(text="O TheroTV está no ar") is False
    assert live._silence_mode.is_muted(subject="Finanças") is True
    assert live._silence_mode.muted_all is False


def test_o_escopado_nunca_desliga_o_global_por_acaso(live_com_palavra):
    live = live_com_palavra
    _boca_muda(live)

    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)
    assert live._silence_mode.muted_all is True
    main.JarvisLive._silence_mode_gate(live, "silêncio therotv", b"\x00" * 3200)

    # O nome solta aquele assunto; o resto da sala continua calado.
    assert live._silence_mode.muted_all is True
    assert live._silence_mode.is_muted(text="O TheroTV está no ar") is False
    assert live._silence_mode.is_muted(subject="financas",
                                       text="Fechei o caixa do dia") is True


def test_libera_silencio_devolve_a_sala_e_limpa_a_uc4(live_com_palavra):
    live = live_com_palavra
    _boca_muda(live)
    _gravar_palavra(live, "silêncio", ("therotv",))
    main.JarvisLive._silence_mode_gate(live, "silêncio", b"\x00" * 3200)
    assert live._silence_mode.muted_all is True

    handled = main.JarvisLive._silence_mode_gate(
        live, "libera silêncio", b"\x00" * 3200)

    assert handled is True
    assert live._silence_mode.active is False
    assert live._silence_code().active_projects() == []
    # Um resumo só para a sala: a fila é compartilhada, não dobrada.
    assert _wait_until(lambda: any("liberado" in t for t in live.wa))
    falas = [t for kind_evt, t in live.events if kind_evt == "say"]
    assert len(falas) == 1, f"resumo dobrado na liberação: {falas}"


def test_libera_silencio_sem_nada_calado_nao_arma_o_global(live_com_palavra):
    """O toggle do gate não pode ser acionado pela liberação.

    «libera silêncio» dito com a sala falando é "nada a liberar" — nunca
    "liga o silêncio total", que é o que um apply_order("") ingenuo faria.
    """
    live = live_com_palavra
    _boca_muda(live)

    assert main.JarvisLive._silence_mode_gate(
        live, "libera silêncio", b"\x00" * 3200) is True
    assert live._silence_mode.muted_all is False
    assert live._silence_mode.active is False
    assert live._silence_code().active_projects() == []


def test_palavra_codigo_propria_continua_viva_na_uc4(live):
    _gravar_palavra(live, "Oklahoma")

    assert main.JarvisLive._silence_mode_gate(
        live, "Oklahoma TheroTV", b"\x00" * 3200) is True
    assert live._silence_code().active_projects() == [
        {"slug": "therotv", "label": "TheroTV"}]
    assert live._silence_mode.active is False
    assert live._speech_route("O TheroTV está no ar")["muted"] is True
    # E a palavra do silêncio segue sendo do gate, independente dela.
    assert main.JarvisLive._silence_mode_gate(
        live, "silêncio", b"\x00" * 3200) is True
    assert live._silence_mode.muted_all is True
    assert main.JarvisLive._silence_mode_gate(
        live, "libera Oklahoma", b"\x00" * 3200) is True
    assert live._silence_code().active_projects() == []


def test_estes_testes_nunca_escrevem_no_estado_real_do_dono(
        live_com_palavra, tmp_path):
    live = live_com_palavra
    antes = _md5_estado_real()
    assert live._silence_code_state_file.is_relative_to(tmp_path)

    for fala in ("silêncio", "silêncio terotv", "silêncio financas",
                 "libera silêncio", "Oklahoma terotv"):
        main.JarvisLive._silence_mode_gate(live, fala, b"\x00" * 3200)

    assert _md5_estado_real() == antes, (
        "memory/silence_code.json do dono foi tocado pelo teste")
