"""N microfones, um stream cada, comparados — nunca somados.

Nenhum teste abre microfone: as "chaves" são arrays numpy e o PortAudio não é
importado. Os fakes de alto-falante aceitam ``device=`` (a suíte já quebrou por
isso) — aqui nenhum speaker real participa.
"""
from __future__ import annotations

import numpy as np
import pytest

from core import mic_compare, mic_route
from core.multi_mic import (MultiMic, activate_filter_conf, channel_count,
                            physical_sources, unavailable_sources)


def _tone(n: int, amp: float, freq: float = 220.0, sr: int = 16000) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / float(sr)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(n: int, amp: float, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n).astype(np.float32) * amp)


class FakeSpeaker:
    """Identificador de voz falso: a assinatura inclui ``device=``."""

    def identify(self, _emb, include_auto: bool = False,
                 voiced_seconds: float | None = None,
                 device: str | None = None):
        return {"name": "dono", "known": True, "authorized": True,
                "verdict": "known", "device": device}


def _two(clock=None):
    return MultiMic(["fone", "webcam"], clock=(clock or (lambda: 0.0)))


# ── a comparação em si ──────────────────────────────────────────────────────


def test_single_microphone_never_enters_the_comparison():
    mic = MultiMic(["fone"])
    assert mic.push(0, _noise(1024, 0.3)) is None
    assert mic.count == 1


def test_waits_until_every_mic_delivered_a_new_block():
    now = [0.0]
    mic = _two(lambda: now[0])
    loud = _noise(1024, 0.4, seed=2)
    quiet = np.zeros(1024, dtype=np.float32)
    assert mic.push(0, loud) is None          # fone chegou, webcam não
    now[0] = 0.01
    decision = mic.push(1, quiet)
    assert decision is not None
    assert mic.cycles == 1


def test_near_selects_the_loud_uncorrelated_microphone():
    now = [0.0]
    mic = _two(lambda: now[0])
    speech = _noise(1024, 0.35, seed=3)
    room = np.zeros(1024, dtype=np.float32)
    mic.push(1, room)
    now[0] = 0.005
    decision = mic.push(0, speech)
    assert decision.verdict == mic_compare.NEAR
    assert decision.source == 0
    assert decision.name == "fone"


def test_the_decision_carries_one_signal_never_a_sum():
    now = [0.0]
    mic = _two(lambda: now[0])
    a = np.full(1024, 0.25, dtype=np.float32)
    b = np.full(1024, 0.05, dtype=np.float32)
    mic.push(0, a)
    now[0] = 0.004
    decision = mic.push(1, b)
    assert decision.block is not None
    assert np.allclose(decision.block, a) or np.allclose(decision.block, b)
    assert not np.allclose(decision.block, a + b)


def test_the_same_sound_at_both_mics_is_diffuse_not_the_owner():
    now = [0.0]
    mic = _two(lambda: now[0])
    tone = _tone(1024, 0.3)
    mic.push(0, tone)
    now[0] = 0.002
    decision = mic.push(1, tone.copy())
    assert decision.verdict == mic_compare.DIFFUSE
    assert decision.source is not None            # portão pode ouvir…
    assert decision.verdict != mic_compare.NEAR   # …mas não abre turno de dono


def test_ambiguous_comparison_does_not_select_owner_speech():
    now = [0.0]
    mic = _two(lambda: now[0])
    loud = _noise(1024, 0.3, seed=4)
    # 3 dB de diferença e nada de correlação: não é "near" nem "diffuse".
    other = _noise(1024, 0.21, seed=5)
    mic.push(0, loud)
    now[0] = 0.003
    decision = mic.push(1, other)
    assert decision.verdict == mic_compare.AMBIGUOUS
    assert decision.votes == ()


def test_blocks_from_different_instants_do_not_make_a_verdict():
    now = [0.0]
    mic = MultiMic(["a", "b"], tolerance_ms=5.0, clock=lambda: now[0])
    mic.push(0, _noise(1024, 0.3))
    now[0] = 0.2                       # 200 ms depois: não são o mesmo quadro
    assert mic.push(1, np.zeros(1024, dtype=np.float32)) is None
    assert mic.cycles == 0


