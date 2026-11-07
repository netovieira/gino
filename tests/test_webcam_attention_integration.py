"""UP-5: o gate de atenção plugado no boot e na ponte viva.

O módulo ``core/webcam_attention.py`` já tinha 46 testes próprios (FASE4-
TASK5), mas nenhum processo vivo o instanciava: o orquestrador aceitava
``attention=`` e nunca recebia nada. Esta carteira prova a integração real:

1. a flag opt-in ``config/webcam_attention.json`` (default ``enabled: false``)
   decide se o gate existe de fato;
2. o boot do ``main.py`` instancia o monitor e o injeta na ponte;
3. a boca passa pelo portão ANTES de falar: desatento → nada vai ao ar;
4. o evento ``attention_event`` é gravado em toda consulta, inclusive com o
   gate desligado (é a prova material de que o portão foi consultado);
5. o watchdog de latência (UC-2) continua intocado — suprimir fala não pode
   falsificar nem esconder a métrica ponta-a-ponta.

Nada aqui abre webcam nem escreve em ``logs/activity.log``: câmera e detector
são falsos e os dois canais de log (atenção e latência) são injetados.
"""
from __future__ import annotations

import inspect
import json
import time
from types import SimpleNamespace

import numpy

from core.flows.config import FlowsConfig
from core.flows.live import FlowsRuntime
from core.flows.orchestrator import Orchestrator
from core.webcam_attention import (
    CAPTURE_HEIGHT,
    CAPTURE_WIDTH,
    DEFAULT_CONFIG_PATH,
    ENV_CONFIG_PATH,
    GAINED_ATTENTION_FRAMES,
    LOST_ATTENTION_FRAMES,
    AttentionConfig,
    AttentionMonitor,
    DetectionResult,
    FakeCamera,
    build_monitor,
)

# ── ajudantes ───────────────────────────────────────────────────────────────


