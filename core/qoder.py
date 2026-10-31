"""
Qoder bridge — the assistant's door to Qoder's models.

WHAT THIS IS
    Qoder is an IDE, and its models are reached either through the CLI the
    person already logged in to, or through a small local server:
    `qoder-proxy` (setup-qoder.sh), which speaks the OpenAI chat-completions
    API. Only FREE models are allowed here (see FREE_MODELS): the assistant
    must never be able to pick a paid model by accident.

    Two things use it:

      core/brain.py     the conversation itself (provider "qoder").
      actions/qoder_agent.py
                        work the user delegates: "ask Qoder to write this",
                        "have Qoder look at my project" — see that file for the
                        answer / plan / write modes.

    And core/gemini.py falls back to it when no Gemini key is configured, so
    every existing side call (screenshot reading, file summaries, the dev
    agent) keeps working in a Gemini-free install.

SETTINGS
    ⚙ → PLUGIN SETTINGS → QODER writes plugin_config.qoder in
    config/api_keys.json:

        transport  cli | proxy                   (cli = the `qoder` binary)
        model      DeepSeek-Flash                (see MODELS below / --list-models)
        thinking   adaptive | enabled | disabled | auto
        reasoning_effort  low | medium | high | max
        context_window    tokens (e.g. 1000000 for the 1M window)
        workspace  the project folder                (default: this repo)

    TRANSPORTS
      cli    calls the `qoder` binary the person already has installed and
             logged in (`qoder -p --output-format json …`). This is the default
             because it reaches the models the CLI advertises — including the
             free DeepSeek-Flash with a 1M context and a real thinking budget —
             without depending on the container being up.
      proxy  the container from setup-qoder.sh, OpenAI-style on 127.0.0.1:3000.
             Kept as a fallback: if the CLI is missing or not logged in, ask()
             falls through to it so a CLI change never silences Qoder.

WHY THE KEY IS NOT THE PAT
    The PAT (pt-...) is the person's Qoder account credential. Nothing here
    needs it: the CLI reads its own login from disk, and the proxy has its own
    PROXY_API_KEY. Both live in exactly one place — .qoder-proxy.env / the CLI
    config — never in this file.
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


import requests

NAMESPACE = "qoder"
CONTAINER = "qoder-proxy"
DEFAULT_BASE_URL = "http://127.0.0.1:3000/v1"
# Model ids for the CLI's --model flag (qoder --list-models).
# FREE ONLY — the assistant may never select a paid model. "Auto" and
# "Efficient" are deliberately absent: Auto can pick a paid model on its own.
FREE_MODELS = ("DeepSeek-Flash", "Qwen3.8-Flash")
HEAVY_MODEL = "DeepSeek-Flash"   # delegated work: deeper reasoning, 1M window
FAST_MODEL = "Qwen3.8-Flash"     # conversation: quick, thinking off, small window
DEFAULT_MODEL = HEAVY_MODEL
CLI_DEFAULT_THINKING = "adaptive"      # auto | adaptive | enabled | disabled
CLI_DEFAULT_EFFORT   = "max"           # low | medium | high | max
CLI_DEFAULT_CONTEXT  = 1_000_000        # 1M-token window
FAST_THINKING = "disabled"
FAST_EFFORT = "low"
FAST_CONTEXT = 32_768
DEFAULT_TIMEOUT = 120

# Only the free models are offered. The id on the left is what goes on the
# wire; the label says what each one is for.
MODELS = {
    "DeepSeek-Flash": "DeepSeek Flash — free, 1M context (trabalho pesado)",
    "Qwen3.8-Flash":  "Qwen 3.8 Flash — free, rápido (conversa)",
}


def free_model(model: str | None) -> str:
    """The requested model if it is on the free allowlist, else the heavy one."""
    m = str(model or "").strip()
    return m if m in FREE_MODELS else HEAVY_MODEL

# The answer to "why is the proxy not there": these two do not get better by
# being retried within the same turn.
_START_TIMEOUT = 25.0


def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
ENV_PATH = BASE_DIR / ".qoder-proxy.env"


def _env_file() -> dict:
    """Values from .qoder-proxy.env. Never raises — missing file is normal."""
    out: dict[str, str] = {}
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


def settings(get_plugin_config=None, profile: str = "work") -> dict:
    """The effective Qoder settings: form values first, env file second.

    `profile="fast"` is the conversation profile: a free fast model with
    thinking off, low effort and a small context — a chat turn should not wait
    on a 1M-token reasoning pass. `profile="work"` is the delegated-job
    profile. Either way the model is clamped to FREE_MODELS and the CLI runs
    with full access (`bypass_permissions`), because JARVIS calls it from a
    non-interactive process where nobody could answer a permission prompt.
    """
    cfg: dict = {}
    try:
        if get_plugin_config is None:
            from memory.config_manager import get_plugin_config as _g
            get_plugin_config = _g
        cfg = get_plugin_config(NAMESPACE) or {}
    except Exception:
        cfg = {}
    env = _env_file()

    base = str(cfg.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URL
    model = free_model(str(cfg.get("model") or "").strip()
                       or str(env.get("MODEL") or "").strip()
                       or HEAVY_MODEL)
    workspace = str(cfg.get("workspace") or "").strip() or str(BASE_DIR)
    try:
        timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    # The CLI is the default transport: it reaches the free models without
    # depending on the container. "proxy" keeps the OpenAI-style path.
    transport = str(cfg.get("transport") or "cli").strip().lower()
    thinking = str(cfg.get("thinking") or CLI_DEFAULT_THINKING).strip().lower()
    effort = str(cfg.get("reasoning_effort") or CLI_DEFAULT_EFFORT).strip().lower()
    try:
        context = int(cfg.get("context_window") or CLI_DEFAULT_CONTEXT)
    except (TypeError, ValueError):
        context = CLI_DEFAULT_CONTEXT
    try:
        think_budget = int(cfg.get("thinking_budget") or 0)
    except (TypeError, ValueError):
        think_budget = 0
    tools_arg = cfg.get("tools")
    tools_arg = "default" if tools_arg is None else str(tools_arg)

    if profile == "fast":
        model = FAST_MODEL
        thinking = FAST_THINKING
        effort = FAST_EFFORT
        context = FAST_CONTEXT
        tools_arg = ""
        think_budget = 0

    # Full access — always. JARVIS runs `qoder -p`: there is no terminal and
    # nobody to approve a tool, so any other mode blocks until the watchdog
    # kills the job. This is not configurable on purpose.
    perm = "bypass_permissions"
    return {
        "base_url": base,
        "api_key": str(cfg.get("api_key") or env.get("PROXY_API_KEY") or "").strip(),
        "model": model,
        "workspace": os.path.expanduser(workspace),
        "auto_start": bool(cfg.get("auto_start", True)),
        "timeout": max(10, min(600, timeout)),
        "transport": "proxy" if transport == "proxy" else "cli",
        "thinking": thinking if thinking in ("auto", "adaptive", "enabled", "disabled") else CLI_DEFAULT_THINKING,
        "reasoning_effort": effort if effort in ("low", "medium", "high", "max") else CLI_DEFAULT_EFFORT,
        "context_window": max(4096, context),
        "thinking_budget": think_budget,
        "tools": tools_arg,
        "permission_mode": perm,
        "profile": "fast" if profile == "fast" else "work",
    }


def _headers(st: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if st.get("api_key"):
        h["Authorization"] = f"Bearer {st['api_key']}"
    return h


def _url(st: dict, path: str) -> str:
    return f"{st['base_url'].rstrip('/')}{path}"


# ── reachability ────────────────────────────────────────────────────────────

def health(st: dict, timeout: float = 3.0) -> bool:
    """Is the proxy answering? The model list is the cheapest proof that it is
    up AND that our key is accepted."""
    try:
        r = requests.get(_url(st, "/models"), headers=_headers(st), timeout=timeout)
        return r.status_code < 400
    except Exception:
        return False


def models(st: dict) -> list[str]:
    """Model ids the proxy advertises. Raises on network/HTTP errors."""
    r = requests.get(_url(st, "/models"), headers=_headers(st), timeout=10)
    r.raise_for_status()
    return [str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id")]


def _docker(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess | None:
    """Run docker with the context this machine actually has.

    The desktop context on this PC points at a socket that does not exist
    (`context "desktop-linux": context not found`), while the system daemon
    answers on `default`. Forcing it here means `docker start` works the same
    way from the app as it does from setup-qoder.sh, with no environment needed
    from whoever launched the app.
    """
    env = dict(os.environ)
    env.setdefault("DOCKER_CONTEXT", "default")
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              env=env, timeout=timeout)
    except FileNotFoundError:
        print("[Qoder] docker is not installed — start the proxy by hand.")
        return None
    except Exception as e:
        print(f"[Qoder] docker {args[0]} failed: {e}")
        return None


def ensure_running(st: dict | None = None, wait: float = _START_TIMEOUT) -> bool:
    """True when the proxy answers. Starts the container if it does not.

    The container is created with --restart unless-stopped, so this is only
    needed after a reboot in which Docker itself was not up, or after the
    container was stopped by hand.
    """
    st = st or settings()
    if health(st):
        return True
    if not st.get("auto_start"):
        return False

    print(f"[Qoder] proxy not answering at {st['base_url']} — starting it…")
    res = _docker("start", CONTAINER)
    missing = res is not None and res.returncode != 0 and "No such container" in (res.stderr or "")
    if missing:
        print(f"[Qoder] container '{CONTAINER}' does not exist — run ./setup-qoder.sh <PAT>")
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.0)
        if health(st):
            print("[Qoder] proxy is up.")
            return True
    print("[Qoder] proxy did not answer in time.")
    return False


# ── asking ──────────────────────────────────────────────────────────────────

def ask_stream(prompt: str, st: dict | None = None,
               on_text=None, on_tool=None, on_progress=None,
               model: str | None = None, system: str | None = None,
               timeout: float | None = None,
               cwd: str | os.PathLike | None = None,
               cancel_event: threading.Event | None = None) -> str:
    """One CLI turn, with the events the caller needs to narrate progress.

    The turn arrives as line-delimited `stream-json`. Assistant text is handed
    to `on_text` (the caller may choose not to speak it), tool use to
    `on_tool`, and progress/summary events to `on_progress` as dicts — that is
    how a background job knows what Qoder is doing without replaying its prose.

    A missing result is an ERROR now, with the real reason (watchdog timeout,
    last stderr lines), never an empty string. `cancel_event` kills the process
    on request; `cwd` makes Qoder's own tools work inside the project.
    """
    cli = _find_cli()
    if not cli:
        raise RuntimeError("qoder CLI não encontrado no PATH nem em ~/.qodersec/bin")
    st = st or settings()
    model = free_model(model or st["model"])
    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
    thinking = st.get("thinking") or CLI_DEFAULT_THINKING
    tools_arg = str(st.get("tools") if st.get("tools") is not None else "default")
    cmd = [cli, "-p", "--output-format", "stream-json",
           "--model", model,
           "--thinking", thinking,
           "--reasoning-effort", st.get("reasoning_effort") or CLI_DEFAULT_EFFORT,
           "--context-window", str(int(st.get("context_window") or CLI_DEFAULT_CONTEXT)),
           "--tools", tools_arg,
           "--max-output-tokens", "8192",
           "--no-session-persistence",
           "--permission-mode", "bypass_permissions",
           "--", full]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1,
                            cwd=str(cwd) if cwd else None)
    limit = float(timeout or st.get("timeout") or DEFAULT_TIMEOUT)
    timed_out = [False]

    def _kill() -> None:
        try:
            proc.kill()
        except Exception:
            pass

    def _on_timeout() -> None:
        timed_out[0] = True
        _kill()

    watchdog = threading.Timer(limit, _on_timeout)
    watchdog.daemon = True
    watchdog.start()

    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    stderr_lines.append(line)
                    if len(stderr_lines) > 20:
                        del stderr_lines[:-20]
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True, name="qoder-stderr").start()

    if cancel_event is not None:
        def _watch_cancel() -> None:
            cancel_event.wait()
            _kill()
        threading.Thread(target=_watch_cancel, daemon=True, name="qoder-cancel").start()

    final = ""
    try:
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            typ = ev.get("type")
            if typ == "assistant":
                for part in (ev.get("message") or {}).get("content") or []:
                    kind = part.get("type")
                    if kind == "text":
                        txt = str(part.get("text") or "").strip()
                        if txt and on_text:
                            on_text(txt)
                    elif kind == "tool_use":
                        name = str(part.get("name") or "")
                        data = part.get("input") or {}
                        if on_tool:
                            try:
                                on_tool(name, data)
                            except Exception:
                                pass
                        if on_progress:
                            try:
                                on_progress({"kind": "tool", "name": name, "input": data})
                            except Exception:
                                pass
            elif typ in ("progress", "tool_use_summary"):
                if on_progress:
                    try:
                        on_progress({"kind": typ,
                                     "data": ev.get("data") or ev.get("summary") or {},
                                     "tool_use_id": ev.get("tool_use_id")})
                    except Exception:
                        pass
            elif typ == "result":
                if _cli_failed(ev):
                    raise _cli_error(ev)
                final = str(ev.get("result") or "")
                if not final.strip():
                    raise RuntimeError("qoder CLI terminou sem resposta")
        try:
            proc.wait(timeout=5)
        except Exception:
            _kill()
    finally:
        watchdog.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass

    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("cancelado")
    if not final:
        detail = " / ".join(stderr_lines[-3:])
        if timed_out[0]:
            raise RuntimeError(f"o Qoder passou de {limit:.0f}s sem terminar")
        raise RuntimeError("o Qoder terminou sem resposta" + (f": {detail}" if detail else ""))
    return final


def ask(prompt: str, st: dict | None = None, model: str | None = None,
        system: str | None = None, max_tokens: int = 1024,
        temperature: float = 0.3, timeout: float | None = None) -> str:
    """One prompt, one answer, as text. Raises RuntimeError with a message a
    person can act on — callers surface that message through the log.

    Transport order: the `qoder` CLI first (the default), the proxy container as
    a fallback. A CLI that is missing or not logged in must not silence Qoder,
    so any CLI failure falls through to the OpenAI-style path.
    """
    st = st or settings()
    if st.get("transport", "cli") == "cli":
        try:
            return _ask_cli(prompt, st, model=model, system=system,
                            max_tokens=max_tokens, timeout=timeout)
        except Exception as e:
            print(f"[Qoder] CLI indisponível ({e}); tentando o proxy")
    return _ask_proxy(prompt, st, model=model, system=system,
                      max_tokens=max_tokens, temperature=temperature,
                      timeout=timeout)


def _find_cli() -> str | None:
    """Path to the `qoder` binary: PATH first, then the known install dir."""
    for name in ("qoder", "qodercli"):
        p = shutil.which(name)
        if p:
            return p
    cand = Path.home() / ".qodersec" / "bin" / "qodercli"
    return str(cand) if cand.exists() else None


def find_cli() -> str | None:
    """Public check: is the CLI reachable? The job runner uses it to decide
    whether write mode means "edit directly" or "return JSON for us to apply"."""
    return _find_cli()


def _cli_error(event: dict) -> RuntimeError:
    """A human-readable error from a CLI result/error event.

    The CLI reports failures as `{"type":"result","subtype":
    "error_during_execution","errors":[...],"error_code":500}` — sometimes with
    no "result" field at all. Returning that stdout as the assistant's answer is
    how a raw JSON blob once got spoken aloud; every caller must raise instead.
    """
    errors = event.get("errors") or []
    if isinstance(errors, str):
        errors = [errors]
    detail = str(errors[0]) if errors else str(event.get("result") or "").strip()
    if not detail:
        detail = str(event.get("subtype") or "erro sem detalhe")
    code = event.get("error_code")
    suffix = f" (código {code})" if code not in (None, "", 0) else ""
    return RuntimeError(f"qoder CLI devolveu erro{suffix}: {detail[:300]}")


def _cli_failed(event: dict) -> bool:
    return bool(event.get("is_error")
                or str(event.get("subtype") or "").startswith("error")
                or event.get("error_code")
                or event.get("errors"))


def _ask_cli(prompt: str, st: dict, model: str | None = None,
             system: str | None = None, max_tokens: int = 1024,
             timeout: float | None = None,
             cwd: str | os.PathLike | None = None) -> str:
    """One non-interactive `qoder -p` turn, returned as text.

    FULL ACCESS — deliberate, and asked for by the user. `--tools default` gives
    the CLI its whole built-in toolset (Bash, Read, Edit, Grep, …) and
    `--permission-mode bypass_permissions` stops it from blocking on an approval
    prompt that nobody is there to answer in `-p` mode. The consequence is
    exactly what it sounds like: this agent can change files and run shell
    commands on this machine by itself. The model is clamped to the free
    allowlist so "full access" never becomes "paid model".
    """
    cli = _find_cli()
    if not cli:
        raise RuntimeError("qoder CLI não encontrado no PATH nem em ~/.qodersec/bin")
    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
    model = free_model(model or st["model"])
    thinking = st.get("thinking") or CLI_DEFAULT_THINKING
    tools_arg = str(st.get("tools") if st.get("tools") is not None else "default")
    cmd = [cli, "-p", "--output-format", "json",
           "--model", model,
           "--thinking", thinking]
    if thinking == "enabled" and int(st.get("thinking_budget") or 0) > 0:
        cmd += ["--thinking-budget", str(int(st["thinking_budget"]))]
    cmd += ["--reasoning-effort", st.get("reasoning_effort") or CLI_DEFAULT_EFFORT,
            "--context-window", str(int(st.get("context_window") or CLI_DEFAULT_CONTEXT)),
            "--tools", tools_arg,
            "--max-output-tokens", str(max(64, int(max_tokens))),
            "--no-session-persistence",
            "--permission-mode", "bypass_permissions",
            "--", full]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout or st["timeout"],
                           cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"o Qoder passou de {(timeout or st['timeout']):.0f}s sem terminar")
    out = (r.stdout or "").strip()
    saw_json = False
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        saw_json = True
        # Any event that says it failed ends the turn as an error. Checking the
        # whole object, not just `result`, covers the CLI's synthetic result
        # event, which carries no answer text.
        if _cli_failed(j) and j.get("type") in ("result", "error", None):
            raise _cli_error(j)
        if j.get("type") == "result" or "result" in j:
            if _cli_failed(j):
                raise _cli_error(j)
            text = str(j.get("result") or "").strip()
            if not text:
                raise RuntimeError("qoder CLI terminou sem resposta")
            return text
    if r.returncode != 0:
        detail = (r.stderr or "").strip() or out[:300]
        raise RuntimeError(f"qoder CLI falhou (rc={r.returncode}): {detail[:300]}")
    if saw_json:
        # A JSON stream that never produced a result is a broken turn, not an
        # answer. Never hand the raw object to the voice.
        raise RuntimeError(f"qoder CLI devolveu uma resposta inválida: {out[:200]}")
    return out


def _ask_proxy(prompt: str, st: dict, model: str | None = None,
               system: str | None = None, max_tokens: int = 1024,
               temperature: float = 0.3, timeout: float | None = None) -> str:
    """The original OpenAI-style path through the qoder-proxy container."""
    if not ensure_running(st):
        raise RuntimeError(
            f"Qoder proxy is not reachable at {st['base_url']}. "
            "Start it with ./setup-qoder.sh <PAT> (or docker start qoder-proxy).")
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": free_model(model or st["model"]), "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens,
               "stream": False}
    try:
        r = requests.post(_url(st, "/chat/completions"), json=payload,
                          headers=_headers(st),
                          timeout=timeout or st["timeout"])
        if r.status_code == 401:
            raise RuntimeError("Qoder proxy rejected the key (401) — check PROXY_API_KEY "
                               "in .qoder-proxy.env and the QODER form.")
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as e:
        body = ""
        try:
            body = (e.response.text or "")[:200] if e.response is not None else ""
        except Exception:
            pass
        raise RuntimeError(f"Qoder HTTP {e.response.status_code if e.response is not None else '?'}: {body}")
    except requests.ConnectionError:
        raise RuntimeError(f"Cannot reach Qoder proxy at {st['base_url']}.")
    except requests.Timeout:
        raise RuntimeError("Qoder took too long to answer.")
    except requests.RequestException as e:
        raise RuntimeError(f"Qoder request failed: {e}")

    try:
        from core import usage as _usage
        _usage.note("qoder", calls=1)
    except Exception:
        pass

    choices = data.get("choices") or [{}]
    msg = choices[0].get("message") or {}
    text = str(msg.get("content") or "").strip()
    if not text and msg.get("reasoning_content"):
        text = str(msg["reasoning_content"]).strip()
    return text


# ── helpers for the rest of the app ─────────────────────────────────────────

_IMAGE_MARK = "[imagem não suportada neste caminho]"


def flatten(contents) -> str:
    """Turn whatever a Gemini call was handed into one prompt string.

    Every existing call site passes a string. A few pass parts (an image plus a
    question) — those arrive as a list, and the text parts are kept while the
    images are noted as dropped, because a proxy-backed text model cannot see
    them. Saying so beats pretending the picture was read.
    """
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, (list, tuple)):
        parts = [_IMAGE_MARK if _looks_like_image(p) else flatten(p) for p in contents]
        return "\n".join(p for p in parts if p)
    text = getattr(contents, "text", None)
    if isinstance(text, str):
        return text
    if getattr(contents, "inline_data", None) is not None:
        return _IMAGE_MARK
    try:
        return str(contents)
    except Exception:
        return ""


def _looks_like_image(part) -> bool:
    if isinstance(part, (bytes, bytearray)):
        return True
    for attr in ("inline_data", "file_data"):
        if getattr(part, attr, None) is not None:
            return True
    return False


def strip_fences(text: str) -> str:
    """Model output → the code or JSON inside it, without the fence."""
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z0-9_+-]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def first_json_object(text: str) -> dict | None:
    """The first {...} in a reply, parsed. Models like to wrap JSON in prose
    even when told not to; this is the tolerant read of that."""
    if not text:
        return None
    body = strip_fences(text)
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        val = json.loads(body[start:end + 1])
    except Exception:
        return None
    return val if isinstance(val, dict) else None


def test_connection(values: dict):
    """⚙ TEST CONNECTION for the QODER form: reachable, key accepted, model
    present, and one tiny real answer."""
    st = settings(lambda _ns: dict(values))
    if not st["api_key"]:
        return False, "No proxy API key — set it in .qoder-proxy.env (PROXY_API_KEY) " \
                      "or here, then run ./setup-qoder.sh."
    if not health(st):
        started = ensure_running(st)
        if not started:
            return False, (f"Nothing is listening at {st['base_url']}. Run "
                           "./setup-qoder.sh <PAT-do-Qoder> once — it creates the "
                           "container; this button only starts an existing one.")
    try:
        ids = models(st)
    except Exception as e:
        return False, f"Proxy answered but the model list failed: {e}"
    note = ""
    if ids and st["model"] not in ids:
        note = (f" (model '{st['model']}' is not in the proxy's list — "
                f"try one of: {', '.join(ids[:6])})")
    try:
        reply = ask("Responda apenas: pronto", st=st, max_tokens=16, timeout=30)
    except Exception as e:
        return False, f"Proxy is up but the model refused: {e}{note}"
    return True, (f"Connected — {len(ids)} models available, '{st['model']}' answered: "
                  f"\"{reply[:40]}\"{note}")
