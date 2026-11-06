"""Detecção de atenção por webcam (FASE4-TASK5).

O módulo captura frames da webcam em thread separada, detecta rosto com
Haar Cascade (OpenCV) e estima se o usuário está olhando para a tela.

Abordagem: Opção A — OpenCV + Haar Cascade (leve, rápido).

A detecção de direção do olhar é feita pela posição relativa do rosto
no frame: rosto centralizado = atenção na tela; rosto deslocado ou
ausente = atenção desviada. Não usa modelo de gaze dedicado (pesado
demais para rodar em 1-2 FPS em CPU).

Integração com o orquestrador: o ``AttentionMonitor`` expõe um estado
thread-safe (``is_attentive``) que o orquestrador consulta antes de
despachar notificações ou falar. Quando ``is_attentive`` é False, o
sistema registra o evento no log e pode silenciar notificações.

Uso:

    monitor = AttentionMonitor(enabled=True)
    monitor.start()
    ...
    if monitor.is_attentive:
        # usuário olhando para a tela
    ...
    monitor.stop()
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

log = logging.getLogger(__name__)

# ── Constantes ──────────────────────────────────────────────────────────────

# Haar Cascade frontal do OpenCV (incluído no pacote cv2).
_HAARCASCADE_FILE = "haarcascade_frontalface_default.xml"

# FPS alvo da captura (baixo para não consumir CPU).
DEFAULT_CAPTURE_FPS = 1.5

# Intervalo entre frames (segundos).
DEFAULT_FRAME_INTERVAL = 1.0 / DEFAULT_CAPTURE_FPS

# Proporções do frame para definir "rosto centralizado".
# Se o centro do rosto está dentro de CENTER_ZONE_X/Y do centro do frame,
# consideramos atenção na tela.
CENTER_ZONE_X = 0.35  # 35% da largura a partir do centro
CENTER_ZONE_Y = 0.40  # 40% da altura a partir do centro

# Número de frames consecutivos sem rosto para declarar desatenção.
# Evita falsos negativos (piscar, virar rápido).
LOST_ATTENTION_FRAMES = 3

# Número de frames consecutivos com rosto para declarar atenção.
# Evita falsos positivos (alguém passando atrás).
GAINED_ATTENTION_FRAMES = 2

# Tamanho mínimo do rosto detectado (pixels). Ignora ruído.
MIN_FACE_SIZE = 40

# Resolução de captura (baixa para velocidade).
CAPTURE_WIDTH = 320
CAPTURE_HEIGHT = 240


# ── Protocolos (injeção de dependência) ─────────────────────────────────────

class CameraSource(Protocol):
    """Abstração de câmera para testes sem hardware."""

    def read(self) -> tuple[bool, Any]:
        """Retorna (ok, frame). Frame é np.ndarray ou None."""
        ...

    def release(self) -> None:
        ...


class _RealCamera:
    """Wrapper sobre cv2.VideoCapture para uso em produção."""

    def __init__(self, device: int = 0):
        self._cap = cv2.VideoCapture(device)
        if self._cap.isOpened():
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)

    def read(self) -> tuple[bool, Any]:
        if not self._cap.isOpened():
            return False, None
        return self._cap.read()

    def release(self) -> None:
        if self._cap.isOpened():
            self._cap.release()


# ── Resultado de detecção ───────────────────────────────────────────────────

@dataclass(frozen=True)
class DetectionResult:
    """Resultado de uma detecção em um frame."""
    face_detected: bool
    face_center: tuple[int, int] | None = None
    face_size: int = 0
    looking_at_screen: bool = False
    frame_width: int = 0
    frame_height: int = 0
    process_ms: float = 0.0


@dataclass
class AttentionState:
    """Estado de atenção agregado (thread-safe via lock do monitor)."""
    attentive: bool = False
    last_face_at: float = 0.0
    last_transition_at: float = 0.0
    consecutive_with_face: int = 0
    consecutive_without_face: int = 0
    total_frames: int = 0
    total_faces: int = 0
    last_detection: DetectionResult | None = None


# ── Detector de rosto e atenção ──────────────────────────────────────────────

class FaceAttentionDetector:
    """Detecta rosto e estima atenção a partir de um frame BGR.

    Sem dependência de câmera nem thread: recebe o frame e devolve o
    resultado. Testável isoladamente.
    """

    def __init__(self, *, cascade_path: str | None = None):
        if not _CV2_AVAILABLE:
            raise RuntimeError(
                "opencv-python (cv2) não está instalado. "
                "Instale com: pip install opencv-python")
        path = cascade_path or cv2.data.haarcascades + _HAARCASCADE_FILE
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise RuntimeError(f"Cascade não carregou: {path}")

    def detect(self, frame: np.ndarray) -> DetectionResult:
        """Processa um frame BGR e retorna o resultado de detecção."""
        t0 = time.monotonic()
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)

        faces = self._cascade.detectMultiScale(
            gray,
            scaleFactor=1.15,
            minNeighbors=5,
            minSize=(MIN_FACE_SIZE, MIN_FACE_SIZE),
        )

        elapsed = (time.monotonic() - t0) * 1000.0

        if len(faces) == 0:
            return DetectionResult(
                face_detected=False,
                frame_width=w,
                frame_height=h,
                process_ms=elapsed,
            )

        # Pega o maior rosto (mais provável de ser o usuário).
        sizes = [fw * fh for (_x, _y, fw, fh) in faces]
        idx = sizes.index(max(sizes))
        x, y, fw, fh = faces[idx]

        cx = x + fw // 2
        cy = y + fh // 2

        # Verifica se o rosto está na zona central (atenção na tela).
        center_x = w / 2
        center_y = h / 2
        zone_hw = w * CENTER_ZONE_X
        zone_hh = h * CENTER_ZONE_Y

        looking = (abs(cx - center_x) <= zone_hw
                   and abs(cy - center_y) <= zone_hh)

        return DetectionResult(
            face_detected=True,
            face_center=(cx, cy),
            face_size=max(fw, fh),
            looking_at_screen=looking,
            frame_width=w,
            frame_height=h,
            process_ms=elapsed,
        )


# ── Monitor de atenção (thread de captura) ──────────────────────────────────

class AttentionMonitor:
    """Monitora a atenção do usuário via webcam em thread separada.

    Parâmetros:
        enabled: se False, ``is_attentive`` é sempre True (sistema
                 considera que o usuário está presente).
        device: índice da câmera (0 = padrão).
        fps: frames por segundo da captura (baixo para CPU).
        camera: injeção de câmera para testes (substitui ``device``).
        detector: injeção de detector para testes.
        on_transition: callback chamado quando o estado de atenção muda.
                       Recebe (attentive: bool, timestamp: float).
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        device: int = 0,
        fps: float = DEFAULT_CAPTURE_FPS,
        camera: CameraSource | None = None,
        detector: FaceAttentionDetector | None = None,
        on_transition: Callable[[bool, float], None] | None = None,
    ):
        self._enabled = enabled
        self._device = device
        self._frame_interval = 1.0 / max(0.1, fps)
        self._camera = camera
        self._detector = detector
        self._on_transition = on_transition

        self._state = AttentionState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    # ── propriedades thread-safe ─────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_attentive(self) -> bool:
        """True quando o usuário está olhando para a tela.

        Se o monitor está desabilitado, retorna True (presença assumida).
        """
        if not self._enabled:
            return True
        with self._lock:
            return self._state.attentive

    @property
    def state(self) -> AttentionState:
        with self._lock:
            return AttentionState(**{
                f.name: getattr(self._state, f.name)
                for f in self._state.__dataclass_fields__.values()
            })

    @property
    def last_detection(self) -> DetectionResult | None:
        with self._lock:
            return self._state.last_detection

    # ── ciclo de vida ────────────────────────────────────────────────────

    def start(self) -> None:
        if self._started:
            return
        if not self._enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="webcam-attention",
            daemon=True,
        )
        self._started = True
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False
        if self._camera is not None:
            try:
                self._camera.release()
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._started and not self._stop_event.is_set()

    # ── loop principal ───────────────────────────────────────────────────

    def _run(self) -> None:
        """Thread de captura: lê frames e atualiza o estado de atenção."""
        camera = self._camera
        if camera is None:
            if not _CV2_AVAILABLE:
                log.warning("[Atenção] cv2 indisponível; monitor desligado")
                return
            camera = _RealCamera(self._device)
            self._camera = camera

        detector = self._detector
        if detector is None:
            try:
                detector = FaceAttentionDetector()
                self._detector = detector
            except Exception as exc:
                log.warning("[Atenção] detector falhou: %s; monitor desligado",
                            exc)
                return

        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                ok, frame = camera.read()
                if not ok or frame is None:
                    self._update(None)
                else:
                    result = detector.detect(frame)
                    self._update(result)
            except Exception as exc:
                log.debug("[Atenção] erro no frame: %s", exc)

            # Espera o intervalo restante para manter o FPS alvo.
            elapsed = time.monotonic() - t0
            wait = max(0.0, self._frame_interval - elapsed)
            if wait > 0:
                self._stop_event.wait(wait)

    # ── atualização de estado ────────────────────────────────────────────

    def _update(self, result: DetectionResult | None) -> None:
        """Atualiza o estado de atenção com base no resultado do frame."""
        now = time.monotonic()
        with self._lock:
            self._state.total_frames += 1
            self._state.last_detection = result

            if result is not None and result.face_detected:
                self._state.total_faces += 1
                self._state.last_face_at = now
                self._state.consecutive_without_face = 0
                self._state.consecutive_with_face += 1

                was_attentive = self._state.attentive
                if (result.looking_at_screen
                        and self._state.consecutive_with_face
                        >= GAINED_ATTENTION_FRAMES):
                    self._state.attentive = True
                elif not result.looking_at_screen:
                    self._state.attentive = False

                if was_attentive != self._state.attentive:
                    self._state.last_transition_at = now
                    self._fire_transition(self._state.attentive, now)
            else:
                self._state.consecutive_with_face = 0
                self._state.consecutive_without_face += 1

                was_attentive = self._state.attentive
                if (self._state.consecutive_without_face
                        >= LOST_ATTENTION_FRAMES):
                    self._state.attentive = False

                if was_attentive and not self._state.attentive:
                    self._state.last_transition_at = now
                    self._fire_transition(False, now)

    def _fire_transition(self, attentive: bool, now: float) -> None:
        callback = self._on_transition
        if callback is not None:
            try:
                callback(attentive, now)
            except Exception as exc:
                log.debug("[Atenção] callback falhou: %s", exc)

    # ── integração com orquestrador ──────────────────────────────────────

    def should_notify(self) -> bool:
        """Se o sistema deve enviar notificação ao usuário agora.

        Retorna True quando:
        - o monitor está desabilitado (presença assumida), ou
        - o usuário está atento.
        """
        return self.is_attentive

    def attention_event(self) -> dict[str, Any]:
        """Snapshot para o orquestrador registrar no log de eventos."""
        s = self.state
        det = s.last_detection
        return {
            "attentive": s.attentive,
            "face_detected": det.face_detected if det else False,
            "looking_at_screen": det.looking_at_screen if det else False,
            "last_face_at": s.last_face_at,
            "total_frames": s.total_frames,
            "total_faces": s.total_faces,
            "enabled": self._enabled,
        }