def _face_result(*, detected: bool = True, looking: bool = True,
                 cx: int = CAPTURE_WIDTH // 2,
                 cy: int = CAPTURE_HEIGHT // 2) -> DetectionResult:
    return DetectionResult(
        face_detected=detected,
        face_center=(cx, cy) if detected else None,
        face_size=80 if detected else 0,
        looking_at_screen=looking if detected else False,
        frame_width=CAPTURE_WIDTH, frame_height=CAPTURE_HEIGHT,
        process_ms=0.5)


def _monitor_with_attentive_user() -> AttentionMonitor:
    mon = AttentionMonitor(enabled=True)
    for _ in range(GAINED_ATTENTION_FRAMES):
        mon._update(_face_result(looking=True))
    return mon


def _monitor_with_user_away() -> AttentionMonitor:
    mon = _monitor_with_attentive_user()
    mon._update(_face_result(looking=False, cx=int(CAPTURE_WIDTH * 0.95)))
    return mon


def _flows_config(**kw) -> FlowsConfig:
    base = dict(enabled=True, mouth_enabled=False, partial_prewarm=True,
                partial_speech=False, executor_enabled=False,
                memory_enabled=False)
    base.update(kw)
    return FlowsConfig(**base)


class _ScriptedCamera:
    """Câmera que devolve frames ilimitados (o conteúdo é do detector)."""

    def __init__(self):
        self.reads = 0
        self.released = False
        self._frame = numpy.zeros(
            (CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), dtype=numpy.uint8)

    def read(self):
        self.reads += 1
        return True, self._frame

    def release(self):
        self.released = True


class _ScriptedDetector:
    """Detector que segue o roteiro e repete o último resultado para sempre."""

    def __init__(self, results):
        self._results = list(results)
        self._index = 0

    def detect(self, frame):
        i = min(self._index, len(self._results) - 1)
        self._index += 1
        return self._results[i]


class _StubMonitor:
    """Monitor fake que só responde o estado pedido; registra chamadas."""

    def __init__(self, *, attentive=True, enabled=True, stop_ok=True,
                 explode_event=False):
        self.is_attentive = attentive
        self.enabled = enabled
        self.stop_ok = stop_ok
        self.explode_event = explode_event
        self.stopped = False
        self.notify_calls = 0

    def should_notify(self) -> bool:
        self.notify_calls += 1
        return bool(self.is_attentive)

    def attention_event(self) -> dict:
        if self.explode_event:
            raise RuntimeError("monitor mudo")
        return {"attentive": bool(self.is_attentive),
                "face_detected": True, "looking_at_screen": self.is_attentive,
                "last_face_at": 1.0, "total_frames": 12, "total_faces": 9,
                "enabled": bool(self.enabled)}

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True
        if not self.stop_ok:
            raise RuntimeError("câmera presa")


def _runtime(attention, *, spoken=None, lines=None, att=None, lat=None,
             **cfg_kw):
    """Ponte viva com todos os canais de log injetados (nada vai ao disco)."""
    return FlowsRuntime(
        _flows_config(**cfg_kw),
        speak=(spoken if spoken is not None else []).append,
        log=(lines if lines is not None else []).append,
        attention=attention,
        attention_log=(att if att is not None else []).append,
        latency_log=(lat if lat is not None else []).append,
        start_expiry_thread=False, interim_ack_ms=0.0)


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── 1. a flag opt-in ────────────────────────────────────────────────────────


class TestAttentionFlag:
    def test_default_e_desligado(self):
        assert AttentionConfig().enabled is False

    def test_arquivo_do_repo_existe_e_deixa_desligado(self):
        assert DEFAULT_CONFIG_PATH.name == "webcam_attention.json"
        data = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        assert data["enabled"] is False

    def test_arquivo_ausente_vira_default_desligado(self, tmp_path):
        cfg = AttentionConfig.load(tmp_path / "nao-existe.json")
        assert cfg == AttentionConfig(enabled=False)

    def test_arquivo_quebrado_nao_derruba_nada(self, tmp_path):
        path = tmp_path / "webcam_attention.json"
        path.write_text("{ isto não é json", encoding="utf-8")
        assert AttentionConfig.load(path).enabled is False

    def test_flag_ligada_e_lida(self, tmp_path):
        path = tmp_path / "webcam_attention.json"
        path.write_text(json.dumps({"enabled": True, "device": 2,
                                    "fps": 3.0}), encoding="utf-8")
        cfg = AttentionConfig.load(path)
        assert (cfg.enabled, cfg.device, cfg.fps) == (True, 2, 3.0)

    def test_chave_desconhecida_e_ignorada(self, tmp_path):
        path = tmp_path / "webcam_attention.json"
        path.write_text(json.dumps({"enabled": True, "cor": "azul"}),
                        encoding="utf-8")
        assert AttentionConfig.load(path).enabled is True

    def test_valores_sujos_coerem_e_fps_limitado(self):
        cfg = AttentionConfig.from_dict({"enabled": 1, "device": "x",
                                         "fps": "100"})
        assert cfg.enabled is True
        assert cfg.device == 0
        assert cfg.fps <= 10.0

    def test_escopo_de_ambiente_escolhe_o_caminho(self, tmp_path,
                                                  monkeypatch):
        path = tmp_path / "outra.json"
        path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(path))
        assert AttentionConfig.load().enabled is True

    def test_build_monitor_nao_abre_camera_com_flag_off(self):
        camera = FakeCamera([])
        mon = build_monitor(AttentionConfig(enabled=False), camera=camera)
        mon.start()
        try:
            assert mon.is_running is False
            assert mon.is_attentive is True
            assert camera.released is False
        finally:
            mon.stop()

    def test_build_monitor_obedece_a_flag(self):
        mon = build_monitor(AttentionConfig(enabled=True))
        try:
            assert mon.attention_event()["enabled"] is True
        finally:
            mon.stop()


class TestFailOpen:
    """Sem evidência positiva de rosto, o gate não cala o assistente."""

    def test_monitor_ligado_sem_frames_ainda_entrega(self):
        mon = AttentionMonitor(enabled=True)
        assert mon.is_attentive is False
        assert mon.should_notify() is True

    def test_camera_que_nunca_vi_rosto_nao_silencia(self):
        mon = AttentionMonitor(enabled=True)
        for _ in range(LOST_ATTENTION_FRAMES * 3):
            mon._update(_face_result(detected=False))
        assert mon.state.total_faces == 0
        assert mon.is_attentive is False
        assert mon.should_notify() is True

    def test_suppressao_so_depois_de_confirmar_presenca(self):
        mon = _monitor_with_user_away()
        assert mon.state.total_faces > 0
        assert mon.should_notify() is False


# ── 2. o orquestrador consulta antes de entregar ────────────────────────────


