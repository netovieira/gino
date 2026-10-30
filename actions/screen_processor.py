"""
Screen & webcam capture for JARVIS vision.

Provides the two capture entry points main.py uses — `_capture_screen()` and
`_capture_camera()` — plus their helpers (compression, camera auto-detection,
config access). main.py grabs a frame here on demand, then injects it into the
main Gemini Live session; there is no separate vision session here.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def looks_blank(img_bytes: bytes, mime: str) -> bool:
    """True when the capture is a single flat colour (a failed screenshot).

    A blank frame is what a Wayland capture without portal permission looks
    like: the X root with no native windows. Describing it to the user as "uma
    tela em branco" is technically true and practically useless — the honest
    answer is "o sistema não autorizou a captura".
    """
    if not _PIL:
        return len(img_bytes) < 8_000
    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("L")
        img.thumbnail((64, 64), PIL.Image.BILINEAR)
        px = list(img.getdata())
        if not px:
            return True
        mean = sum(px) / len(px)
        var = sum((p - mean) ** 2 for p in px) / len(px)
        return var < 4.0          # desvio < 2 níveis: cor chapada
    except Exception:
        return False


def _capture_screen_wayland() -> tuple[bytes, str] | None:
    """Full-screen capture through the XDG portal. None = not applicable.

    On GNOME/Wayland `mss` photographs only the XWayland root: native windows
    are missing and the whole thing comes back as an ~11 KB black frame — which
    is why the assistant described "uma tela em branco" while a full desktop was
    in front of the user. GNOME's own Shell.Screenshot is closed to outside
    callers, so the portal (one approval dialog, remembered afterwards) is the
    only honest path. The helper runs on the system python because the venv has
    no PyGObject.
    """
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
        return None
    helper = Path(__file__).resolve().parent.parent / "core" / "portal_shot.py"
    if not helper.exists():
        return None
    tmp = Path(tempfile.gettempdir()) / f"jarvis-shot-{os.getpid()}.png"
    try:
        r = subprocess.run(["/usr/bin/python3", str(helper), str(tmp), "30"],
                           capture_output=True, text=True, timeout=45)
    except Exception as e:
        print(f"[Vision] portal indisponível: {e}")
        return None
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()
        print(f"[Vision] portal falhou ({r.returncode}): "
              f"{detail[-1][:120] if detail else 'sem detalhe'}")
        return None
    try:
        data = tmp.read_bytes()
    except Exception as e:
        print(f"[Vision] portal sem arquivo: {e}")
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    print(f"[Vision] portal: {len(data):,} bytes")
    return _compress(data, "PNG")


def _capture_screen() -> tuple[bytes, str]:

    wayland = _capture_screen_wayland()
    if wayland:
        return wayland

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def _get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


def _capture_camera() -> tuple[bytes, str]:
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    index   = _get_camera_index()
    backend = _cv2_backend()
    cap     = cv2.VideoCapture(index, backend)

    if not cap.isOpened():
        raise RuntimeError(f"Camera index {index} could not be opened.")

    for _ in range(10):
        cap.read()

    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise RuntimeError("Camera returned no frame.")

    if _PIL:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(rgb)
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q)
        return buf.getvalue(), "image/jpeg"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _JPEG_Q])
    return buf.tobytes(), "image/jpeg"
