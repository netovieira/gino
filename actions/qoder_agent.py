"""
Ask Qoder's models to do something — the assistant's coding hands.

WHAT IT IS FOR
    The conversation runs on a small, fast local model that is good at talking
    and routing, not at writing a module. This action is the delegation: the
    user says "pede pro Qoder revisar esse projeto", "manda o Qoder escrever
    esse script", and this file hands that job to the free Qoder models
    (core/qoder.py). The model comes from the free catalog and a paid one is
    replaced automatically, with the switch announced.

THE THREE MODES
    answer   (default) a question about code, an explanation, a snippet. May
             inspect the project with Qoder's own tools, but does not modify it.
    plan     the same, but asked for as an ordered plan, no file is touched.
    write    explicit opt-in to touching the disk — but as a proposal. Qoder
             first returns a plan with the CLI tools disabled; nothing changes,
             and the job waits for the user's explicit approval (qoder_job
             action='approve'). Only then does the write pass run with full
             access inside the workspace.

    Writing is a separate mode on purpose. "Responde o que é isso" e "cria o
    arquivo" should never be one accidental word apart, and a write never
    starts before the user says yes to a plan.

WHY NON_BLOCKING
    The job runs on a thread in core/qoder_jobs.py. This function only starts it
    and returns the acknowledgement, so the assistant keeps listening and
    talking. Progress and questions arrive as events; a question is answered
    with the qoder_job tool.
"""
from __future__ import annotations

import os
from pathlib import Path

from core import qoder

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

_QUESTION_PROTOCOL = """
QUESTION PROTOCOL
If you cannot finish without a decision only the user can make, stop and reply
with ONLY this JSON — no prose, no markdown fences:
{"question": "the decision you need", "options": ["<option>", "<option>"], "recommended": "<the default you advise>"}
Do not guess, and do not continue with an assumption. JARVIS will ask the user
and send the answer back to you in a new turn.
"""

_FULL_ACCESS = """
You are running on the user's machine with FULL ACCESS: your Read/Edit/Bash
tools work inside the project at {root}, and no permission prompt will appear.
Use them when the task needs it. Never say you are about to open a file and then
stop — open it. Never claim you cannot see the project.
"""

_TEXT_ONLY = """
You cannot reach the file system on this path: this prompt is everything you can
see. If a file you need is not below, ask for it by path in one line and answer
with what you have.
"""


def _answer_system(root: str, direct: bool) -> str:
    access = _FULL_ACCESS.format(root=root) if direct else _TEXT_ONLY
    return f"""You are Qoder, the senior software engineer that JARVIS delegates to.
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
{access}
- This is the ANSWER mode: inspect if useful, but do not modify files. Creating
  or changing files belongs to the write mode the user must ask for explicitly.
{_QUESTION_PROTOCOL}"""


def _plan_system(root: str, direct: bool) -> str:
    return _answer_system(root, direct) + """

This request asks for a PLAN, not for code. Answer as a short ordered list of
steps. For each step say which file or command it touches and why. No code
beyond one-line examples. Do not modify anything."""


def _write_system(root: str, direct: bool) -> str:
    if direct:
        return f"""You are Qoder, applying changes to a real project at {root}.
You work with FULL ACCESS: Read/Edit/Bash are available and no permission prompt
will appear. Do not return a JSON dump of files — edit them with your tools.

Rules:
- Inspect what you need first, then make the change.
- Keep the code consistent with the project: same imports, style and names. Do
  not rewrite what already works.
- Touch only the files the task requires.
- Verify when the project allows it (run the relevant test or command).
- End with a short summary (at most 5 sentences): what changed, which files, and
  how you verified it. If something failed, say so plainly.
{_QUESTION_PROTOCOL}"""
    return """You are Qoder, preparing changes to a real project on disk.
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
  explaining what is missing and an empty "files" list instead of a stub.
""" + _QUESTION_PROTOCOL


def _log(player, msg: str) -> None:
    print(f"[Qoder] {msg}")
    try:
        if player is not None:
            player.write_log(f"[Qoder] {msg}")
    except Exception:
        pass


# ── secrets ─────────────────────────────────────────────────────────────────

def _is_sensitive(path: Path) -> bool:
    """Files that must never travel inside a Qoder prompt.

    The workspace is now the JARVIS repository, and the repository has one file
    whose whole purpose is to hold keys. The listing already reveals names, so
    this also keeps them out of it; explicit reads are answered with a note.
    """
    name = path.name.lower()
    if name in {".env", ".qoder-proxy.env", "api_keys.json", "long_term.json",
                "whatsapp_inbox.jsonl"}:
        return True
    if name.startswith(".env."):
        return True
    if name.endswith(".json") and name.startswith(("client_secret", "token", "credentials")):
        return True
    if name.endswith((".db", ".sqlite", ".sqlite3")):
        return True
    low = "/" + str(path).replace("\\", "/").lower().lstrip("/")
    return any(part in low for part in (
        "/config/certs/", "/config/whatsapp_web/",
        "/memory/whatsapp_media/", "/memory/mac/",
    ))