class TestOrchestratorGate:
    def test_sem_monitor_e_legado_e_sem_evento(self):
        lines: list[str] = []
        orch = Orchestrator(_flows_config(), log=lines.append)
        assert orch.attention_gate(reason="brief") is True
        assert lines == []

    def test_monitor_e_injetado_pelo_orc(self):
        mon = _monitor_with_attentive_user()
        orch = Orchestrator(_flows_config(), attention=mon)
        assert orch.attention is mon
        assert orch.is_user_attentive() is True
        assert orch.should_deliver_notification() is True

    def test_atento_entrega_e_grava_attention_event(self):
        events: list[dict] = []
        lines: list[str] = []

        def log(msg):
            lines.append(msg)

        orch = Orchestrator(_flows_config(), log=log, on_event=events.append,
                            attention=_monitor_with_attentive_user())
        assert orch.attention_gate(reason="brief") is True
        kinds = [e["type"] for e in events]
        assert "attention_event" in kinds
        event = events[kinds.index("attention_event")]
        assert event["attentive"] is True
        assert event["deliver"] is True
        assert event["reason"] == "brief"
        assert event["enabled"] is True

    def test_desatento_nao_entrega_e_grava_o_motivo(self):
        events: list[dict] = []
        orch = Orchestrator(_flows_config(), on_event=events.append,
                            attention=_monitor_with_user_away())
        assert orch.attention_gate(reason="notificacao") is False
        event = events[-1]
        assert event["type"] == "attention_event"
        assert event["attentive"] is False
        assert event["deliver"] is False
        assert event["reason"] == "notificacao"

    def test_flag_desligada_entrega_mas_continua_gravando(self):
        events: list[dict] = []
        mon = AttentionMonitor(enabled=False)
        orch = Orchestrator(_flows_config(), on_event=events.append,
                            attention=mon)
        assert orch.attention_gate(reason="brief") is True
        assert events[-1]["type"] == "attention_event"
        assert events[-1]["enabled"] is False

    def test_monitor_que_quebra_nao_cala_ninguem(self):
        lines: list[str] = []
        broken = SimpleNamespace(
            is_attentive=False,
            should_notify=lambda: (_ for _ in ()).throw(RuntimeError("pifou")),
            attention_event=lambda: {"attentive": False, "enabled": True})
        orch = Orchestrator(_flows_config(), log=lines.append,
                            attention=broken)
        assert orch.should_deliver_notification() is True
        assert orch.attention_gate(reason="brief") is True

    def test_metricas_expoe_o_estado_de_atencao(self):
        orch = Orchestrator(_flows_config(),
                            attention=_monitor_with_attentive_user())
        assert orch.metrics()["attention"]["attentive"] is True
        assert orch.metrics()["attention"]["enabled"] is True

    def test_evento_de_transicao_vai_pelo_callback(self):
        seen: list[tuple[bool, float]] = []
        mon = AttentionMonitor(
            enabled=True,
            on_transition=lambda attentive, when: seen.append(
                (attentive, when)))
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(looking=True))
        mon._update(_face_result(looking=False, cx=int(CAPTURE_WIDTH * 0.95)))
        assert [s[0] for s in seen] == [True, False]


# ── 3. a boca passa pelo portão antes de falar ──────────────────────────────


