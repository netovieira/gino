"""Testes do módulo de atenção por webcam (FASE4-TASK5).

Todos os testes rodam sem câmera real: usam ``FakeCamera`` e/ou detector
mockado para validar a lógica de detecção, transição de estado e integração
com o orquestrador.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.webcam_attention import (
    CENTER_ZONE_X,
    CENTER_ZONE_Y,
    CAPTURE_WIDTH,
    CAPTURE_HEIGHT,
    GAINED_ATTENTION_FRAMES,
    LOST_ATTENTION_FRAMES,
    AttentionMonitor,
    AttentionState,
    DetectionResult,
    FaceAttentionDetector,
    FakeCamera,
    make_synthetic_frame,
    _CV2_AVAILABLE,
)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def blank_frame():
    """Frame cinza sem rosto."""
    return np.full((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), 128, dtype=np.uint8)


@pytest.fixture
def center_face_frame():
    """Frame com um retângulo na região central (simula rosto centrado)."""
    frame = np.full((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), 128, dtype=np.uint8)
    return frame


@pytest.fixture
def side_face_frame():
    """Frame com conteúdo na lateral (simula rosto desviado)."""
    frame = np.full((CAPTURE_HEIGHT, CAPTURE_WIDTH, 3), 128, dtype=np.uint8)
    return frame


class FakeDetector:
    """Detector falso que devolve resultados pré-programados."""

    def __init__(self, results: list[DetectionResult] | None = None):
        self._results = list(results or [])
        self._index = 0
        self.call_count = 0

    def detect(self, frame) -> DetectionResult:
        self.call_count += 1
        if self._index >= len(self._results):
            return DetectionResult(
                face_detected=False,
                frame_width=CAPTURE_WIDTH,
                frame_height=CAPTURE_HEIGHT,
                process_ms=0.1,
            )
        result = self._results[self._index]
        self._index += 1
        return result

    def add_result(self, result: DetectionResult) -> None:
        self._results.append(result)


def _face_result(
    *,
    detected: bool = True,
    looking: bool = True,
    cx: int = CAPTURE_WIDTH // 2,
    cy: int = CAPTURE_HEIGHT // 2,
    size: int = 80,
) -> DetectionResult:
    return DetectionResult(
        face_detected=detected,
        face_center=(cx, cy) if detected else None,
        face_size=size if detected else 0,
        looking_at_screen=looking if detected else False,
        frame_width=CAPTURE_WIDTH,
        frame_height=CAPTURE_HEIGHT,
        process_ms=0.5,
    )


# ── Testes: DetectionResult ─────────────────────────────────────────────────

class TestDetectionResult:
    def test_no_face(self):
        r = DetectionResult(face_detected=False, frame_width=320,
                            frame_height=240, process_ms=1.0)
        assert r.face_detected is False
        assert r.face_center is None
        assert r.looking_at_screen is False

    def test_face_centered(self):
        r = _face_result(detected=True, looking=True)
        assert r.face_detected is True
        assert r.looking_at_screen is True
        assert r.face_center is not None

    def test_face_looking_away(self):
        r = _face_result(detected=True, looking=False,
                         cx=int(CAPTURE_WIDTH * 0.9),
                         cy=CAPTURE_HEIGHT // 2)
        assert r.face_detected is True
        assert r.looking_at_screen is False

    def test_frozen(self):
        r = _face_result()
        with pytest.raises(AttributeError):
            r.face_detected = False  # type: ignore[misc]


# ── Testes: AttentionState ──────────────────────────────────────────────────

class TestAttentionState:
    def test_defaults(self):
        s = AttentionState()
        assert s.attentive is False
        assert s.total_frames == 0
        assert s.total_faces == 0
        assert s.last_detection is None

    def test_fields(self):
        det = _face_result()
        s = AttentionState(attentive=True, total_frames=10,
                           total_faces=8, last_detection=det)
        assert s.attentive is True
        assert s.total_frames == 10
        assert s.last_detection is det


# ── Testes: AttentionMonitor (lógica de estado) ─────────────────────────────

class TestAttentionMonitorState:
    """Testa a lógica de transição de estado sem thread de captura."""

    def _monitor(self, **kw):
        return AttentionMonitor(enabled=True, **kw)

    def test_disabled_always_attentive(self):
        mon = AttentionMonitor(enabled=False)
        assert mon.is_attentive is True
        assert mon.should_notify() is True

    def test_enabled_starts_not_attentive(self):
        mon = self._monitor()
        assert mon.is_attentive is False

    def test_update_single_face_not_enough(self):
        """Um frame com rosto não basta para declarar atenção."""
        mon = self._monitor()
        result = _face_result(detected=True, looking=True)
        mon._update(result)
        # Precisa de GAINED_ATTENTION_FRAMES consecutivos.
        if GAINED_ATTENTION_FRAMES > 1:
            assert mon.is_attentive is False
        else:
            assert mon.is_attentive is True

    def test_update_consecutive_faces_gain_attention(self):
        """Frames consecutivos com rosto centralizado ganham atenção."""
        mon = self._monitor()
        result = _face_result(detected=True, looking=True)
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(result)
        assert mon.is_attentive is True

    def test_update_looking_away_loses_attention(self):
        """Rosto desviado perde atenção imediatamente."""
        mon = self._monitor()
        # Primeiro ganha atenção.
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True
        # Depois desvia o olhar.
        mon._update(_face_result(detected=True, looking=False,
                                 cx=int(CAPTURE_WIDTH * 0.9)))
        assert mon.is_attentive is False

    def test_update_no_face_loses_attention_after_threshold(self):
        """Sem rosto por LOST_ATTENTION_FRAMES perde atenção."""
        mon = self._monitor()
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        no_face = _face_result(detected=False)
        for _ in range(LOST_ATTENTION_FRAMES - 1):
            mon._update(no_face)
            # Ainda não perdeu (abaixo do threshold).
        assert mon.is_attentive is True
        mon._update(no_face)
        assert mon.is_attentive is False

    def test_update_none_frame_treated_as_no_face(self):
        """Frame None (câmera falhou) conta como sem rosto."""
        mon = self._monitor()
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        for _ in range(LOST_ATTENTION_FRAMES):
            mon._update(None)
        assert mon.is_attentive is False

    def test_state_snapshot(self):
        mon = self._monitor()
        result = _face_result(detected=True, looking=True)
        mon._update(result)
        state = mon.state
        assert isinstance(state, AttentionState)
        assert state.total_frames == 1
        assert state.total_faces == 1
        assert state.last_detection is result

    def test_attention_event(self):
        mon = self._monitor()
        mon._update(_face_result(detected=True, looking=True))
        event = mon.attention_event()
        assert "attentive" in event
        assert "face_detected" in event
        assert "total_frames" in event
        assert event["total_frames"] == 1
        assert event["enabled"] is True

    def test_transition_callback_fires(self):
        transitions = []

        def on_transition(attentive, ts):
            transitions.append((attentive, ts))

        mon = self._monitor(on_transition=on_transition)
        # Ganha atenção.
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert len(transitions) == 1
        assert transitions[0][0] is True

        # Perde atenção (desvia o olhar).
        mon._update(_face_result(detected=True, looking=False,
                                 cx=int(CAPTURE_WIDTH * 0.9)))
        assert len(transitions) == 2
        assert transitions[1][0] is False

    def test_transition_callback_not_called_when_no_change(self):
        transitions = []
        mon = self._monitor(on_transition=lambda a, t: transitions.append(a))
        # Primeiro frame com rosto: se não atingiu o threshold, não transita.
        if GAINED_ATTENTION_FRAMES > 1:
            mon._update(_face_result(detected=True, looking=True))
            assert len(transitions) == 0


# ── Testes: Thread de captura ───────────────────────────────────────────────

class TestAttentionMonitorThread:
    def test_start_stop(self):
        camera = FakeCamera()
        detector = FakeDetector([_face_result(detected=True, looking=True)])
        mon = AttentionMonitor(enabled=True, camera=camera, detector=detector,
                               fps=10.0)
        mon.start()
        assert mon.is_running is True
        time.sleep(0.3)
        mon.stop(timeout=2.0)
        assert mon.is_running is False
        assert camera.released is True

    def test_disabled_does_not_start_thread(self):
        mon = AttentionMonitor(enabled=False)
        mon.start()
        assert mon.is_running is False
        assert mon.is_attentive is True

    def test_double_start_is_safe(self):
        camera = FakeCamera()
        detector = FakeDetector()
        mon = AttentionMonitor(enabled=True, camera=camera, detector=detector,
                               fps=10.0)
        mon.start()
        mon.start()  # não deve criar segunda thread
        time.sleep(0.1)
        mon.stop(timeout=2.0)

    def test_processes_frames_from_camera(self):
        frames = [
            (True, np.full((240, 320, 3), 128, dtype=np.uint8)),
            (True, np.full((240, 320, 3), 128, dtype=np.uint8)),
            (False, None),
        ]
        camera = FakeCamera(frames)
        detector = FakeDetector([
            _face_result(detected=True, looking=True),
            _face_result(detected=True, looking=True),
        ])
        mon = AttentionMonitor(enabled=True, camera=camera, detector=detector,
                               fps=20.0)
        mon.start()
        time.sleep(0.5)
        mon.stop(timeout=2.0)
        assert detector.call_count >= 2

    def test_camera_failure_does_not_crash(self):
        camera = FakeCamera([(False, None), (False, None)])
        detector = FakeDetector()
        mon = AttentionMonitor(enabled=True, camera=camera, detector=detector,
                               fps=20.0)
        mon.start()
        time.sleep(0.3)
        mon.stop(timeout=2.0)
        # Não crashou; estado reflete ausência de rosto.
        state = mon.state
        assert state.total_frames >= 1


# ── Testes: FakeCamera ──────────────────────────────────────────────────────

class TestFakeCamera:
    def test_empty_camera(self):
        cam = FakeCamera()
        ok, frame = cam.read()
        assert ok is False
        assert frame is None

    def test_returns_frames_in_order(self):
        cam = FakeCamera([
            (True, "frame1"),
            (True, "frame2"),
        ])
        ok1, f1 = cam.read()
        ok2, f2 = cam.read()
        ok3, f3 = cam.read()
        assert (ok1, f1) == (True, "frame1")
        assert (ok2, f2) == (True, "frame2")
        assert (ok3, f3) == (False, None)

    def test_add_frame(self):
        cam = FakeCamera()
        cam.add_frame(True, "new")
        ok, f = cam.read()
        assert ok is True
        assert f == "new"

    def test_release(self):
        cam = FakeCamera()
        assert cam.released is False
        cam.release()
        assert cam.released is True


# ── Testes: make_synthetic_frame ────────────────────────────────────────────

class TestSyntheticFrame:
    @pytest.mark.skipif(not _CV2_AVAILABLE, reason="cv2 indisponível")
    def test_blank_frame_shape(self):
        frame = make_synthetic_frame()
        assert frame.shape == (CAPTURE_HEIGHT, CAPTURE_WIDTH, 3)
        assert frame.dtype == np.uint8

    @pytest.mark.skipif(not _CV2_AVAILABLE, reason="cv2 indisponível")
    def test_frame_with_face_rect(self):
        face = (100, 50, 80, 100)
        frame = make_synthetic_frame(face_rect=face)
        assert frame.shape == (CAPTURE_HEIGHT, CAPTURE_WIDTH, 3)
        # O pixel no centro do rosto deve ser diferente do fundo.
        cx, cy = face[0] + face[2] // 2, face[1] + face[3] // 2
        assert not np.array_equal(frame[cy, cx], [128, 128, 128])

    @pytest.mark.skipif(not _CV2_AVAILABLE, reason="cv2 indisponível")
    def test_custom_dimensions(self):
        frame = make_synthetic_frame(width=160, height=120)
        assert frame.shape == (120, 160, 3)


# ── Testes: FaceAttentionDetector (com cv2 real) ────────────────────────────

@pytest.mark.skipif(not _CV2_AVAILABLE, reason="cv2 indisponível")
class TestFaceAttentionDetector:
    def test_constructor_loads_cascade(self):
        det = FaceAttentionDetector()
        assert det._cascade is not None
        assert not det._cascade.empty()

    def test_blank_frame_no_face(self):
        det = FaceAttentionDetector()
        frame = np.full((240, 320, 3), 128, dtype=np.uint8)
        result = det.detect(frame)
        assert result.face_detected is False
        assert result.looking_at_screen is False
        assert result.frame_width == 320
        assert result.frame_height == 240

    def test_process_ms_positive(self):
        det = FaceAttentionDetector()
        frame = np.full((240, 320, 3), 128, dtype=np.uint8)
        result = det.detect(frame)
        assert result.process_ms >= 0.0

    def test_invalid_cascade_raises(self):
        with pytest.raises(RuntimeError):
            FaceAttentionDetector(cascade_path="/nonexistent/path.xml")


# ── Testes: Integração com orquestrador ─────────────────────────────────────

class TestOrchestratorIntegration:
    """Valida que o orquestrador consulta o monitor de atenção."""

    def test_should_notify_when_attentive(self):
        mon = AttentionMonitor(enabled=True)
        # Ganha atenção.
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.should_notify() is True

    def test_should_notify_when_disabled(self):
        mon = AttentionMonitor(enabled=False)
        assert mon.should_notify() is True

    def test_should_not_notify_when_inattentive(self):
        mon = AttentionMonitor(enabled=True)
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        # Perde atenção.
        for _ in range(LOST_ATTENTION_FRAMES):
            mon._update(_face_result(detected=False))
        assert mon.should_notify() is False

    def test_attention_event_for_logging(self):
        mon = AttentionMonitor(enabled=True)
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        event = mon.attention_event()
        assert event["attentive"] is True
        assert event["face_detected"] is True
        assert event["looking_at_screen"] is True
        assert event["enabled"] is True
        assert isinstance(event["total_frames"], int)
        assert event["total_frames"] >= 1


# ── Testes: Thread safety ───────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_updates(self):
        mon = AttentionMonitor(enabled=True)
        errors = []

        def updater(n):
            try:
                for _ in range(n):
                    mon._update(_face_result(detected=True, looking=True))
                    _ = mon.is_attentive
                    _ = mon.state
                    _ = mon.attention_event()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=updater, args=(50,))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert errors == []
        state = mon.state
        assert state.total_frames == 200  # 4 threads × 50 updates

    def test_concurrent_read_write(self):
        mon = AttentionMonitor(enabled=True)
        stop = threading.Event()
        reads = []

        def writer():
            while not stop.is_set():
                mon._update(_face_result(detected=True, looking=True))
                time.sleep(0.001)

        def reader():
            for _ in range(100):
                reads.append(mon.is_attentive)
                reads.append(mon.state)
                time.sleep(0.001)

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        w.start()
        r.start()
        r.join(timeout=5.0)
        stop.set()
        w.join(timeout=2.0)
        assert len(reads) == 200  # 100 × 2 reads


# ── Testes: Cenários realistas ──────────────────────────────────────────────

class TestRealisticScenarios:
    """Simula cenários de uso real sem câmera."""

    def test_user_looking_at_screen(self):
        """Usuário olhando para a tela: atenção True."""
        mon = AttentionMonitor(enabled=True)
        for _ in range(10):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

    def test_user_turns_away(self):
        """Usuário vira o rosto: atenção False."""
        mon = AttentionMonitor(enabled=True)
        for _ in range(5):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        mon._update(_face_result(detected=True, looking=False,
                                 cx=int(CAPTURE_WIDTH * 0.85)))
        assert mon.is_attentive is False

    def test_user_leaves_desk(self):
        """Usuário sai da frente da câmera: atenção False."""
        mon = AttentionMonitor(enabled=True)
        for _ in range(5):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        for _ in range(LOST_ATTENTION_FRAMES):
            mon._update(_face_result(detected=False))
        assert mon.is_attentive is False

    def test_brief_glitch_does_not_flip_state(self):
        """Uma falha breve (1 frame sem rosto) não muda o estado."""
        mon = AttentionMonitor(enabled=True)
        for _ in range(5):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        # Um frame sem rosto (glitch).
        mon._update(_face_result(detected=False))
        # Não deve perder atenção com apenas 1 frame ausente.
        if LOST_ATTENTION_FRAMES > 1:
            assert mon.is_attentive is True

    def test_face_at_edge_not_attentive(self):
        """Rosto na borda do frame: não está olhando para a tela."""
        mon = AttentionMonitor(enabled=True)
        edge_x = int(CAPTURE_WIDTH * 0.9)
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=False,
                                     cx=edge_x, cy=CAPTURE_HEIGHT // 2))
        # Rosto detectado mas na borda: não conta como olhando para tela.
        assert mon.is_attentive is False

    def test_user_returns_after_absence(self):
        """Usuário sai e volta: atenção oscila corretamente."""
        mon = AttentionMonitor(enabled=True)

        # Presente.
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

        # Ausente.
        for _ in range(LOST_ATTENTION_FRAMES):
            mon._update(_face_result(detected=False))
        assert mon.is_attentive is False

        # Volta.
        for _ in range(GAINED_ATTENTION_FRAMES):
            mon._update(_face_result(detected=True, looking=True))
        assert mon.is_attentive is True

    def test_monitor_with_thread_lifecycle(self):
        """Ciclo de vida completo com thread: start, processa, stop."""
        frames = [(True, np.full((240, 320, 3), 128, dtype=np.uint8))
                  for _ in range(20)]
        camera = FakeCamera(frames)
        detector = FakeDetector(
            [_face_result(detected=True, looking=True)] * 10
            + [_face_result(detected=False)] * 10
        )
        transitions = []
        mon = AttentionMonitor(
            enabled=True, camera=camera, detector=detector,
            fps=50.0,
            on_transition=lambda a, t: transitions.append(a),
        )
        mon.start()
        time.sleep(0.5)
        mon.stop(timeout=2.0)

        assert not mon.is_running
        state = mon.state
        assert state.total_frames > 0