# ── context ─────────────────────────────────────────────────────────────────

def _is_text(path: Path) -> bool:
    name = path.name.lower()
    if _is_sensitive(path):
        return False
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
            if _is_sensitive(p):
                contents.append(f"--- {p.name} (sensitive file, not shared) ---")
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


def _task_prompt(task: str, mode: str, listing: str, contents: str,
                 direct: bool = False) -> str:
    parts = [f"TASK:\n{task}"]
    if listing:
        parts.append(f"PROJECT FILES:\n{listing}")
    if contents:
        parts.append(f"FILE CONTENTS:\n{contents}")
    if mode == "write" and not direct:
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
        if _is_sensitive(target):
            refused.append(f"{rel} (sensitive file)")
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


# ── the job, prepared for the background runner ─────────────────────────────

def _mode(value) -> str:
    mode = str(value or "answer").strip().lower()
    if mode in ("editar", "escrever", "aplicar", "arquivos"):
        return "write"
    if mode in ("planejar", "plano"):
        return "plan"
    if mode in ("responder", "explicar", ""):
        return "answer"
    return mode if mode in ("answer", "plan", "write") else "answer"


def prepare_job(job, mode: str | None = None, readonly: bool = False) -> dict:
    """Everything the runner needs to start one Qoder turn.

    Context building happens here, on the job thread, so the acknowledgement
    the user hears is immediate even when the project listing takes a moment.

    `mode` overrides the job's mode for a single pass (the approval flow runs a
    plan pass first). `readonly` strips the CLI tools so that pass cannot touch
    the disk even if the model decides to: the plan is a proposal, not an edit.
    """
    st = qoder.settings()
    mode = _mode(mode or job.mode)
    root = Path(os.path.expanduser(str(job.workspace or st["workspace"]).strip()
                                  or st["workspace"]))
    model = qoder.free_model(job.model or st["model"])
    named = [str(n) for n in (job.files or []) if str(n).strip()]

    _log(job.player, f"{mode} · {model} · {root}")
    if mode == "write" and not readonly and not root.exists():
        # write needs a real cwd for the CLI's own tools
        root.mkdir(parents=True, exist_ok=True)
    if root.is_dir():
        if not named:
            named = _mentioned(root, job.task)
        listing, contents = _context(root, named)
    else:
        listing, contents = ("(no project attached — the workspace does not exist)", "")

    direct = bool(qoder.find_cli()) and st.get("transport") == "cli"
    if readonly:
        st = dict(st)
        st["tools"] = ""
        direct = False
    build = {"plan": _plan_system, "write": _write_system}.get(mode, _answer_system)
    system = build(str(root), direct)
    prompt = _task_prompt(job.task, mode, listing, contents, direct=direct)

    if job.decisions:
        lines = ["USER DECISIONS (Qoder asked; this is the answer):"]
        for question, decision in job.decisions:
            if question:
                lines.append(f"- Question: {question}")
            lines.append(f"  Answer: {decision}")
        prompt = ("\n".join(lines) + "\n\nContinue the task with that decision.\n\n"
                  + prompt)
    if mode == "write" and getattr(job, "plan", ""):
        prompt = ("APPROVED PLAN (the user approved this; follow it exactly, "
                  "deviating only if a fact in the files makes a step "
                  "impossible — and say so in the summary):\n"
                  + job.plan + "\n\n" + prompt)

    proxy_system = (_write_system(str(root), False)
                    if mode == "write" else system)
    return {"st": st, "root": root, "model": model, "system": system,
            "proxy_system": proxy_system, "prompt": prompt,
            "mode": mode, "direct": direct}


def parse_question(reply: str) -> dict | None:
    """The QUESTION PROTOCOL reply, if Qoder stopped to ask something.

    Only a reply that IS the JSON object counts: a question-like sentence inside
    an answer must never put the job into a waiting state.
    """
    body = qoder.strip_fences(str(reply or ""))
    if not body.startswith("{"):
        return None
    data = qoder.first_json_object(body)
    if not data:
        return None
    question = str(data.get("question") or "").strip()
    if not question:
        return None
    options = data.get("options") or []
    if isinstance(options, str):
        options = [options]
    options = [str(o).strip() for o in options if str(o).strip()][:8]
    return {"question": question,
            "options": options,
            "recommended": str(data.get("recommended") or "").strip()}


