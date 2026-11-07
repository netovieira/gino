"""
Vision through Qoder — an image becomes a description the assistant can speak.

WHAT THIS IS
    Phase 2 of cutting out the local AI: the screenshot no longer goes to the
    7B VL model on Ollama. It goes to the Qoder CLI as an ATTACHMENT
    (`qoder -p --attachment shot.png -- "…"`), on the free Qwen3.8-Flash, which
    reads the image itself. The CLI runs with NO tools (`--tools ""`): this turn
    only has to look, never to act on the machine.

WHY A FILE AND NOT BASE64
    The CLI's attachment flag takes a path; that is the channel that was proven
    (the description mentioned the shape, the colour and the text in the image).
    The proxy/chat-completions path has no attachment field, so it is not used
    here — a text model answering from the prompt alone would invent a
    description. `qoder.ask_with_file` raises instead of guessing.

SETTINGS (plugin_config.brain, ⚙ → ECONOMY BRAIN)
    vision_provider     qoder | local | off      (default: qoder)
    vision_qoder_model  free CLI model with vision (default: Qwen3.8-Flash)
    vision_timeout      seconds for one attempt  (default: 120)

    `local` keeps the old Ollama VL backend (core/brain.py) alive as an option;
    `off` disables vision and makes every call site say so honestly. Phase 3
    decides whether the local backend and its model are removed.

FAILURE IS HONEST
    One attempt, one timeout, one error message. A picture that could not be
    described comes back as an exception the caller shows — never as an empty
    string that reads like "the screen is empty".

WHAT COMES IN FROM WHATSAPP
    `describe_file` is the same turn starting from a file on disk instead of
    bytes in memory: a photo the assistant received lands in
    `memory/whatsapp_media/`, the inbox item carries that path
    (`core/whatsapp.py`), and `actions/file_processor.py` hands it here. The
    answer is then about the file that arrived — never about what a text model
    would guess from the caption.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

from core import qoder

PROVIDERS = ("qoder", "local", "off")
DEFAULT_PROVIDER = "qoder"
# Qwen3.8-Flash is on the CLI's free allowlist and reads images natively (proven
# with a real PNG: it named the red rectangle and the text in it).
DEFAULT_MODEL = qoder.FAST_MODEL
DEFAULT_TIMEOUT = 120.0
TIMEOUT_MIN = 10.0
TIMEOUT_MAX = 600.0

# A Retina/5K screenshot is several megabytes and thousands of image tokens for
# no extra detail at this size. 1568 px on the long edge is the usual sweet spot
# for VL models and keeps one vision turn around two seconds.
MAX_EDGE = 1568
_JPEG_Q = 85

_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp"}
_FORMAT_SUFFIX = {"png": ".png", "jpeg": ".jpg", "jpg": ".jpg",
                  "webp": ".webp", "gif": ".gif", "bmp": ".bmp"}
# O caminho inverso, para o arquivo que já está no disco: a foto que chegou pelo
# WhatsApp vem com a extensão, e é ela que diz o MIME do anexo.
_SUFFIX_MIME = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif", ".bmp": "image/bmp"}
# Um arquivo maior que isto não é uma foto lida em um turno — é o download de
# outra coisa. O limite evita estourar memória antes mesmo de chamar o CLI.
MAX_FILE_BYTES = 25 * 1024 * 1024


def _truthy(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def provider_of(cfg: dict | None) -> str:
    """The configured backend, including the old config shape.

    A config written before this phase only has `vision_local`: `false` there
    meant "no vision", so it still means that. Everything else defaults to
    Qoder; an unknown spelling keeps vision working rather than silently
    turning it off.
    """
    cfg = cfg or {}
    raw = str(cfg.get("vision_provider") or "").strip().lower()
    if raw in PROVIDERS:
        return raw
    if raw:
        return DEFAULT_PROVIDER
    if "vision_local" in cfg and not _truthy(cfg.get("vision_local")):
        return "off"
    return DEFAULT_PROVIDER


def settings(cfg: dict | None) -> dict:
    """The vision keys every caller reads out of `brain.settings()`."""
    cfg = cfg or {}
    provider = provider_of(cfg)
    try:
        timeout = float(cfg.get("vision_timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return {
        "vision_provider": provider,
        "vision_enabled": provider != "off",
        "vision_qoder_model": (str(cfg.get("vision_qoder_model") or "").strip()
                               or DEFAULT_MODEL),
        "vision_timeout": max(TIMEOUT_MIN, min(TIMEOUT_MAX, timeout)),
    }


def enabled(cfg: dict | None) -> bool:
    """True when any vision backend is on (Qoder or the local VL)."""
    return provider_of(cfg) != "off"


def _prepare(image: bytes, mime: str) -> tuple[bytes, str]:
    """Bytes + suffix the CLI will read, shrinking oversized screenshots."""
    suffix = _SUFFIX.get(str(mime or "").strip().lower(), ".png")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image))
        fmt = str(img.format or "").lower()
        suffix = _FORMAT_SUFFIX.get(fmt, suffix)
        if max(img.size) > MAX_EDGE:
            img = img.convert("RGB")
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=True)
            return buf.getvalue(), ".jpg"
    except Exception as e:
        print(f"[Vision] preparo da imagem falhou ({e}); mandando original")
    return image, suffix


def describe(image: bytes, mime: str, question: str, *,
             model: str | None = None, timeout: float | None = None) -> str:
    """Ask Qoder what is in this image. One attempt; raises on failure."""
    if not image:
        raise RuntimeError("nenhuma imagem para descrever")
    prompt = str(question or "").strip() or "Descreva esta imagem em uma frase."
    if not qoder.find_cli():
        raise RuntimeError(
            "qoder CLI não encontrado — a visão em nuvem (--attachment) precisa "
            "dele no PATH")
    data, suffix = _prepare(image, mime)
    fd, path = tempfile.mkstemp(prefix="jarvis-vision-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            text = qoder.ask_with_file(
                prompt, path, model=model or DEFAULT_MODEL,
                max_tokens=600,
                timeout=timeout if timeout is not None else DEFAULT_TIMEOUT)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"visão via Qoder falhou: {e}") from e
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("a visão via Qoder voltou vazia")
    return text


def describe_file(path: str, question: str = "", *,
                  model: str | None = None,
                  timeout: float | None = None) -> str:
    """O que existe neste arquivo de imagem — uma foto que chegou no WhatsApp.

    Mesma tentativa única e mesma honestidade de `describe`: arquivo que não
    está no disco, não é imagem ou é grande demais levanta em vez de virar
    descrição inventada. O MIME vem da extensão porque é ela que o download do
    WhatsApp grava (`_media_filename`), e o `--prepare` ainda confere o formato
    real do arquivo antes de mandar.
    """
    raw = str(path or "").strip()
    if not raw:
        raise RuntimeError("nenhum arquivo de imagem para descrever")
    p = Path(raw).expanduser()
    try:
        is_file = p.is_file()
    except OSError:
        is_file = False
    if not is_file:
        raise RuntimeError(f"não encontrei o arquivo de imagem: {p.name}")
    mime = _SUFFIX_MIME.get(p.suffix.lower(), "")
    if not mime:
        raise RuntimeError(
            f"{p.name} não é uma imagem que eu saiba ler "
            f"({p.suffix or 'sem extensão'})")
    try:
        size = p.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError(
                f"imagem grande demais para um turno ({size // 1048576} MB)")
        image = p.read_bytes()
    except RuntimeError:
        raise
    except OSError as e:
        raise RuntimeError(f"não consegui ler {p.name}: {e}") from e
    return describe(image, mime, question, model=model, timeout=timeout)