# ── Síntese de frame para teste (sem câmera) ────────────────────────────────

def make_synthetic_frame(
    width: int = CAPTURE_WIDTH,
    height: int = CAPTURE_HEIGHT,
    face_rect: tuple[int, int, int, int] | None = None,
) -> np.ndarray:
    """Cria um frame sintético (cinza) com um retângulo simulando rosto.

    O Haar Cascade não detecta esse frame (é só um retângulo cinza), mas
    o ``FaceAttentionDetector`` pode ser mockado em testes. Esta função
    serve para testes de pipeline que precisam de um np.ndarray válido.
    """
    if not _CV2_AVAILABLE:
        raise RuntimeError("cv2 necessário para frames sintéticos")
    frame = np.full((height, width, 3), 128, dtype=np.uint8)
    if face_rect is not None:
        x, y, w, h = face_rect
        # Desenha um "rosto" mais realista: oval com gradiente.
        cv2.ellipse(frame, (x + w // 2, y + h // 2),
                    (w // 2, h // 2), 0, 0, 360, (180, 150, 130), -1)
        # Olhos
        eye_y = y + h // 3
        cv2.circle(frame, (x + w // 3, eye_y), 4, (60, 40, 30), -1)
        cv2.circle(frame, (x + 2 * w // 3, eye_y), 4, (60, 40, 30), -1)
    return frame


# ── Câmera falsa para testes ────────────────────────────────────────────────

class FakeCamera:
    """Câmera de teste que devolve frames pré-configurados."""

    def __init__(self, frames: list[tuple[bool, Any]] | None = None):
        self._frames = list(frames or [])
        self._index = 0
        self.released = False

    def read(self) -> tuple[bool, Any]:
        if self._index >= len(self._frames):
            return False, None
        frame = self._frames[self._index]
        self._index += 1
        return frame

    def release(self) -> None:
        self.released = True

    def add_frame(self, ok: bool, frame: Any) -> None:
        self._frames.append((ok, frame))
