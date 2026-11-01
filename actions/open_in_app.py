"""Abre um arquivo no aplicativo certo: Obsidian, Qoder IDE ou padrão do sistema.

Roteamento em ``app="auto"``:

* ``.md``/``.markdown`` -> Obsidian, via ``core.obsidian.open_path`` (import
  tardio: o módulo do cofre pode não existir em testes ou numa instalação sem
  Obsidian);
* extensões de código listadas em ``CODE_EXTENSIONS`` -> Qoder IDE;
* pastas -> Qoder IDE (abre o projeto inteiro);
* qualquer outro arquivo existente -> aplicativo padrão, via ``xdg-open``.

``app`` explícito ignora a extensão: ``obsidian``, ``qoder`` ou ``system``.
Nada aqui usa shell; o processo roda em segundo plano e nunca bloqueia a
conversa.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

# Extensões que pertencem ao editor de código.
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".yml", ".yaml", ".toml",
    ".rs", ".go", ".java", ".c", ".h", ".cpp", ".cs", ".php", ".rb", ".sh",
    ".sql", ".css", ".html", ".xml", ".dockerfile",
})

# Ordem de preferência do Qoder (novo app unificado, depois o IDE antigo,
# depois o CLI empacotado e, por último, o VS Code do PATH).
QODER_UNIFIED = "/opt/Qoder/qoder"
QODER_IDE = "/usr/share/qoder-ide/qoder-ide"
QODER_CLI = "/usr/share/qoder-ide/bin/code"

_APPS = ("auto", "obsidian", "qoder", "system")


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _qoder_launcher() -> str:
    """Novo app unificado, depois o IDE antigo, o CLI empacotado e ``code``."""
    if _is_executable(QODER_UNIFIED):
        return QODER_UNIFIED
    if _is_executable(QODER_IDE):
        return QODER_IDE
    if _is_executable(QODER_CLI):
        return QODER_CLI
    return shutil.which("code") or "code"


def _spawn(argv: list[str]) -> None:
    """Dispara o aplicativo em segundo plano, sem shell e sem prender a call."""
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kind_for(path: Path) -> str:
    if path.is_dir():
        return "qoder"
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        return "obsidian"
    if suffix in CODE_EXTENSIONS or path.name.lower() == "dockerfile":
        return "qoder"
    return "system"


def _open_obsidian(path: Path) -> str:
    try:
        obsidian = importlib.import_module("core.obsidian")
    except ImportError:
        return (f"Não consegui abrir {path.name} no Obsidian: "
                f"módulo core.obsidian indisponível.")
    try:
        result = obsidian.open_path(str(path))
    except Exception as exc:  # a fala nunca pode quebrar por causa do cofre
        return f"Não consegui abrir {path.name} no Obsidian: {exc}"
    if isinstance(result, str) and result.strip():
        return result
    return f"Abri {path.name} no Obsidian."


def _open_qoder(path: Path) -> str:
    launcher = _qoder_launcher()
    argv = [launcher]
    if os.path.realpath(launcher) == os.path.realpath(QODER_UNIFIED):
        # The unified Qoder desktop accepts files through --open-file; its
        # desktop entry uses exactly this flag.
        argv.extend(["--open-file", str(path)])
    else:
        argv.append(str(path))
    try:
        _spawn(argv)
    except Exception as exc:
        return f"Não consegui abrir {path.name} no Qoder IDE: {exc}"
    return f"Abri {path.name} no Qoder IDE."


def _open_system(path: Path) -> str:
    launcher = "xdg-open"
    try:
        _spawn([launcher, str(path)])
    except Exception as exc:
        return f"Não consegui abrir {path.name} no aplicativo padrão: {exc}"
    return f"Abri {path.name} no aplicativo padrão."


_OPENERS = {
    "obsidian": _open_obsidian,
    "qoder": _open_qoder,
    "system": _open_system,
}


def open_in(parameters, player=None, speak=None, session_memory=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}

    raw = str(p.get("path") or "").strip()
    if not raw:
        return "Informe o caminho do arquivo para abrir."

    app = str(p.get("app") or "auto").strip().casefold()
    if app not in _APPS:
        return f"app inválido: use {', '.join(_APPS)}."

    path = Path(os.path.abspath(os.path.expanduser(raw)))
    if not path.exists():
        return f"Caminho não encontrado: {path}"

    kind = app if app != "auto" else _kind_for(path)
    return _OPENERS[kind](path)


TOOL = {
    "name": "open_in",
    "description": (
        "Abre um arquivo existente no aplicativo certo. Use app='auto' para "
        "planos .md abrirem no Obsidian, código (.py, .js, .ts, .json, .rs, "
        ".go, .sh...) abrir no Qoder IDE e pastas abrirem como projeto no "
        "Qoder. Arquivos de outros tipos vão para o aplicativo padrão "
        "(xdg-open). Passe app='obsidian'/'qoder'/'system' para forçar. "
        "Sempre chame esta ferramenta quando o dono pedir para abrir ou "
        "mostrar um arquivo; nunca diga que abriu sem chamar."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "Caminho absoluto ou com ~ do arquivo/pasta.",
            },
            "app": {
                "type": "STRING",
                "enum": list(_APPS),
                "description": "auto (padrão), obsidian, qoder ou system.",
            },
        },
        "required": ["path"],
    },
    "handler": open_in,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