class TestLiveGate:
    def test_ponte_injeta_o_monitor_no_orquestrador(self):
        mon = _monitor_with_attentive_user()
        rt = _runtime(mon)
        assert rt.attention is mon
        assert rt.orchestrator.attention is mon
        rt.close()

    def test_desatento_nao_fala_nada(self):
        spoken: list[str] = []
        lines: list[str] = []
        att: list[str] = []
        rt = _runtime(_monitor_with_user_away(), spoken=spoken, lines=lines,
                      att=att)
        plan = rt.start_turn("t1", "reinicia o assistente aí")
        assert plan is not None and plan.active
        time.sleep(0.25)
        assert spoken == []
        assert any("suprimida" in l for l in lines)
        assert any("attention_event" in l and "deliver=0" in l for l in att)
        rt.finish("t1")
        rt.close()

    def test_atento_fala_normalmente(self):
        spoken: list[str] = []
        att: list[str] = []
        rt = _runtime(_monitor_with_attentive_user(), spoken=spoken, att=att)
        rt.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(spoken)), spoken
        rt.finish("t1")
        assert any("attention_event" in l and "deliver=1" in l for l in att)
        rt.close()

    def test_flag_off_muda_comportamento_zero(self):
        spoken: list[str] = []
        lines: list[str] = []
        att: list[str] = []
        mon = AttentionMonitor(enabled=False)
        rt = _runtime(mon, spoken=spoken, lines=lines, att=att)
        rt.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(spoken)), spoken
        assert not any("suprimida" in l for l in lines)
        assert any("enabled=0" in l for l in att)
        rt.finish("t1")
        rt.close()

    def test_sem_monitor_nada_muda_e_nada_e_gravado(self):
        spoken: list[str] = []
        att: list[str] = []
        rt = _runtime(None, spoken=spoken, att=att)
        rt.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(spoken)), spoken
        assert att == []
        rt.finish("t1")
        rt.close()

    def test_supressao_nao_marca_primeira_frase(self):
        rt = _runtime(_monitor_with_user_away())
        rt.start_turn("t1", "reinicia o assistente aí")
        time.sleep(0.2)
        assert rt.turn_metrics("t1")["first_phrase_ms"] is None
        rt.finish("t1")
        rt.close()

    def test_watchdog_de_latencia_intocado(self):
        lat_with: list[str] = []
        lat_off: list[str] = []
        spoken: list[str] = []
        ok = _runtime(_monitor_with_attentive_user(), spoken=spoken,
                      lat=lat_with)
        ok.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(spoken))
        ok.finish("t1")
        ok.close()
        silent = _runtime(_monitor_with_user_away(), lat=lat_off)
        silent.start_turn("t2", "reinicia o assistente aí")
        time.sleep(0.2)
        silent.finish("t2")
        silent.close()
        assert any("latencia e2e" in l for l in lat_with)
        # turno calado: a métrica de boca não pode ser fingida
        assert not any("caminho=boca" in l for l in lat_off)
        assert any("latencia e2e" in l for l in lat_off)

    def test_todas_as_falas_passam_pelo_portao(self):
        """Cada caminho de fala (brief e parcial) é gateado, não só um."""
        spoken: list[str] = []
        att: list[str] = []
        rt = _runtime(_monitor_with_attentive_user(), spoken=spoken, att=att,
                      partial_speech=True, partial_stability_ms=20)
        rt.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(spoken))
        brief_motivos = [self._motivo(l) for l in att]
        assert "brief" in brief_motivos

        rt.start_turn("t2", "oi")
        rt.partial("t2", "oi")
        assert _wait(lambda: len(spoken) > 1)
        assert "partial" in [self._motivo(l) for l in att]
        rt.finish("t1")
        rt.finish("t2")
        rt.close()

    @staticmethod
    def _motivo(line: str) -> str:
        return line.split("motivo=")[1].split()[0] if "motivo=" in line else ""

    def test_notificacao_usa_o_mesmo_portao(self):
        att: list[str] = []
        delivered = _runtime(_monitor_with_attentive_user(),
                             att=att).attention_allows(reason="notificacao")
        suppressed = _runtime(_monitor_with_user_away(),
                              att=att).attention_allows(reason="notificacao")
        assert delivered is True
        assert suppressed is False
        assert any("motivo=notificacao" in l for l in att)

    def test_close_libera_a_camera(self):
        camera = FakeCamera([(True, None)] * 4)
        mon = AttentionMonitor(enabled=False, camera=camera)
        rt = _runtime(mon)
        rt.close()
        assert camera.released is True

    def test_close_tolera_monitor_sem_stop(self):
        class SemStop:
            is_attentive = True

            def should_notify(self):
                return True

            def attention_event(self):
                return {}

        _runtime(SemStop()).close()

    def test_close_engole_erro_do_stop(self):
        _runtime(_StubMonitor(stop_ok=False)).close()

    def test_evento_do_orquestrador_sai_pelo_log_da_ui(self):
        lines: list[str] = []
        att: list[str] = []
        rt = _runtime(_monitor_with_attentive_user(), lines=lines, att=att)
        rt.start_turn("t1", "reinicia o assistente aí")
        assert _wait(lambda: bool(lines))
        assert any("attention_event" in l for l in lines)
        rt.finish("t1")
        rt.close()


class TestThreadIntegration:
    """Câmera de verdade não: o portão é movido pela thread de captura."""

    def _monitor(self, results):
        camera = _ScriptedCamera()
        detector = _ScriptedDetector(results)
        mon = AttentionMonitor(enabled=True, camera=camera,
                               detector=detector, fps=50.0)
        return mon, camera

    def test_thread_presente_depois_ausente_silencia_a_boca(self):
        mon, camera = self._monitor(
            [_face_result(looking=True)] * 6
            + [_face_result(looking=False, cx=int(CAPTURE_WIDTH * 0.95))] * 60)
        mon.start()
        try:
            assert _wait(lambda: mon.is_attentive), mon.state
            spoken: list[str] = []
            att: list[str] = []
            rt = _runtime(mon, spoken=spoken, att=att)
            assert rt.attention is mon
            assert _wait(lambda: mon.should_notify() is False), mon.state
            rt.start_turn("t1", "reinicia o assistente aí")
            time.sleep(0.2)
            assert spoken == []
            assert any("deliver=0" in l for l in att)
            assert camera.reads > 0
            rt.finish("t1")
        finally:
            mon.stop(timeout=2.0)

    def test_thread_para_de_ler_com_a_flag_off(self):
        camera = _ScriptedCamera()
        detector = _ScriptedDetector([_face_result(looking=True)])
        off = AttentionMonitor(enabled=False, camera=camera,
                               detector=detector, fps=50.0)
        off.start()
        try:
            time.sleep(0.1)
            assert camera.reads == 0
        finally:
            off.stop()


