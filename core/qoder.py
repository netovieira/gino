"""
Qoder bridge — the assistant's door to Qoder's models.

WHAT THIS IS
    Qoder is an IDE, and its models are reached through a small local server:
    `qoder-proxy` (setup-qoder.sh), which speaks the OpenAI chat-completions
    API and forwards each request to Qoder on the person's own paid plan. So
    from this application's point of view Qoder is a LOCAL model server: it
    listens on 127.0.0.1, it costs nothing per call, and it is already the
    fastest thing reachable. That is why it leads the brain chain.

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
        workspace  ~/JarvisProjects              (where written files land)

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
DEFAULT_MODEL = "DeepSeek-Flash"
CLI_DEFAULT_THINKING = "adaptive"      # auto | adaptive | enabled | disabled
CLI_DEFAULT_EFFORT   = "max"           # low | medium | high | max
CLI_DEFAULT_CONTEXT  = 1_000_000        # 1M-token window
DEFAULT_WORKSPACE = "~/JarvisProjects"
DEFAULT_TIMEOUT = 120

# What the proxy advertises, by what each one is for. Labels only — the id on
# the left is what goes on the wire. Offered in the settings form so nobody has
# to remember ids like "gm51model".
MODELS = {
    "DeepSeek-Flash":      "DeepSeek Flash — free, 1M context (recomendado)",
    "DeepSeek-V4-Pro":     "DeepSeek V4 Pro — raciocínio mais fundo",
    "Qwen3.8-Max":         "Qwen 3.8 Max",
    "Qwen3.8-Flash":       "Qwen 3.8 Flash — rápido",
    "Kimi-K3.1-Pro":       "Kimi K3.1 Pro",
    "Kimi-K2.8-Preview":   "Kimi K2.8 (preview)",
    "GLM-5.3":             "GLM 5.3",
    "GLM-5.3-Flash":       "GLM 5.3 Flash",
    "MiniMax-M3.1-Flash":  "MiniMax M3.1 Flash",
    "Efficient":           "Efficient — o mais barato",
    "Auto":                "Auto — o Qoder escolhe por tarefa",
}

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


def settings(get_plugin_config=None) -> dict:
    """The effective Qoder settings: form values first, env file second."""
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
    model = str(cfg.get("model") or "").strip() or str(env.get("MODEL") or "").strip() or DEFAULT_MODEL
    workspace = str(cfg.get("workspace") or "").strip() or DEFAULT_WORKSPACE
    try:
        timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    # The CLI is the default transport: it reaches the free DeepSeek-Flash with
    # a 1M window and a real thinking budget, and it does not depend on the
    # container. "proxy" keeps the old OpenAI-style path as an explicit choice.
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
    # Full access is the default now, because the user asked for it. The two
    # knobs that control it are read here so they can be taken back from the
    # settings form without touching code: "default" tools + a permission mode
    # of "default" restores the old pure-reply behaviour.
    tools_arg = cfg.get("tools")
    tools_arg = "default" if tools_arg is None else str(tools_arg)
    perm = str(cfg.get("permission_mode") or "bypass_permissions").strip().lower()
    if perm not in ("default", "accept_edits", "bypass_permissions", "dont_ask", "auto"):
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
               on_text=None, on_tool=None,
               model: str | None = None, system: str | None = None,
               timeout: float | None = None) -> str:
    """One CLI turn, narrated while it works — then the final answer.

    The non-streamed call only speaks at the end, which for a coding task means
    20–40 seconds of silence and then a wall of text. In `stream-json` the same
    turn arrives as events, and the model's own progress lines are right there:

        {"type":"assistant","content":[{"type":"text","text":"Vou olhar …"}]}
        {"type":"assistant","content":[{"type":"tool_use","name":"Read", …}]}
        {"type":"result","result":"…"}

    `on_text` receives the sentences worth saying out loud (the model writes them
    for the user); `on_tool` receives (name, input) so the caller can decide
    whether a short "lendo tal arquivo" is useful. Returns the final result.
    """
    cli = _find_cli()
    if not cli:
        raise RuntimeError("qoder CLI não encontrado no PATH nem em ~/.qodersec/bin")
    st = st or settings()
    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
    thinking = st.get("thinking") or CLI_DEFAULT_THINKING
    tools_arg = str(st.get("tools") if st.get("tools") is not None else "default")
    perm = str(st.get("permission_mode") or "bypass_permissions").strip()
    cmd = [cli, "-p", "--output-format", "stream-json",
           "--model", model or st["model"],
           "--thinking", thinking,
           "--reasoning-effort", st.get("reasoning_effort") or CLI_DEFAULT_EFFORT,
           "--context-window", str(int(st.get("context_window") or CLI_DEFAULT_CONTEXT)),
           "--tools", tools_arg,
           "--max-output-tokens", "8192",
           "--no-session-persistence"]
    if perm:
        cmd += ["--permission-mode", perm]
    cmd += ["--", full]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    limit = float(timeout or st.get("timeout") or DEFAULT_TIMEOUT)

    def _kill() -> None:
        try:
            proc.kill()
        except Exception:
            pass

    watchdog = threading.Timer(limit, _kill)
    watchdog.daemon = True
    watchdog.start()

    final = ""
    try:
        for line in proc.stdout:
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
                    elif kind == "tool_use" and on_tool:
                        try:
                            on_tool(str(part.get("name") or ""),
                                    part.get("input") or {})
                        except Exception:
                            pass
            elif typ == "result":
                final = str(ev.get("result") or "")
                if ev.get("is_error"):
                    raise RuntimeError(str(ev.get("result"))[:300])
        proc.wait(timeout=5)
    finally:
        watchdog.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass
    if not final:
        err = (proc.stderr.read() or "").strip() if proc.stderr else ""
        if err:
            raise RuntimeError(err[:300])
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


def _ask_cli(prompt: str, st: dict, model: str | None = None,
             system: str | None = None, max_tokens: int = 1024,
             timeout: float | None = None) -> str:
    """One non-interactive `qoder -p` turn, returned as text.

    FULL ACCESS — deliberate, and asked for by the user. `--tools default` gives
    the CLI its whole built-in toolset (Bash, Read, Edit, Grep, …) and
    `--permission-mode bypass_permissions` stops it from blocking on an approval
    prompt that nobody is there to answer in `-p` mode. The consequence is
    exactly what it sounds like: this agent can now change files and run shell
    commands on this machine by itself.

    To take it back, set plugin_config.qoder.permission_mode to "default" and
    plugin_config.qoder.tools to "" (the values are read here on every call).

    Thinking, effort and context come from settings so the same choices (free
    DeepSeek-Flash, 1M window, max effort) hold for every delegated task.
    """
    cli = _find_cli()
    if not cli:
        raise RuntimeError("qoder CLI não encontrado no PATH nem em ~/.qodersec/bin")
    full = prompt if not system else f"{system}\n\n---\n\n{prompt}"
    thinking = st.get("thinking") or CLI_DEFAULT_THINKING
    tools_arg = str(st.get("tools") if st.get("tools") is not None else "default")
    perm = str(st.get("permission_mode") or "bypass_permissions").strip()
    cmd = [cli, "-p", "--output-format", "json",
           "--model", model or st["model"],
           "--thinking", thinking]
    if thinking == "enabled" and int(st.get("thinking_budget") or 0) > 0:
        cmd += ["--thinking-budget", str(int(st["thinking_budget"]))]
    cmd += ["--reasoning-effort", st.get("reasoning_effort") or CLI_DEFAULT_EFFORT,
            "--context-window", str(int(st.get("context_window") or CLI_DEFAULT_CONTEXT)),
            "--tools", tools_arg,
            "--max-output-tokens", str(max(64, int(max_tokens))),
            "--no-session-persistence"]
    if perm:
        cmd += ["--permission-mode", perm]
    cmd += ["--", full]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout or st["timeout"])
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"qoder CLI não respondeu em {timeout or st['timeout']}s")
    out = (r.stdout or "").strip()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if isinstance(j, dict) and "result" in j:
            text = str(j.get("result") or "").strip()
            if j.get("is_error"):
                raise RuntimeError(f"qoder CLI devolveu erro: {text[:300]}")
            return text
    if r.returncode != 0 and not out:
        raise RuntimeError(f"qoder CLI falhou (rc={r.returncode}): "
                           f"{(r.stderr or '').strip()[:300]}")
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

    payload = {"model": model or st["model"], "messages": messages,
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
