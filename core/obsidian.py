"""Obsidian vault mirror: plans, external Markdown, and the memory spider.

WHY THIS EXISTS
    Qoder plans are plain Markdown in ``~/.qoder/plans`` but Obsidian can only
    show files that live inside a vault. This module owns the vault chosen by
    the owner (``~/Jarvis``), mirrors plans into ``Planos/``, copies outside
    Markdown into the vault on demand, writes the memory index into
    ``Indices/`` and opens the result through the ``obsidian://`` URI scheme
    (``xdg-open``), with the desktop binary as fallback.

FAILURE POLICY
    ``mirror_plan`` refuses paths that are neither a Qoder plan nor already in
    the vault (``ValueError``). Copies are atomic and content-aware: an
    unchanged plan is not rewritten, so Obsidian's file watcher stays quiet.
    No Qt and no ``main.py`` imports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

VAULT = Path.home() / "Jarvis"
PLANS_DIR = Path.home() / ".qoder" / "plans"
OBSIDIAN_BIN = (
    os.environ.get("JARVIS_OBSIDIAN_BIN")
    or shutil.which("obsidian")
    or ("/snap/bin/obsidian" if Path("/snap/bin/obsidian").exists() else "obsidian")
)

FOLDERS = ("Planos", "Projetos", "Personas", "Indices", "Topicos")

_LOCK = threading.RLock()


# ── helpers ─────────────────────────────────────────────────────────────────

def _vault() -> Path:
    return Path(VAULT).expanduser()


def _plans() -> Path:
    return Path(PLANS_DIR).expanduser()


def _atomic_write(path: Path, text: str) -> None:
    tmp: Path | None = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_copy(source: Path, destination: Path) -> Path:
    """Copy bytes atomically, skipping the write when content is identical."""
    data = source.read_bytes()
    try:
        if destination.is_file() and destination.read_bytes() == data:
            return destination
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def ensure_vault() -> Path:
    """Create the vault, its folders and a minimal ``.obsidian`` config."""
    with _LOCK:
        vault = _vault()
        vault.mkdir(parents=True, exist_ok=True)
        for name in FOLDERS:
            (vault / name).mkdir(parents=True, exist_ok=True)
        settings = vault / ".obsidian"
        settings.mkdir(parents=True, exist_ok=True)
        app = settings / "app.json"
        if not app.is_file():
            _atomic_write(app, "{}")
        return vault


# ── plans ───────────────────────────────────────────────────────────────────

def mirror_plan(path) -> Path:
    """Copy one Qoder plan into ``Planos/<name>.md`` and return the copy."""
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"Plano não encontrado: {source}")
    source = _resolved(source)
    if not (source.is_relative_to(_resolved(_plans()))
            or source.is_relative_to(_resolved(_vault()))):
        raise ValueError(f"Fora de {_plans()} ou do cofre: {source}")
    destination = ensure_vault() / "Planos" / source.name
    return _atomic_copy(source, destination)


def mirror_plans(source_dir=None) -> list[Path]:
    """Mirror every ``*.md`` plan; return destination paths."""
    source = Path(source_dir).expanduser() if source_dir is not None else _plans()
    if not source.is_dir():
        return []
    destinations: list[Path] = []
    for plan in sorted(source.glob("*.md")):
        try:
            destinations.append(mirror_plan(plan))
        except (ValueError, OSError) as exc:
            print(f"[Obsidian] plano ignorado ({plan.name}): {exc}")
    return destinations


# ── index mirror ────────────────────────────────────────────────────────────

def mirror_index(markdown: str, graph: dict) -> Path:
    """Write ``Indices/Teia.md`` and ``Indices/Grafo.json``; return the MD."""
    with _LOCK:
        indices = ensure_vault() / "Indices"
        teia = indices / "Teia.md"
        grafo = indices / "Grafo.json"
        _atomic_write(teia, str(markdown or ""))
        _atomic_write(
            grafo,
            json.dumps(graph or {}, ensure_ascii=False, indent=2, sort_keys=True),
        )
        return teia


# ── opening paths in Obsidian ───────────────────────────────────────────────

def _open_with_obsidian(target: Path) -> str:
    url = "obsidian://open?path=" + quote(str(target), safe="")
    return_code = 1
    try:
        result = subprocess.run(["xdg-open", url], timeout=5)
        return_code = result if isinstance(result, int) else getattr(result, "returncode", 0)
        if not isinstance(return_code, int):
            # A mocked/unknown runner result still means the call happened.
            return_code = 0
    except Exception:  # noqa: BLE001 - any launcher failure falls back
        return_code = 1

    if return_code == 0:
        return f"Abrindo {target.name} no Obsidian."
    try:
        subprocess.Popen([str(OBSIDIAN_BIN), str(target)])
        return f"Abrindo {target.name} no Obsidian."
    except Exception as exc:  # noqa: BLE001
        return (
            f"Não consegui abrir {target.name} no Obsidian ({exc}); "
            f"o arquivo está em {target}."
        )


def open_path(path) -> str:
    """Ensure the file is inside the vault, then open it in Obsidian."""
    source = Path(path).expanduser()
    if not source.exists():
        return f"Arquivo não encontrado: {source.name}"
    source = _resolved(source)
    if source.is_dir():
        return f"A pasta {source.name} não pode ser aberta no Obsidian."

    vault = ensure_vault()
    vault_resolved = _resolved(vault)
    plans_resolved = _resolved(_plans())

    if source.is_relative_to(plans_resolved):
        target = mirror_plan(source)
    elif source.is_relative_to(vault_resolved):
        target = source
    else:
        folder = "Planos" if source.suffix.lower() == ".md" else "Projetos"
        target = _atomic_copy(source, vault / folder / source.name)
    return _open_with_obsidian(target)