def test_silence_is_ambiguous_and_never_near():
    now = [0.0]
    mic = _two(lambda: now[0])
    zeros = np.zeros(1024, dtype=np.float32)
    mic.push(0, zeros)
    now[0] = 0.001
    decision = mic.push(1, zeros.copy())
    assert decision.verdict == mic_compare.AMBIGUOUS
    assert decision.source == 0


def test_three_mics_vote_on_their_pairs_and_a_tie_is_ambiguous():
    now = [0.0]
    mic = MultiMic(["a", "b", "c"], clock=lambda: now[0])
    assert len(mic._comparators) == 3           # um por par
    # a e b mudos, c com sinal: os dois pares de c votam em c.
    zeros = np.zeros(1024, dtype=np.float32)
    mic.push(0, zeros)
    now[0] = 0.001
    mic.push(1, zeros.copy())
    now[0] = 0.002
    decision = mic.push(2, _noise(1024, 0.4, seed=6))
    assert decision.verdict == mic_compare.NEAR
    assert decision.source == 2
    assert decision.votes == (2, 2)


def test_tie_between_two_near_votes_is_ambiguous():
    now = [0.0]
    mic = MultiMic(["a", "b", "c"], clock=lambda: now[0])
    zeros = np.zeros(1024, dtype=np.float32)
    mic.push(0, _noise(1024, 0.4, seed=7))
    now[0] = 0.001
    mic.push(1, _noise(1024, 0.4, seed=8))
    now[0] = 0.002
    decision = mic.push(2, zeros)
    # dois vizinhos gritando sem correlação: votos empatados (0 e 1)
    assert decision.verdict != mic_compare.NEAR


def test_ambiguous_cycles_keep_the_current_source():
    """Ruído de sala não pode alternar a fonte a cada ciclo."""
    now = [0.0]
    mic = _two(lambda: now[0])
    zeros = np.zeros(1024, dtype=np.float32)
    mic.push(0, zeros)
    now[0] = 0.001
    first = mic.push(1, zeros.copy())
    assert first.source == 0
    for k in range(8):
        loud = _noise(1024, 0.30, seed=20 + k)
        quiet = _noise(1024, 0.21, seed=40 + k)
        a, b = (loud, quiet) if k % 2 == 0 else (quiet, loud)
        now[0] += 0.005
        assert mic.push(0, a) is None
        now[0] += 0.001
        decision = mic.push(1, b)
        assert decision.verdict == mic_compare.AMBIGUOUS
        assert decision.source == 0


def test_a_new_winner_must_confirm_before_the_stream_switches():
    """A troca real custa três ciclos consecutivos (latch_wins); glitch, não."""
    now = [0.0]
    mic = _two(lambda: now[0])
    zeros = np.zeros(1024, dtype=np.float32)
    loud = lambda seed: _noise(1024, 0.40, seed=seed)
    mic.push(0, loud(1))
    now[0] = 0.001
    first = mic.push(1, zeros)
    assert first.verdict == mic_compare.NEAR and first.source == 0

    mic.push(0, zeros)
    now[0] = 0.006
    pending = mic.push(1, loud(2))
    assert pending.source == 0            # primeira prova: ainda não trocou
    assert pending.verdict == mic_compare.AMBIGUOUS

    mic.push(0, zeros)
    now[0] = 0.011
    second = mic.push(1, loud(3))
    assert second.source == 0             # segunda prova: ainda não trocou
    assert second.verdict == mic_compare.AMBIGUOUS

    mic.push(0, zeros)
    now[0] = 0.016
    switched = mic.push(1, loud(4))
    assert switched.verdict == mic_compare.NEAR
    assert switched.source == 1


# ── quais fontes físicas entram ─────────────────────────────────────────────


def test_physical_sources_skip_monitors_aec_and_unavailable():
    rows = [
        {"name": "alsa_input.fone", "monitor": False, "state": "RUNNING"},
        {"name": "alsa_output.monitor", "monitor": True, "state": "RUNNING"},
        {"name": "jarvis_aec_source", "monitor": False, "state": "RUNNING"},
        {"name": "alsa_input.onboard", "monitor": False, "state": "SUSPENDED"},
        {"name": "alsa_input.gone", "monitor": False, "state": "UNAVAILABLE"},
    ]
    picked = physical_sources(rows, is_aec=mic_route.is_aec_source)
    assert [row["name"] for row in picked] == ["alsa_input.fone",
                                               "alsa_input.onboard"]


