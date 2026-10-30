"""
Ask Qoder's models to do something — the assistant's coding hands.

WHAT IT IS FOR
    The conversation runs on a small, fast local model that is good at talking
    and routing, not at writing a module. This action is the delegation: the
    user says "pede pro Qoder revisar esse projeto", "manda o Qoder escrever
    esse script", and this file hands that job to the models behind the Qoder
    proxy (core/qoder.py) — DeepSeek V4 Pro, Qwen 3.6, Kimi K2.6, GLM 5.1 —
    which are the strongest models this machine can reach without a new bill.

THE THREE MODES
    answer   (default) a question about code, an explanation, a snippet. The
             reply is returned as text and read out / shown to the user.
    plan     the same, but asked for as an ordered plan, no file is touched.
    write    explicit opt-in to touching the disk: Qoder is asked for a JSON
             object of whole files, and those are written inside the workspace
             (~/JarvisProjects by default). Paths that would escape the
             workspace, or that point at a binary, are refused.

    Writing is a separate mode on purpose. "Responde o que é isso" and "cria o
    arquivo" should never be one accidental word apart.

WHY NON_BLOCKING
    A strong model on a real task takes tens of seconds. The declaration says
    so, so the assistant can say "já pedi, senhor" and keep the conversation
    alive instead of freezing mid-sentence.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from core import qoder
from core.brain import strip_tool_markup

# Narration arrives in paragraphs; the voice wants one sentence at a time.
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Folders never worth walking when the user did not name the files: their
# contents are either generated or enormous, and both crowd the prompt.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist",
              "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}

_TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".html", ".css", ".scss", ".sh", ".bash", ".sql",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".lua", ".env.example", ".gitignore", ".dockerfile", "dockerfile",
}

_MAX_FILE_CHARS = 20_000      # one file in the prompt
_MAX_CONTEXT_CHARS = 60_000   # the whole context
_MAX_WRITE_BYTES = 300_000    # one file written to disk
_MAX_LISTED_FILES = 120

_ANSWER_SYSTEM = """You are Qoder, the senior software engineer that JARVIS delegates to.
You answer the person who asked JARVIS for help.

Rules:
- Answer in the language the task is written in.
- Be concrete: real code, real commands, real file paths. No filler, no
  restating of the question, no "as an AI".
- When you show code, put it in a fenced block with the language tag.
- If the task cannot be done as asked, say what blocks it and what you would
  do instead. Do not invent APIs, flags or file contents you cannot see.
- If files are included below, they are the current contents — trust them over
  your assumptions about the project.
- You have no file system, no shell and no editor: this message is everything
  you can see. Never say you are about to open, list or inspect a file, and
  never describe a sandbox you cannot reach. If a file you need is not below,
  ask for it by path in one line and answer with what you have."""

_PLAN_SYSTEM = _ANSWER_SYSTEM + """

This request asks for a PLAN, not for code. Answer as a short ordered list of
steps. For each step say which file or command it touches and why. No code
beyond one-line examples."""

_WRITE_SYSTEM = """You are Qoder, applying changes to a real project on disk.
The caller will write exactly the files you return, so they must be complete.

Return ONLY a JSON object — no prose, no markdown fences:
{"summary": "one sentence describing the change",
 "files": [{"path": "relative/path.py", "content": "the complete file text"}]}

Rules:
- "path" is relative to the project root. Never absolute, never containing "..".
- "content" is the COMPLETE new contents of that file, not a diff or a snippet.
- Include every file you touch. Do not include files you are not changing.
- Keep the code consistent with the files shown to you: same imports, same
  style, same names. Do not rewrite what already works.
