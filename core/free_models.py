"""Free-only model selection for Qoder.

``memory/models.json`` is the catalog the Qoder Live sidecar writes from
``getAvailableModels()``, with each model's price multiplier. This module is
the single reader of that catalog for model choice: a model is usable only
when the catalog says ``isFree`` or ``priceFactor == 0``. Anything else is
paid and is never sent to the CLI.

The owner's rule is explicit: always free. When the configured model becomes
paid, :func:`resolve` switches to another free model and notifies once through
the listeners registered by the UI, instead of waiting for the next request to
burn credits.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CATALOG_PATH = _base_dir() / "memory" / "models.json"

# Known free models, used only when the catalog is missing or has no free
# entry at all. Qwen3.8-Flash is the conversation model; Efficient is the
# catalog's zero-multiplier fallback.
DEFAULT_FREE = "Qwen3.8-Flash"
SAFE_FREE = ("Qwen3.8-Flash", "Efficient")
PREFERENCE = SAFE_FREE

_lock = threading.RLock()
_cache: tuple[float, int, list[dict]] | None = None
_listeners: list = []
_notified: set[tuple[str, str]] = set()


def _read_catalog() -> list[dict]:
    """Catalog entries, cached by mtime/size. Never raises."""
    global _cache
    try:
        stat = CATALOG_PATH.stat()
    except OSError:
        return []
    with _lock:
        if _cache and _cache[0] == stat.st_mtime and _cache[1] == stat.st_size:
            return _cache[2]
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    models = raw.get("models") if isinstance(raw, dict) else raw
    if not isinstance(models, list):
        return []
    clean = [item for item in models if isinstance(item, dict)]
    with _lock:
        _cache = (stat.st_mtime, stat.st_size, clean)
    return clean


def catalog_entry(model: str | None) -> dict | None:
    """The catalog entry whose value or display name matches ``model``."""
    wanted = str(model or "").strip().lower()
    if not wanted:
        return None
    for entry in _read_catalog():
        value = str(entry.get("value") or "").strip().lower()
        display = str(entry.get("displayName") or "").strip().lower()
        if wanted in (value, display):
            return entry
    return None


def is_free(entry: dict | None) -> bool:
    """Free means the catalog says so, or the multiplier is exactly zero."""
    if not entry:
        return False
    if entry.get("isFree") is True:
        return True
    try:
        return float(entry.get("priceFactor")) == 0.0
    except (TypeError, ValueError):
        return False


def cli_name(entry: dict) -> str:
    """The name the Qoder CLI accepts: its display name.

    Catalog values are internal ids (``qfmodel``); ``qoder --list-models``
    prints the display names (``Qwen3.8-Flash``).
    """
    return str(entry.get("displayName") or entry.get("value") or "").strip()


def free_entries() -> list[dict]:
    """Every free catalog entry, preferred models first."""
    entries = [item for item in _read_catalog() if is_free(item)]
    rank = {name.lower(): index for index, name in enumerate(PREFERENCE)}

    def order(item: dict):
        name = cli_name(item).lower()
        return (rank.get(name, len(rank)), name)

    return sorted(entries, key=order)


def free_choices() -> dict[str, str]:
    """``{cli name: description}`` for the settings form."""
    out: dict[str, str] = {}
    for entry in free_entries():
        name = cli_name(entry)
        if name:
            out[name] = str(entry.get("description") or "Gratuito no catálogo Qoder.")
    if not out:
        out = {name: "Gratuito (catálogo Qoder indisponível)." for name in SAFE_FREE}
    return out


def alternative(current: str | None) -> str | None:
    """A free model different from ``current``, or None when there is none."""
    wanted = str(current or "").strip().lower()
    for entry in free_entries():
        name = cli_name(entry)
        if name and name.lower() != wanted:
            return name
    return None


def _announce(previous: str, chosen: str, reason: str) -> None:
    if not previous or previous == chosen:
        return
    key = (previous, chosen)
    with _lock:
        if key in _notified:
            return
        _notified.add(key)
        listeners = list(_listeners)
    line = (f"modelo {previous} deixou de ser gratuito — usando {chosen} "
            f"({reason})")
    print(f"[Modelos] {line}")
    for listener in listeners:
        try:
            listener(previous, chosen, reason)
        except Exception as exc:
            print(f"[Modelos] aviso falhou: {exc!r}")


def resolve(preferred: str | None = None,
            purpose: str = "") -> tuple[str, str | None]:
    """Return ``(free model, previous model)``.

    ``previous`` is non-None only when a switch happened. The chosen name is
    always one the CLI accepts, and never a model the catalog marks as paid.
    """
    entries = _read_catalog()
    wanted = str(preferred or "").strip() or DEFAULT_FREE
    entry = catalog_entry(wanted)
    if entry and is_free(entry):
        return cli_name(entry), None
    if not entries:
        chosen = wanted if wanted in SAFE_FREE else DEFAULT_FREE
        if chosen != wanted:
            _announce(wanted, chosen, "catálogo de modelos indisponível")
        return chosen, (wanted if chosen != wanted else None)
    free = free_entries()
    if not free:
        # Nothing free in the catalog: stay on the known-free default rather
        # than silently spend credits.
        chosen = DEFAULT_FREE
        _announce(wanted, chosen, "nenhum modelo gratuito no catálogo")
        return chosen, (wanted if chosen != wanted else None)
    chosen = cli_name(free[0])
    kind = ("modelo pago no catálogo Qoder" if entry is not None
            else "modelo fora do catálogo Qoder")
    reason = f"{kind} ({purpose})" if purpose else kind
    _announce(wanted, chosen, reason)
    return chosen, (wanted if chosen != wanted else None)


def announce(previous: str, chosen: str, reason: str) -> None:
    """Notify a switch detected outside Python (e.g. the Node sidecar).

    Deduplicated with :func:`resolve`, so a switch announced at startup is not
    repeated when the sidecar reports the same pair.
    """
    _announce(previous, chosen, reason)


def register_listener(fn) -> None:
    """UI hook: called once per (old, new) switch."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def reset() -> None:
    """Test helper: drop the cache and the already-notified set."""
    global _cache
    with _lock:
        _cache = None
        _notified.clear()
        _listeners.clear()
