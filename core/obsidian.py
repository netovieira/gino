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

UP-7 (FLUXO TRABALHO, passo 4)
    Besides mirroring, this module is now the single place that records an
    Obsidian opening: every successful open writes ``event=obsidian_opened``
    with the path to ``logs/activity.log``, and ``open_note`` writes a fresh
    action-plan note into the vault and opens it on screen. When the owner's
    Nextcloud sync folder exists, ``sync_to_nextcloud`` copies the note there
    so the phone can open the very same file. Copying into the owner's own
    sync folder is all it does: no share, no public link, no other account —
    the vault stays exclusive to the owner (skills/actions/SKILL.md).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent.parent
VAULT = Path.home() / "Jarvis"
PLANS_DIR = Path.home() / ".qoder" / "plans"
# Pasta de sincronização do dono (cliente Nextcloud). Nada de conta de terceiro.
NEXTCLOUD_DIR = Path.home() / "Nextcloud"
SYNC_FOLDER = "Jarvis"
ACTIVITY_LOG = BASE_DIR / "logs" / "activity.log"
OBSIDIAN_BIN = (
    os.environ.get("JARVIS_OBSIDIAN_BIN")
    or shutil.which("obsidian")
    or ("/snap/bin/obsidian" if Path("/snap/bin/obsidian").exists() else "obsidian")
)

FOLDERS = ("Planos", "Projetos", "Personas", "Indices", "Topicos")

_LOCK = threading.RLock()


# ── helpers ─────────────────────────────────────────────────────────────────

def _vault() -> Path:
    override = os.environ.get("JARVIS_OBSIDIAN_VAULT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(VAULT).expanduser()


def registered_vault() -> str:
    """O cofre que o Obsidian desta máquina tem registrado ("" se não houver).

    ``obsidian://open?path=`` só mostra um arquivo que esteja num cofre que o
    aplicativo conhece. Sem essa leitura, abrir uma nota de um cofre não
    registrado daria "Abrindo..." na cara do dono e nada na tela.
    """
    config = Path.home() / ".config" / "obsidian" / "obsidian.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - sem config, sem opinião
        return ""
    vaults = data.get("vaults") if isinstance(data, dict) else None
    if not isinstance(vaults, dict) or not vaults:
        return ""
    def score(item):
        entry = item[1] if isinstance(item[1], dict) else {}
        return (bool(entry.get("open")), int(entry.get("ts") or 0))
    try:
        best = max(vaults.items(), key=score)
    except Exception:  # noqa: BLE001 - config corrompida não decide nada
        return ""
    entry = best[1] if isinstance(best[1], dict) else {}
    return str(entry.get("path") or "")


def _inside_registered_vault(target: Path) -> bool:
    """True quando não há config do Obsidian (não sabemos) ou o cofre bate."""
    registered = registered_vault()
    if not registered:
        return True
    try:
        return _resolved(target).is_relative_to(_resolved(Path(registered).expanduser()))
    except Exception:  # noqa: BLE001
        return True


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


def _slug(value, limit: int = 80) -> str:
    """Nome de arquivo a partir de um título, preservando o português.

    ``\\w`` é unicode aqui: ``ação`` continua ``ação``. Só o que não cabe num
    nome de arquivo (separador de caminho, controle, símbolo estranho) vira
    ``_``; o espaço é legítimo no Obsidian e fica.
    """
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"[^\w .()—–-]+", "_", text)
    return text.strip(" ._")[:limit] or "nota"


# ── activity.log ────────────────────────────────────────────────────────────

def _log_event(event: str, **fields) -> None:
    """Uma linha ``event=... chave="valor"`` no activity.log. Nunca levanta.

    É a evidência de que a integração aconteceu (UP-7): sem ela, abrir uma
    nota no Obsidian não deixava rastro nenhum para a auditoria.
    """
    try:
        parts = [f"event={event}"]
        for key, value in fields.items():
            if value is None or value == "":
                continue
            text = str(value).replace('"', "'").replace("\n", " ")
            parts.append(f'{key}="{text}"' if " " in text else f"{key}={text}")
        path = Path(ACTIVITY_LOG).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{' '.join(parts)}\n")
            fh.flush()
    except Exception:      # noqa: BLE001 - registrar não pode quebrar a abertura
        pass


# ── Nextcloud (só a conta do dono) ──────────────────────────────────────────

def sync_root() -> Path:
    return Path(NEXTCLOUD_DIR).expanduser()


def sync_available() -> bool:
    """True quando a pasta de sincronização do dono existe neste computador."""
    return sync_root().is_dir()