- If the task needs a file you cannot produce completely, return the summary
  explaining what is missing and an empty "files" list instead of a stub."""


def _log(player, msg: str) -> None:
    print(f"[Qoder] {msg}")
    try:
        if player is not None:
            player.write_log(f"[Qoder] {msg}")
    except Exception:
        pass


# ── context ─────────────────────────────────────────────────────────────────

def _is_text(path: Path) -> bool:
    name = path.name.lower()
    if name in ("dockerfile", "makefile", "procfile"):
        return True
    return path.suffix.lower() in _TEXT_SUFFIXES


def _read_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read {path.name}: {e})"
    if len(text) > _MAX_FILE_CHARS:
        return text[:_MAX_FILE_CHARS] + f"\n… (truncated, {len(text)} chars total)"
    return text


def _context(root: Path, named: list[str]) -> tuple[str, str]:
    """(project listing, file contents) for the prompt. Never raises."""
    listing, contents = [], []
    if not root.exists():
        return ("(the workspace does not exist yet)", "")

    wanted: list[Path] = []
    for name in named:
        p = Path(name)
        wanted.append(p if p.is_absolute() else root / p)

    if wanted:
        for p in wanted:
            if not p.is_file():
                contents.append(f"--- {p} (not found) ---")
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            contents.append(f"--- {rel} ---\n{_read_file(p)}")
        listing_txt = "\n".join(f"- {Path(n)}" for n in named)
        return listing_txt, "\n\n".join(contents)[:_MAX_CONTEXT_CHARS]

    # No files named: give the model the shape of the project, not its bulk.
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if not _is_text(p):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(root)
            listing.append(f"- {rel} ({size} bytes)")
            count += 1
            if count >= _MAX_LISTED_FILES:
                break
        if count >= _MAX_LISTED_FILES:
            break
    if not listing:
        return ("(empty project)", "")
    return ("\n".join(listing), "")


def _mentioned(root: Path, task: str) -> list[str]:
    """Files the task names, found by walking the project once.

    "planeja a mudança em core/brain.py" already says which file matters; making
    the user (or the model) repeat it in `files` would send Qoder the listing
    without the contents and it would then ask for exactly what was asked for.
    Matching is on the path or the bare file name, and short names are skipped
    because "app" or "main" appear in ordinary sentences.
    """
    if not root.is_dir():
        return []
    low = task.lower()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if not _is_text(p):
                continue
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                continue
            stem = p.name.lower()
            if (rel.lower() in low) or (len(stem) >= 6 and stem in low):
                found.append(rel)
                if len(found) >= 6:
                    return found
    return found


def _task_prompt(task: str, mode: str, listing: str, contents: str) -> str:
    parts = [f"TASK:\n{task}"]
    if listing:
        parts.append(f"PROJECT FILES:\n{listing}")
    if contents:
        parts.append(f"FILE CONTENTS:\n{contents}")
    if mode == "write":
        parts.append("Return the JSON object described in your instructions.")
    return "\n\n".join(parts)


# ── writing ─────────────────────────────────────────────────────────────────

def _safe_target(root: Path, rel: str) -> Path | None:
    """Resolve `rel` inside `root`, or None when it would escape it."""
    rel = str(rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or rel.startswith("~"):
        return None
    if any(part == ".." for part in Path(rel).parts):
        return None
    target = (root / rel).resolve()
    root_r = root.resolve()
    if target != root_r and root_r not in target.parents:
        return None
    return target


def _apply_files(root: Path, files) -> tuple[list[str], list[str]]:
    """Write the model's files. Returns (written, refused) — never partially
    silent: everything it declined is named in the answer."""
    written, refused = [], []
    for item in files if isinstance(files, list) else []:
        if not isinstance(item, dict):
            continue
        rel = item.get("path")
        content = item.get("content")
        if not isinstance(content, str):
            refused.append(f"{rel} (no content)")
            continue
        target = _safe_target(root, rel)
        if target is None:
            refused.append(f"{rel} (path outside the workspace)")
            continue
        if target.suffix.lower() not in _TEXT_SUFFIXES and target.name.lower() not in (
                "dockerfile", "makefile"):
            refused.append(f"{rel} (not a text file)")
            continue
        data = content.encode("utf-8")
        if len(data) > _MAX_WRITE_BYTES:
            refused.append(f"{rel} ({len(data)} bytes, over the {_MAX_WRITE_BYTES} limit)")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(str(target.relative_to(root.resolve())))
        except Exception as e:
            refused.append(f"{rel} ({e})")
    return written, refused


# ── the action ──────────────────────────────────────────────────────────────

def qoder_agent(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    p = parameters or {}
    task = str(p.get("task") or "").strip()
    if not task:
        return "Diga o que você quer que o Qoder faça, senhor."
    mode = str(p.get("mode") or "answer").strip().lower()
    if mode in ("editar", "escrever", "aplicar", "arquivos"):
        mode = "write"
    if mode in ("planejar", "plano"):
        mode = "plan"
    if mode in ("responder", "explicar", ""):
        mode = "answer"

    st = qoder.settings()
    root = Path(os.path.expanduser(str(p.get("workspace") or st["workspace"]).strip()
                                  or st["workspace"]))
    model = str(p.get("model") or "").strip() or st["model"]
    named = p.get("files") or []
    if isinstance(named, str):
        named = [n.strip() for n in named.split(",") if n.strip()]
    named = [str(n) for n in named if str(n).strip()]

    _log(player, f"{mode} · {model} · {root}")
    if not root.exists():
        if mode == "write":
            root.mkdir(parents=True, exist_ok=True)
        # A question does not need a project: asking "o que é um proxy reverso"
        # should not fail because the workspace folder was never created.
    if root.is_dir():
        if not named:
            named = _mentioned(root, task)
        listing, contents = _context(root, named)
    else:
        listing, contents = ("(no project attached — the workspace does not exist)", "")

    system = {"plan": _PLAN_SYSTEM, "write": _WRITE_SYSTEM}.get(mode, _ANSWER_SYSTEM)
    prompt = _task_prompt(task, mode, listing, contents)

    # Narration: the CLI streams its own progress lines ("vou olhar o arquivo…",
    # "achei o problema em X") while it works. Speaking them turns 20-40 s of
    # silence into a conversation — which is the whole difference between asking
    # an assistant and waiting for a batch job. Only when the host offers a way
    # to speak outside a turn; a headless call stays silent.
    narrator = getattr(player, "on_say_now", None) if player is not None else None

    def _say(txt: str) -> None:
        txt = strip_tool_markup(str(txt or "")).strip()
        if not txt or not callable(narrator):
            return
        # Long paragraphs get cut into speakable sentences so the voice starts
        # on the first one instead of waiting for the whole block.
        for piece in _SENT_SPLIT.split(txt):
            piece = piece.strip()
            if len(piece) < 3:
                continue
            try:
                narrator(piece[:300])
            except Exception:
                pass

    try:
        if callable(narrator) and mode != "write":
            reply = qoder.ask_stream(prompt, st=st, model=model, system=system,
                                     on_text=_say)
        else:
            reply = qoder.ask(prompt, st=st, model=model, system=system,
                              max_tokens=4096 if mode != "write" else 8192,
                              temperature=0.2)
    except Exception as e:
        return f"O Qoder não respondeu: {e}"

    if not reply.strip():
        return "O Qoder não devolveu nada, senhor."

    if mode != "write":
        _log(player, f"answered ({len(reply)} chars)")
        return reply[:6000]

    data = qoder.first_json_object(reply)
    if not data:
        _log(player, "write: reply was not JSON")
        return ("O Qoder não devolveu o JSON esperado, então nada foi escrito. "
                "O que ele respondeu:\n" + reply[:1500])

    written, refused = _apply_files(root, data.get("files"))
    summary = str(data.get("summary") or "").strip()
    lines = []
    if written:
        lines.append(f"Qoder aplicou {len(written)} arquivo(s) em {root}:")
        lines.extend(f"  • {w}" for w in written)
    else:
        lines.append("Qoder não alterou nenhum arquivo.")
    if refused:
        lines.append("Recusados:")
        lines.extend(f"  • {r}" for r in refused)
    if summary:
        lines.append(f"Resumo: {summary}")
    _log(player, f"write: {len(written)} written, {len(refused)} refused")
    return "\n".join(lines)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────

TOOL = {
    "name": "qoder_agent",
    "description": (
        "ESCALATION / delegate heavy work to Qoder, a much stronger coding "
        "agent. Use this whenever the request is beyond what the assistant's own "
        "tools can do: installing software, changing system configuration that "
        "needs sudo, editing code or config files, writing a script, a multi-step "
        "setup, or anything the user says the assistant 'cannot do' / 'não "
        "consegue' / 'pede pro Qoder' / 'pede pro coder'. Prefer a direct tool "
        "when one clearly covers the request (whatsapp_control, music_control, "
        "open_app, computer_control…); reach for this the moment no direct tool "
        "fits. mode='answer' (default) only replies with an explanation or the "
        "steps/script; mode='plan' replies with ordered steps; mode='write' "
        "WRITES files inside the workspace and must only be used when the user "
        "clearly asked for files to be created or changed. Returns Qoder's reply, "
        "or the list of files written."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "What Qoder should do, in the user's own terms — "
                               "the goal, the files involved and any constraint "
                               "that matters."
            },
            "mode": {
                "type": "STRING",
                "description": "'answer' (explain/review/draft, default), 'plan' "
                               "(ordered steps, no files), or 'write' (create or "
                               "modify files inside the workspace)."
            },
            "files": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Optional paths (relative to the workspace) whose "
                               "current contents Qoder should read. Omit to let it "
                               "see the project's file list instead."
            },
            "workspace": {
                "type": "STRING",
                "description": "Optional project folder. Defaults to "
                               "~/JarvisProjects."
            },
            "model": {
                "type": "STRING",
                "description": "Optional Qoder model id (e.g. DeepSeek-Flash — free, "
                               "1M context; DeepSeek-V4-Pro; Qwen3.8-Max). Defaults "
                               "to the configured one."
            },
        },
        "required": ["task"],
    },
    "handler": qoder_agent,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