def test_channel_count_reads_a_pactl_spec():
    assert channel_count("s16le 2ch 48000Hz") == 2
    assert channel_count("float32le 1ch 32000Hz") == 1
    assert channel_count("") == 0


def test_unavailable_sources_skips_only_the_unplugged_port():
    """SUSPENDED with a usable port stays; "not available" does not."""
    text = """Source #71
	State: SUSPENDED
	Name: alsa_input.pci-onboard.analog-stereo
	Ports:
		analog-input-front-mic: Microfone frontal (type: Mic, priority: 8500, not available)
	Active Port: analog-input-front-mic
	Formats:
		pcm
Source #72
	State: SUSPENDED
	Name: alsa_input.usb-resting.mono-fallback
	Ports:
		analog-input-mic: Microfone (type: Mic, priority: 8700, availability unknown)
	Active Port: analog-input-mic
	Formats:
		pcm
"""
    assert unavailable_sources(text) == {
        "alsa_input.pci-onboard.analog-stereo"}
    assert unavailable_sources("") == set()


# ── ativação da conf, com backup e restauração ──────────────────────────────


class _FakeConf:
    def __init__(self, directory, *, node_names=None, probe_level=0.01,
                 restart_ok=True):
        self.directory = directory
        self.path = mic_route.managed_path(directory)
        self.writes = []
        self.restarts = 0
        self.node_names = list(node_names or [])
        self.probe_level = probe_level
        self.restart_ok = restart_ok
        self.probes = []

    def write(self, names, *, directory=None, rate=None, channels=None,
              short=None):
        self.writes.append(list(names))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(mic_route.render_managed_conf(
            list(names), rate=rate, channels=channels, short=short),
            encoding="utf-8")
        return {"path": str(self.path), "changed": True,
                "target": list(names)[0], "targets": list(names)}

    def restart(self):
        self.restarts += 1
        return {"ok": self.restart_ok, "detail": "" if self.restart_ok else "boom"}

    def sources(self):
        return [{"name": name, "monitor": False, "state": "RUNNING"}
                for name in self.node_names]

    def probe(self, name):
        self.probes.append(name)
        return self.probe_level


def test_activation_writes_backup_and_verifies_nodes_and_capture(tmp_path):
    fake = _FakeConf(tmp_path, node_names=mic_route.managed_node_names(2))
    fake.path.write_text("CONF ANTIGA", encoding="utf-8")
    result = activate_filter_conf(
        ["alsa_input.fone", "alsa_input.webcam"], directory=tmp_path,
        rates=[48000, 32000], channels=[1, 2], write=fake.write,
        restart=fake.restart, list_sources=fake.sources, probe=fake.probe)
    assert result["ok"], result
    assert fake.restarts == 1
    assert len(list(tmp_path.glob("*.backup-*"))) == 1
    backup = next(tmp_path.glob("*.backup-*"))
    assert backup.read_text(encoding="utf-8") == "CONF ANTIGA"
    assert fake.probes == mic_route.managed_node_names(2)
    assert "jarvis_aec_source_2" in fake.path.read_text(encoding="utf-8")


def test_missing_node_restores_the_previous_file_and_restarts(tmp_path):
    fake = _FakeConf(tmp_path, node_names=["jarvis_aec_source"])
    fake.path.write_text("CONF ANTIGA", encoding="utf-8")
    result = activate_filter_conf(
        ["alsa_input.fone", "alsa_input.webcam"], directory=tmp_path,
        write=fake.write, restart=fake.restart, list_sources=fake.sources)
    assert not result["ok"]
    assert result["failed"] == "nodes"
    assert result["restored"] is True
    assert fake.path.read_text(encoding="utf-8") == "CONF ANTIGA"
    assert fake.restarts == 2          # aplica, falha, restaura