def _describe_tool(name: str, args: dict) -> str:
    """One short line a person would say about what the tool is doing."""
    n = str(name or "").lower()
    raw = (args.get("file_path") or args.get("path") or args.get("file")
           or args.get("pattern") or args.get("command") or "")
    label = Path(str(raw)).name if raw else ""
    if n in ("read", "view", "notebookread"):
        return f"lendo {label}" if label else "lendo arquivos"
    if n in ("edit", "write", "multiedit", "notebookedit", "applypatch", "apply_patch"):
        return f"editando {label}" if label else "editando arquivos"
    if n in ("bash", "run", "shell", "execute"):
        return "rodando um comando"
    if n in ("grep", "glob", "search", "find"):
        return "procurando no projeto"
    if n in ("webfetch", "websearch"):
        return "buscando informação"
    if n in ("task", "todo", "todowrite", "todoread"):
        return "organizando o trabalho"
    return "trabalhando no projeto"


def describe_event(ev: dict) -> str:
    """Turn one stream event into the short line the user gets as an update."""
    if not isinstance(ev, dict):
        return ""
    if ev.get("kind") == "tool":
        return _describe_tool(str(ev.get("name") or ""), ev.get("input") or {})
    data = ev.get("data")
    if isinstance(data, dict):
        for key in ("description", "summary", "text", "message", "detail"):
            value = data.get(key)
            if value:
                return str(value)[:120]
        return ""
    if data:
        return str(data)[:120]
    return ""


def apply_proxy_reply(job, reply: str, root: Path, mode: str) -> str | None:
    """Write the files from a proxy (text-only) reply. None when not applicable."""
    if mode != "write":
        return None
    data = qoder.first_json_object(reply)
    if not data:
        _log(job.player, "write: reply was not JSON")
        return ("O Qoder não devolveu o JSON esperado, então nada foi escrito. "
                "O que ele respondeu:\n" + str(reply or "")[:1500])
    written, refused = _apply_files(root, data.get("files"))
    summary = str(data.get("summary") or "").strip()
    lines: list[str] = []
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
    _log(job.player, f"write: {len(written)} written, {len(refused)} refused")
    return "\n".join(lines)


def qoder_agent(parameters: dict, player=None, speak=None, session_memory=None) -> str:
    """Start a Qoder job and return the acknowledgement — never wait for it.

    The job runs on its own thread (core/qoder_jobs.py): the conversation keeps
    going, progress arrives as updates, and a decision Qoder needs is asked to
    the user and answered with the qoder_job tool.
    """
    p = parameters or {}
    task = str(p.get("task") or "").strip()
    if not task:
        return "Diga o que você quer que o Qoder faça, senhor."
    named = p.get("files") or []
    if isinstance(named, str):
        named = [n.strip() for n in named.split(",") if n.strip()]
    named = [str(n) for n in named if str(n).strip()]

    from core import qoder_jobs as jobs
    return jobs.start(task=task, mode=_mode(p.get("mode")),
                      model=str(p.get("model") or "").strip() or None,
                      workspace=str(p.get("workspace") or "").strip() or None,
                      files=named, player=player)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────

TOOL = {
    "name": "qoder_agent",
    "description": (
        "Delegate a task to Qoder, a much stronger coding agent that runs on "
        "this machine with FULL ACCESS. This returns IMMEDIATELY with an "
        "acknowledgement: the job runs in the background, the conversation stays "
        "alive, and progress/questions arrive on their own. Use it for installing "
        "software, system configuration, editing code or config files, scripts, "
        "multi-step setups, or whenever the user says 'pede pro Qoder' / 'não "
        "consegue'. mode='answer' (default) explains or reviews and may inspect "
        "the project but does not modify it; mode='plan' returns ordered steps; "
        "mode='write' proposes a plan first and changes nothing until the user "
        "approves it with qoder_job action='approve' — use it only when the user "
        "clearly asked for files to be created or changed. If Qoder needs a "
        "decision, the assistant asks the user and sends the reply with the "
        "qoder_job tool (action='answer'). Only free models are used. Do NOT use "
        "this for a direct tool that already covers the request (whatsapp_control, "
        "music_control, open_app, computer_control…)."
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
                "description": "'answer' (explain/review, default), 'plan' "
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
                "description": "Optional project folder. Defaults to the JARVIS "
                               "repository."
            },
            "model": {
                "type": "STRING",
                "description": "Optional free model id (from the Qoder catalog; "
                               "work, Qwen3.8-Flash for quick turns). Paid models "
                               "are refused."
            },
        },
        "required": ["task"],
    },
    "handler": qoder_agent,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
