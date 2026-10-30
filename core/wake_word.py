"""
Local wake-word detection for JARVIS ("Hey Jarvis").

Design goals:
  • ZERO cost when the feature is off — openwakeword is imported ONLY inside
    start()/install helpers, never at module load. If the user never enables
    wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); the actual model inference runs in
    this module's own background thread, so the real-time audio thread and the
    Gemini stream are never slowed.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

openwakeword ships small ONNX models (a few MB each) and runs comfortably on a
CPU. The pretrained wake phrase used here is "Hey Jarvis".
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# Pretrained openwakeword model that listens for "Hey Jarvis".
WAKE_MODEL = "hey_jarvis"
# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000
# openwakeword only predicts on multiples of 80 ms — 1280 samples at 16 kHz.
# The microphone hands over 1024-sample blocks, so the thread regroups them
# instead of feeding an odd-sized window the model was not trained for.
FRAME_SAMPLES = 1280


def _models_dir() -> Path | None:
    """Where openwakeword keeps its .onnx files, or None if it has none."""
    try:
        import openwakeword
        d = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        return d if d.is_dir() else None
    except Exception:
        return None


def _model_paths() -> list[str]:
    """Absolute paths of the wake model files, both extensions."""
    d = _models_dir()
    if d is None:
        return []
    paths = sorted(str(p) for p in d.glob(f"{WAKE_MODEL}*.onnx"))
    if not paths:
        paths = sorted(str(p) for p in d.glob(f"{WAKE_MODEL}*.tflite"))
    return paths


def _load_model():
    """Construct the detector across openwakeword versions.

    The package changed its constructor between 0.4 and 0.6: the old one takes
    `wakeword_model_paths` (and knows nothing about the framework), the new one
    takes `wakeword_models` by phrase name plus `inference_framework`. Trying the
    new form first and falling back on TypeError means a later `pip install -U`
    does not silently break the wake word.
    """
    from openwakeword.model import Model
    try:
        return Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    except TypeError:
        paths = _model_paths()
        if not paths:
            raise
        return Model(wakeword_model_paths=paths)


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        import importlib.util
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True if openwakeword is installed AND its model files are present on disk.

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does NOT
    construct a Model to probe readiness — doing that is slow and, worse, can clash
    with the detector's own Model when it's already running, which intermittently
    returned False and made the UI flicker to 'not downloaded'. Never raises.
    """
    if not is_installed():
        return False
    try:
        import openwakeword
        models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_wake = (any(models_dir.glob(f"{WAKE_MODEL}*.onnx"))
                    or any(models_dir.glob(f"{WAKE_MODEL}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    make sure the wake model is on disk. Returns (ok, message). Never raises —
    every failure is reported through the returned message and the logger.
    """
    _tell = notify or (lambda _msg: None)
    if is_ready():
        # Most installs ship the models inside the package; nothing to download.
        logger("Wake word: already installed and ready.")
        return True, "Wake word already installed and ready."
    try:
        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)…")
            _tell("Wake word: installing openwakeword (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
        if is_ready():
            logger("Wake word: ready.")
            return True, "Wake word installed and ready."

        # Not shipped with this version: ask the package for the model files.
        logger("Wake word: downloading models…")
        _tell("Wake word: downloading models…")
        download = getattr(__import__("openwakeword.utils", fromlist=["utils"]),
                           "download_models", None)
        if download is None:
            return False, ("this openwakeword version ships no download helper and "
                           f"no '{WAKE_MODEL}' model files were found.")
        try:
            download([WAKE_MODEL])
        except TypeError:
            download()            # older signature downloads the default set
        except Exception as e:
            return False, f"model download failed: {e}"

        if not is_ready():
            return False, "installed, but the wake model could not be loaded."
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as e:
        return False, f"setup error: {e}"


class WakeWordDetector:
    """
    Runs the wake model in a dedicated thread. The mic thread calls feed() with
    raw int16 frames; detections invoke on_detect() (called from this thread —
    the callback must marshal to whatever loop/UI it needs).
    """

    # Silence between two accepted detections: the phrase lights up several
    # consecutive windows, and waking JARVIS four times for one "hey jarvis"
    # would also mean four log lines and four session nudges.
    REFRACTORY_S = 2.5

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = threshold
        self._logger    = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify    = notify or (lambda _msg: None)
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._thread: threading.Thread | None = None
        self._running = False
        self._model = None
        self._ready = False

    def start(self) -> bool:
        """Load the model and spawn the inference thread. Returns True on success.
        Safe to call again — a no-op if already running. Never raises."""
        if self._running:
            return True
        try:
            self._model = _load_model()
        except Exception as e:
            self._logger(f"Palavra de ativação: não deu para carregar o modelo — {e}")
            self._notify("Palavra de ativação indisponível — use o botão WAKE NOW.")
            self._model = None
            return False
        self._running = True
        self._ready = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WakeWordThread")
        self._thread.start()
        self._logger("Palavra de ativação: escutando 'Hey Jarvis' (acústica) "
                     "e o nome escolhido (por texto).")
        return True

    def stop(self) -> None:
        self._running = False
        # unblock the thread if it's waiting on the queue
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        self._model = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied and dropped if the queue is backed up."""
        if not self._running:
            return
        try:
            # frame_int16 is a numpy int16 array (possibly 2-D mono) — flatten to 1-D
            data = frame_int16[:, 0].copy() if getattr(frame_int16, "ndim", 1) > 1 else frame_int16.copy()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _loop(self) -> None:
        import numpy as np
        pending = np.zeros(0, dtype=np.int16)
        last_fire = 0.0
        while self._running:
            try:
                frame = self._queue.get()
                if frame is None or not self._running:
                    break
                pending = np.concatenate((pending, np.asarray(frame, dtype=np.int16)))
                # The mic delivers 1024-sample blocks, the model wants 80 ms
                # (1280). Regrouping here keeps the audio callback untouched and
                # the windows the shape the model was trained on.
                while pending.size >= FRAME_SAMPLES:
                    window, pending = pending[:FRAME_SAMPLES], pending[FRAME_SAMPLES:]
                    scores = self._model.predict(window)
                    score = 0.0
                    if isinstance(scores, dict):
                        # match the jarvis model regardless of exact key suffix
                        for k, v in scores.items():
                            if "jarvis" in k.lower():
                                score = max(score, float(v))
                        if score == 0.0 and scores:
                            score = max(float(v) for v in scores.values())
                    if score >= self._threshold:
                        if time.monotonic() - last_fire < self.REFRACTORY_S:
                            # The same phrase crosses the threshold on several
                            # consecutive windows; one "hey jarvis" is one wake.
                            continue
                        last_fire = time.monotonic()
                        # drain any backlog so we don't double-fire on the same utterance
                        self._drain()
                        pending = np.zeros(0, dtype=np.int16)
                        try:
                            self._on_detect()
                        except Exception as e:
                            self._logger(f"Wake word: on_detect error — {e}")
                        break
            except Exception as e:
                self._logger(f"Wake word: inference error — {e}")

    def _drain(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