def test_silent_capture_restores_the_previous_file(tmp_path):
    fake = _FakeConf(tmp_path, node_names=mic_route.managed_node_names(2),
                     probe_level=0.0)
    fake.path.write_text("CONF ANTIGA", encoding="utf-8")
    result = activate_filter_conf(
        ["alsa_input.fone", "alsa_input.webcam"], directory=tmp_path,
        write=fake.write, restart=fake.restart, list_sources=fake.sources,
        probe=fake.probe)
    assert not result["ok"]
    assert result["failed"] == "capture"
    assert fake.path.read_text(encoding="utf-8") == "CONF ANTIGA"


def test_restart_failure_restores_and_reports_the_exact_step(tmp_path):
    fake = _FakeConf(tmp_path, node_names=mic_route.managed_node_names(2),
                     restart_ok=False)
    fake.path.write_text("CONF ANTIGA", encoding="utf-8")
    result = activate_filter_conf(
        ["alsa_input.fone", "alsa_input.webcam"], directory=tmp_path,
        write=fake.write, restart=fake.restart)
    assert not result["ok"]
    assert result["failed"] == "restart"
    assert fake.path.read_text(encoding="utf-8") == "CONF ANTIGA"


def test_unchanged_conf_does_not_restart(tmp_path):
    fake = _FakeConf(tmp_path, node_names=mic_route.managed_node_names(1))
    fake.path.write_text(mic_route.render_managed_conf("alsa_input.fone"),
                         encoding="utf-8")

    def noop_write(*_a, **_k):
        return {"path": str(fake.path), "changed": False,
                "target": "alsa_input.fone", "targets": ["alsa_input.fone"]}

    result = activate_filter_conf(["alsa_input.fone"], directory=tmp_path,
                                  write=noop_write, restart=fake.restart)
    assert result["ok"] and not result["changed"]
    assert fake.restarts == 0


def test_fake_speaker_accepts_the_device_keyword():
    speaker = FakeSpeaker()
    match = speaker.identify([0.1, 0.2], device="fone")
    assert match["device"] == "fone"


# ── mute cala a boca (e só a boca) e a boca não anuncia ação ────────────────


def _live_for_speech():
    import main

    obj = object.__new__(main.JarvisLive)
    obj._speech_epoch = 0
    obj._asst_name = "Gino"
    obj._turn_ref = 3
    obj._turn_input_chan = "voz"
    obj._speech_audit = {}
    import threading
    obj._speech_audit_lock = threading.RLock()
    obj._session_log = []

    class _UI:
        def __init__(self):
            self.muted = False
            self.logs = []

        def write_log(self, text):
            self.logs.append(str(text))

    obj.ui = _UI()
    return obj, main


def test_muted_mic_still_allows_speech(monkeypatch):
    # Ordem do dono (07/11/2026): mute da UI = microfone apenas. A fala sai
    # mesmo com o microfone mutado, e nenhum "mutado" trava o synthesis.
    obj, main = _live_for_speech()
    obj.ui.muted = True
    called = []
    monkeypatch.setattr("core.brain.synth_pcm24k",
                        lambda *a, **k: called.append(1) or b"\x00" * 240)
    main.JarvisLive._say_blocking(obj, "Abrindo o Terminal.", label="Gino")
    assert called == [1]
    assert not any("mutado" in line for line in obj.ui.logs)


def test_unmuted_still_speaks(monkeypatch):
    obj, main = _live_for_speech()
    called = []
    monkeypatch.setattr("core.brain.synth_pcm24k",
                        lambda *a, **k: called.append(1) or b"")
    main.JarvisLive._say_blocking(obj, "Bom dia, criador.", label="Gino")
    assert called == [1]


def test_mouth_output_point_refuses_action_announcements():
    obj, main = _live_for_speech()
    said = []
    obj._say_now = lambda text, **kw: said.append(str(text))
    main.JarvisLive._flows_speak(obj, "Abrindo o Terminal.")
    assert said == []
    assert any("boca calada" in line for line in obj.ui.logs)
    main.JarvisLive._flows_speak(obj, "Já te digo, criador.")
    assert said == ["Já te digo, criador."]


# ── registro contínuo no callback (classificador + ledger) ──────────────────