# ── 4. o boot pluga o monitor (nenhum outro canto de main.py) ───────────────


class TestBootWiring:
    def _stub(self, **over):
        captured: list[str] = []
        stub = SimpleNamespace(
            ui=SimpleNamespace(write_log=captured.append),
            _flows_attention_transition=lambda attentive, when: None,
            _captured=captured)
        for key, value in over.items():
            setattr(stub, key, value)
        return stub

    def test_main_importa_e_expoe_os_metodos_de_boot(self):
        import main

        assert hasattr(main.JarvisLive, "_flows_attention")
        assert hasattr(main.JarvisLive, "_flows_attention_transition")

    def test_boot_passa_attention_na_construcao_da_ponte(self):
        import main

        src = inspect.getsource(main.JarvisLive._flows_get)
        assert "attention=self._flows_attention()" in src
        assert "persona_routed" not in src
        assert "multi_mic" not in src
        assert "stt_partial" not in src

    def test_boot_e_opt_in_e_nao_abre_camera_com_flag_off(
            self, tmp_path, monkeypatch):
        import main

        path = tmp_path / "webcam_attention.json"
        path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(path))
        started: list[bool] = []
        monkeypatch.setattr(
            AttentionMonitor, "start",
            lambda self: started.append(self.enabled))
        stub = self._stub()
        mon = main.JarvisLive._flows_attention(stub)
        assert mon is not None
        assert mon.attention_event()["enabled"] is False
        assert started == [False]
        assert any("attention_boot enabled=0" in l for l in stub._captured)

    def test_boot_sobe_a_thread_so_com_flag_ligada(self, tmp_path,
                                                   monkeypatch):
        import main

        path = tmp_path / "webcam_attention.json"
        path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        monkeypatch.setenv(ENV_CONFIG_PATH, str(path))
        started: list[bool] = []
        monkeypatch.setattr(
            AttentionMonitor, "start",
            lambda self: started.append(self.enabled))
        mon = main.JarvisLive._flows_attention(self._stub())
        assert started == [True]
        assert mon.attention_event()["enabled"] is True

    def test_boot_reaproveita_o_monitor_instanciado(self, monkeypatch):
        import main

        calls: list[int] = []
        monkeypatch.setattr(
            AttentionConfig, "load",
            classmethod(lambda cls, path=None: calls.append(1) or cls()))
        stub = self._stub(_attention_monitor=AttentionMonitor(enabled=False))
        mon = main.JarvisLive._flows_attention(stub)
        assert mon is stub._attention_monitor
        assert calls == []

    def test_boot_sem_config_quebrado_e_presenca_assumida(self, monkeypatch):
        import main

        monkeypatch.setenv(ENV_CONFIG_PATH,
                           "/caminho/que/nao/existe/webcam_attention.json")
        monkeypatch.setattr(AttentionMonitor, "start", lambda self: None)
        mon = main.JarvisLive._flows_attention(self._stub())
        assert mon.is_attentive is True

    def test_transicao_de_atencao_e_gravada_no_log(self):
        import main

        stub = self._stub()
        main.JarvisLive._flows_attention_transition(
            stub, attentive=False, when=1234.5)
        line = stub._captured[-1]
        assert "attention_event" in line
        assert "attentive=0" in line

    def test_transicao_tolera_ui_muda(self):
        import main

        def explode(_text):
            raise RuntimeError("sem janela")

        stub = self._stub(ui=SimpleNamespace(write_log=explode))
        main.JarvisLive._flows_attention_transition(stub, True, 1.0)


# ── 5. nada disto toca os outros fluxos ─────────────────────────────────────


class TestScopeGuard:
    def test_o_gate_nao_entrou_no_caminho_de_transcricao(self):
        import inspect as _inspect

        from core import groq_stt

        assert "attention" not in _inspect.getsource(groq_stt)

    def test_config_flows_nao_ganha_chave_de_atencao(self):
        src = inspect.getsource(FlowsConfig)
        assert "attention" not in src

    def test_orquestrador_continua_sem_regras_de_atencao_no_turno(self):
        assert "attention" not in inspect.getsource(Orchestrator.begin)

    def test_o_gate_vive_fora_do_watchdog_de_latencia(self):
        src = inspect.getsource(FlowsRuntime._log_latency)
        assert "attention" not in src