def sync_to_nextcloud(path) -> Path | None:
    """Copia um arquivo do cofre para ``~/Nextcloud/Jarvis/...``.

    Devolve o destino, ou ``None`` quando não há pasta de sincronização ou a
    cópia falhou. Manter a cópia dentro da conta do dono é exigência do
    contrato: nada aqui compartilha, cria link público ou mexe em ACL.
    """
    source = _resolved(Path(path).expanduser())
    if not source.is_file():
        return None
    root = sync_root()
    if not root.is_dir():
        return None
    try:
        relative = source.relative_to(_resolved(_vault()))
    except ValueError:
        relative = Path(source.name)
    destination = root / SYNC_FOLDER / relative
    try:
        return _atomic_copy(source, destination)
    except OSError as exc:  # noqa: BLE001 - o cofre local continua válido
        print(f"[Obsidian] espelho Nextcloud falhou ({source.name}): {exc}")
        return None


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

def _open_with_obsidian(target: Path, source: str = "open_in") -> str:
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
        _log_event("obsidian_opened", path=target, source=source, via="xdg-open")
        return f"Abrindo {target.name} no Obsidian."
    try:
        subprocess.Popen([str(OBSIDIAN_BIN), str(target)])
        _log_event("obsidian_opened", path=target, source=source, via="binario")
        return f"Abrindo {target.name} no Obsidian."
    except Exception as exc:  # noqa: BLE001
        _log_event("obsidian_open_failed", path=target, source=source,
                   error=str(exc)[:200])
        return (
            f"Não consegui abrir {target.name} no Obsidian ({exc}); "
            f"o arquivo está em {target}."
        )


def open_path(path, source: str = "open_in") -> str:
    """Ensure the file is inside the vault, then open it in Obsidian."""
    file_source = Path(path).expanduser()
    if not file_source.exists():
        return f"Arquivo não encontrado: {file_source.name}"
    file_source = _resolved(file_source)
    if file_source.is_dir():
        return f"A pasta {file_source.name} não pode ser aberta no Obsidian."

    vault = ensure_vault()
    vault_resolved = _resolved(vault)
    plans_resolved = _resolved(_plans())

    if file_source.is_relative_to(plans_resolved):
        target = mirror_plan(file_source)
    elif file_source.is_relative_to(vault_resolved):
        target = file_source
    else:
        folder = "Planos" if file_source.suffix.lower() == ".md" else "Projetos"
        target = _atomic_copy(file_source, vault / folder / file_source.name)
    return _open_with_obsidian(target, source=source)


# ── notas do espelho de execução (UP-7) ─────────────────────────────────────

def write_note(title: str, body: str, folder: str = "Planos") -> Path:
    """Grava uma nota Markdown no cofre e devolve o caminho."""
    name = _slug(title)
    destination = ensure_vault() / folder / f"{name}.md"
    _atomic_write(destination, str(body or ""))
    return destination


def open_note(title: str, body: str, folder: str = "Planos",
              sync: bool = True, source: str = "task_mirror") -> dict:
    """Escreve a nota, espelha no Nextcloud do dono e abre na tela.

    Devolve ``{"ok", "path", "nextcloud", "registered", "message"}``. Falha de
    cofre ou de abertura não levanta: vira ``ok=False`` com a mensagem honesta,
    porque o dono pediu a nota na tela e um ``ImportError`` não pode derrubar
    a fala.

    ``registered`` diz se o cofre que recebemos é o que o Obsidian desta
    máquina tem aberto. Não é cosmético: ``obsidian://open?path=`` de um cofre
    não registrado corre o risco de não mostrar nada, e dizer "Abrindo" nesse
    caso seria mentira. Nada aqui registra cofre, muda config do Obsidian ou
    redefine a rota do vault do dono — isso é decisão dele.
    """
    try:
        note = write_note(title, body, folder=folder)
    except Exception as exc:  # noqa: BLE001 - o cofre pode estar fora do ar
        return {"ok": False, "path": "", "nextcloud": "", "registered": True,
                "message": f"Não consegui escrever a nota no cofre: {exc}"}
    copied = sync_to_nextcloud(note) if sync else None
    message = open_path(note, source=source)
    registered = _inside_registered_vault(note)
    if message.startswith("Abrindo") and not registered:
        message += (f" (atenção: o Obsidian desta máquina tem o cofre "
                    f"{registered_vault()} registrado; o da nota é {_vault()} — "
                    f"registre este cofre no Obsidian ou aponte "
                    f"JARVIS_OBSIDIAN_VAULT para ele)")
    return {"ok": bool(message.startswith("Abrindo")), "path": str(note),
            "nextcloud": str(copied) if copied else "",
            "registered": registered, "message": message}