class _FakeClassifier:
    def __init__(self, label="pessoa", confidence=0.8):
        self.label = label
        self.confidence = confidence
        self.pushed = []
        self.resets = 0

    def push(self, chunk, sample_rate=None):
        self.pushed.append((np.asarray(chunk).copy(), sample_rate))
        return type("Reading", (), {"label": self.label,
                                    "confidence": self.confidence})()

    def reset(self):
        self.resets += 1


class _FakeLedger:
    def __init__(self):
        self.pushes = []
        self.flushes = 0
        self.closed = 0

    def push(self, chunk, **kw):
        self.pushes.append((np.asarray(chunk).copy(), dict(kw)))
        return True

    def flush(self, timeout=5.0):
        self.flushes += 1
        return True

    def close(self, timeout=15.0):
        self.closed += 1


class _FakeGate:
    def __init__(self):
        self.feeds = []

    def feed(self, block, preroll_only=False):
        self.feeds.append((np.asarray(block).copy(), bool(preroll_only)))


def _audit_live(muted=False):
    import main

    obj = object.__new__(main.JarvisLive)
    obj._last_block_at = 0.0
    obj._multi_mic_key = ""
    obj._wake_enabled = False
    obj._awake = True
    obj._phone_active = False
    obj._audio_ledger = _FakeLedger()
    obj._audio_audit_states = []
    obj._multi_mic = None

    class _UI:
        def __init__(self):
            self.muted = muted

        def write_log(self, text):
            pass

    obj.ui = _UI()
    return obj, main


def _states(obj, n=2):
    states = [obj._audio_audit_stream(f"mic{i}") for i in range(n)]
    for state in states:
        state["classifier"] = _FakeClassifier()
    obj._audio_audit_states = states
    return states


def test_every_block_of_the_chosen_stream_reaches_the_pipeline():
    """Antes só 1 bloco por ciclo fechado descia: metade da fala se perdia."""
    obj, main = _audit_live()
    mic = MultiMic(["a", "b"])
    states = _states(obj)
    heard = []

    def base(indata, frames, time_info, status):
        heard.append(np.asarray(indata).copy())

    cb0 = main.JarvisLive._multi_mic_callback(
        obj, 0, mic, base, None, {}, audit=states)
    cb1 = main.JarvisLive._multi_mic_callback(
        obj, 1, mic, base, None, {}, audit=states)

    loud = _noise(1024, 0.35, seed=11)
    quiet = np.zeros(1024, dtype=np.int16)
    cb0(loud.astype(np.int16), 1024, None, None)
    assert heard == []                       # ciclo ainda aberto
    cb1(quiet, 1024, None, None)
    assert mic.decision.verdict == mic_compare.NEAR
    assert mic.decision.source == 0
    assert heard == []                       # quem fechou o ciclo foi o mic1

    second = _noise(1024, 0.30, seed=12).astype(np.int16)
    cb0(second, 1024, None, None)
    cb0(second, 1024, None, None)
    cb1(quiet, 1024, None, None)
    assert len(heard) == 2, "todo bloco da fonte escolhida deve descer"
    assert np.array_equal(heard[0], second)
    assert np.array_equal(heard[1], second)


def test_diffuse_verdict_only_reaches_the_preroll_ring():
    obj, main = _audit_live()
    mic = MultiMic(["a", "b"])
    states = _states(obj)
    gate = _FakeGate()
    obj._groq = gate
    heard = []

    def base(indata, frames, time_info, status):
        heard.append(indata)

    cb0 = main.JarvisLive._multi_mic_callback(
        obj, 0, mic, base, None, {}, audit=states)
    cb1 = main.JarvisLive._multi_mic_callback(
        obj, 1, mic, base, None, {}, audit=states)
    tone = (_tone(1024, 0.3) * 32767).astype(np.int16)
    cb0(tone, 1024, None, None)
    cb1(tone.copy(), 1024, None, None)
    assert mic.decision.verdict == mic_compare.DIFFUSE
    cb0(tone, 1024, None, None)
    assert heard == []
    assert gate.feeds and gate.feeds[0][1] is True


