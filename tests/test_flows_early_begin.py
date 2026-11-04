"""C: o turno de fluxos abre antes da diarização e arma só com o voiceprint."""
from __future__ import annotations

import threading

import main


class FakeUI:
    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, _state):
        pass


class FakeSpeaker:
    owner_name = "Anthero"

    def __init__(self, known=True, authorized=True, verdict="known"):
        self.known = known
        self.authorized = authorized
        self.verdict = verdict
        self.analyzed = 0

    def list(self):
        return [{"name": "Anthero", "owner": True, "authorized": True}]

    def analyze_utterance(self, _pcm, transcribe=None, full_text=""):
        self.analyzed += 1
        return {
            "labeled_text": full_text,
            "primary": {"known": self.known, "authorized": self.authorized,
                        "verdict": self.verdict, "effective_score": 0.9},
            "primary_name": "Anthero",
            "segments": [],
        }


class FakeLive:
    def __init__(self):
        self.asks = []

    def ask(self, payload, **kw):
        self.asks.append((payload, kw))


def _obj(speaker):
    obj = object.__new__(main.JarvisLive)
    obj._speaker = speaker
    obj._voice_id_enabled = True
    obj._power = None
    obj._qoder_lock = threading.RLock()
    obj._qoder_current_id = None
    obj._qoder_turns = {}
    obj._qoder_text_buf = {}
    obj._qoder_spoken = {}
    obj._qoder_meta = {}
    obj._qoder_live = FakeLive()
    obj._qoder_ignore = False
    obj._qoder_retry_used = False
    obj._qoder_first_sentence = False
    obj._qoder_tool_count = 0
    obj._turn_ref = 3
    obj._turn_input_chan = "voz"
    obj._turn_done_event = None
    obj._loop = None
    obj._reply_active = False
    obj._brain_busy = False
    obj.ui = FakeUI()
    obj.events = []
    obj.events_lock = threading.Lock()

    def record(name, *args):
        with obj.events_lock:
            obj.events.append((name, *args))

    obj._flows_begin_turn = lambda turn_id, text, meta=None, armed=True: record(
        "begin", armed, turn_id, speaker.analyzed)
    obj._flows_arm_speech = lambda turn_id, allowed=True: record(
        "arm", bool(allowed), turn_id)
    obj._flows_cancel = lambda turn_id, reason="": record(
        "cancel", reason, turn_id)
    obj._device_enroll_cancel_gate = lambda *a, **k: False
    obj._restart_answer_expected = lambda *a, **k: False
    obj._mail_send_answer_expected = lambda *a, **k: False
    obj._owner_direct_action_gate = lambda *a, **k: False
    obj._cinema_mode_gate = lambda *a, **k: False
    obj._silence_mode_gate = lambda *a, **k: False
    obj._series_mode_gate = lambda *a, **k: False
    obj._mirror_show_gate = lambda *a, **k: False
    obj._try_action_consent = lambda *a, **k: False
    obj._emotion_hears_owner = lambda _text: False
    obj._series_decision_for = lambda _pcm: None
    obj._cinema_active_now = lambda: False
    obj._series_mode_on = lambda: False
    obj._cinema_observe = lambda *a, **k: None
    obj._auto_enroll_voice = lambda *a, **k: None
    obj._cancel_narration = lambda *a, **k: None
    obj._answers_on_whatsapp = lambda _meta: False
    obj._say_now = lambda text, **kw: record("say", str(text))
    obj._qoder_hold = lambda *a, **k: None
    obj._flows_cancel_original = None
    return obj


PCM = b"\x01\x00" * 4000


def test_flows_turn_opens_before_diarization_and_arms_after_accept():
    speaker = FakeSpeaker()
    obj = _obj(speaker)
    main.JarvisLive._qoder_turn(obj, "que horas são?", PCM)
    names = [event[0] for event in obj.events]
    assert names[0] == "begin"
    assert names.count("begin") == 1
    begin = obj.events[0]
    assert begin[1] is False                              # desarmada
    assert begin[3] == 0                                  # ANTES da diarização
    assert speaker.analyzed == 1                          # diarização rodou
    arm = next(e for e in obj.events if e[0] == "arm")
    assert arm[1] is True
    assert names.index("arm") > 0
    assert obj._qoder_live.asks                           # cérebro foi chamado


def test_refused_voiceprint_cancels_before_arming():
    speaker = FakeSpeaker(known=False, authorized=False, verdict="unknown")
    obj = _obj(speaker)
    obj._voice_indulgence_ok = lambda *a: False
    main.JarvisLive._qoder_turn(obj, "que horas são?", PCM)
    names = [event[0] for event in obj.events]
    assert "cancel" in names
    assert "arm" not in names
    cancel = next(e for e in obj.events if e[0] == "cancel")
    assert "voiceprint" in cancel[1]
    assert speaker.analyzed == 1
    assert obj._qoder_live.asks == []