def test_asleep_still_feeds_the_wake_detector():
    obj, main = _audit_live()
    obj._wake_enabled = True
    obj._awake = False
    mic = MultiMic(["a", "b"])
    states = _states(obj)
    heard = []

    def base(indata, frames, time_info, status):
        heard.append(indata)

    cb0 = main.JarvisLive._multi_mic_callback(
        obj, 0, mic, base, None, {}, audit=states)
    cb1 = main.JarvisLive._multi_mic_callback(
        obj, 1, mic, base, None, {}, audit=states)
    block = np.full(1024, 500, dtype=np.int16)
    cb0(block, 1024, None, None)
    cb1(block, 1024, None, None)
    cb0(block, 1024, None, None)
    assert len(heard) == 1


def test_ledger_gets_one_push_per_stream_with_its_own_device():
    obj, main = _audit_live()
    mic = MultiMic(["a", "b"])
    states = _states(obj)
    gate = _FakeGate()
    obj._groq = gate

    def base(indata, frames, time_info, status):
        pass

    cb0 = main.JarvisLive._multi_mic_callback(
        obj, 0, mic, base, None, {}, audit=states)
    cb1 = main.JarvisLive._multi_mic_callback(
        obj, 1, mic, base, None, {}, audit=states)
    cb0(np.full(1024, 800, dtype=np.int16), 1024, None, None)
    cb1(np.zeros(1024, dtype=np.int16), 1024, None, None)

    pushes = obj._audio_ledger.pushes
    assert [kw["device_id"] for _, kw in pushes] == ["mic0", "mic1"]
    assert all(kw["sample_rate"] == 16000 for _, kw in pushes)
    assert all(kw["label"] == "pessoa" for _, kw in pushes)
    assert all(kw["confidence"] == 0.8 for _, kw in pushes)
    # O bloco registrado é o de CADA stream, nunca uma soma.
    assert np.all(pushes[0][0] == 800)
    assert np.all(pushes[1][0] == 0)
    for state in states:
        assert len(state["classifier"].pushed) == 1


def test_muted_microphone_records_nothing():
    obj, main = _audit_live(muted=True)
    mic = MultiMic(["a", "b"])
    states = _states(obj)
    called = []
    obj._groq = _FakeGate()

    def base(indata, frames, time_info, status):
        called.append(1)

    cb0 = main.JarvisLive._multi_mic_callback(
        obj, 0, mic, base, None, {}, audit=states)
    cb1 = main.JarvisLive._multi_mic_callback(
        obj, 1, mic, base, None, {}, audit=states)
    cb0(np.full(1024, 900, dtype=np.int16), 1024, None, None)
    cb1(np.zeros(1024, dtype=np.int16), 1024, None, None)
    assert obj._audio_ledger.pushes == []
    assert states[0]["classifier"].pushed == []


def test_single_stream_records_the_raw_block_before_processing():
    obj, main = _audit_live()
    states = _states(obj, 1)
    order = []

    def base(indata, frames, time_info, status):
        order.append("base")

    wrapped = main.JarvisLive._audio_audit_callback(obj, states[0], base)
    original = np.full(1024, 1234, dtype=np.int16)
    wrapped(original, 1024, None, None)
    assert order == ["base"]
    chunk, kw = obj._audio_ledger.pushes[0]
    assert kw["device_id"] == "mic0"
    assert np.array_equal(chunk, original)
    assert np.array_equal(states[0]["classifier"].pushed[0][0], original)


def test_switch_resets_windows_and_flushes_the_ledger():
    obj, main = _audit_live()
    states = _states(obj, 2)
    main.JarvisLive._audio_audit_switch(obj)
    assert [state["classifier"].resets for state in states] == [1, 1]
    assert obj._audio_ledger.flushes == 1


def test_shutdown_closes_the_ledger():
    import asyncio

    obj, main = _audit_live()
    ledger = obj._audio_ledger
    asyncio.run(main.JarvisLive._audio_audit_close(obj))
    assert ledger.closed == 1
    assert obj._audio_ledger is None


def test_missing_ledger_never_breaks_the_callback():
    """Ledger indisponível é sentinela: o callback conta o bloco e segue."""
    obj, main = _audit_live()
    obj._audio_ledger = False               # sentinela: ledger indisponível
    state = obj._audio_audit_stream("mic0")
    obj._audio_audit_feed(state, np.zeros(1024, dtype=np.int16))
    assert state["blocks"] == 1
    assert obj._audio_audit_ledger() is None
