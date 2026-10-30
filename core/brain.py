"""
Economy brain — an OpenAI-compatible chat model writes the replies, a free
voice reads them, and Gemini is not involved at all.

The expensive part of Gemini Live is audio: every second in and every second
out is billed. This mode replaces the whole session with three cheap pieces:

    microphone → Whisper on Groq (core.groq_stt, free)
              → a chat model over /chat/completions (DeepSeek, Groq's free
                Llama, OpenRouter, or anything OpenAI-compatible)
              → Microsoft Edge TTS (free, no key) → the speakers

Everything else the assistant does stays: the persona prompt, memory, every
action and plugin (they are offered to the model as OpenAI tools), reminders,
the briefing, the avatar's lip-sync (the voice is played through the same
queue Gemini's audio used). What is lost: barge-in by voice, vision tools
(deepseek-chat cannot see), and Gemini's native hearing of tone.

Settings come from ⚙ → PLUGIN SETTINGS → ECONOMY BRAIN (plugins/brain_economy.py),
stored under plugin_config.brain in config/api_keys.json.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Callable

import requests

NAMESPACE = "brain"
TIMEOUT = 60
MAX_REPLY_CHARS = 12000        # a spoken reply never needs more; guards runaway streams
# A spoken reply is two or three sentences; the rest of this budget is room for
# models like gpt-oss, which think before the first word. Left at 1200 it was
# also part of the problem: Groq's free tier counts prompt + tool schemas +
# max_tokens against 8000 tokens per minute, so the request was refused (HTTP
# 413) before the model ever ran. `_payload` asks for brief reasoning where the
# model accepts the hint, which makes 700 generous.
MAX_TOKENS = 700

PRESETS = {
    # The local proxy that fronts Qoder's paid models (setup-qoder.sh): it
    # listens on this machine and costs nothing per call, but it wraps
    # `qodercli`, which only hands over the finished text — ~15-40 s per reply —
    # and answers a request carrying tools with an empty completion (the
    # non-streamed form is the one that works, see _chat_stream_one). It earns
    # its place as the rung BEHIND the network one: slow beats silent.
    "qoder":      {"base_url": "http://127.0.0.1:3000/v1",
                   "model": "deepseek-v4-flash",
                   "key_url": "run ./setup-qoder.sh <PAT-do-Qoder>"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-chat",
                   "key_url": "https://platform.deepseek.com/api_keys"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1",
                   "model": "openai/gpt-oss-120b",
                   "key_url": "https://console.groq.com/keys"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "model": "deepseek/deepseek-chat",
                   "key_url": "https://openrouter.ai/keys"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                   "model": "gemini-2.5-flash",
                   "key_url": "https://aistudio.google.com/app/apikey"},
    "custom":     {"base_url": "http://localhost:11434/v1", "model": "llama3.2", "key_url": ""},
    # The `qoder` CLI the person already has installed and logged in, invoked
    # as a subprocess (`qoder -p …`). It is text-only — it exposes Qoder's own
    # built-in tools (bash, edit, grep), NOT this assistant's 44 tools — so it
    # can answer and reason but cannot act on the PC. brain.chat_stream therefore
    # skips it on any turn that carries tools, which is exactly the "smart for
    # conversation, local for tools" split. base_url is unused for this rung.
    "qodercli":   {"base_url": "", "model": "DeepSeek-Flash",
                   "key_url": "qoder login (CLI)"},
}
PROVIDERS = tuple(PRESETS)
# Offered in the FALLBACK dropdown. "none" ends the chain.
FALLBACK_PROVIDERS = ("groq", "gemini", "qoder", "deepseek", "openrouter", "custom", "none")
FALLBACK_NONE = ("none", "", "-")

# When a Groq key is configured but no fallback was spelled out, this is the
# rung behind the local model: the strongest chat model the free Groq tier still
# serves to every account. (llama-3.3-70b-versatile was retired — a name that
# 404s is worse than no fallback at all, because it costs a round trip per turn.)
GROQ_DEFAULT_MODEL = PRESETS["groq"]["model"]

# A provider that failed this way is not worth paying for on the next turn for
# a little while: a proxy that is not running refuses instantly, but one that
# is wedged takes the full TIMEOUT. Remembering that turns "every reply waits
# a minute" into "every reply is answered by the fallback".
RETRY_AFTER_S = 60
_down_until: dict[str, float] = {}

# A 429 that asks for a wait this short is worth waiting out: the same provider
# answers in a few seconds, while the rung behind it (the local proxy) would take
# ~40 s for the identical reply. Longer than this and the wait itself becomes the
# problem, so the turn moves on to the next rung instead.
RATE_LIMIT_WAIT_S = 20.0

# How long the voice may stay silent before the first sentence of a reply.
# "Local first" is a rule about ORDER, not about patience: the Qoder proxy
# wraps `qodercli`, which only hands over the finished text (~13 s), so a turn
# served by it begins speaking long after the user stopped talking. A rung
# measured above this budget is moved behind the fast ones for the rest of the
# session — still available, just not the voice of an ordinary conversation.
TTFT_BUDGET_S = 6.0
_ttft: dict[str, float] = {}
_demoted: set[str] = set()      # announced once per session, not once per turn

# Rungs that answer a request carrying `tools` with an empty completion. The
# Qoder proxy does exactly that: 200 OK, an `assistant` role delta and
# `end_turn` with nothing between them — while the same request WITHOUT tools
# (or the same request non-streamed) is answered normally. From the outside an
# empty completion is indistinguishable from a success, which is how it once
# silenced every turn of a conversation. Remembered for the session so the
# cost is one wasted round trip, not one per turn.
_empty_with_tools: set[str] = set()

DEFAULT_VOICE = "pt-BR-AntonioNeural"
EDGE_VOICES = ("pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaNeural",
               "pt-PT-DuarteNeural", "en-GB-RyanNeural", "en-US-GuyNeural")

# Which service reads the replies aloud. "edge" is free (Microsoft); "fish" is
# Fish Audio (fish.audio), paid per character, any voice from its library or a
# clone of your own — this is how the assistant gets a specific voice.
TTS_ENGINES = ("edge", "fish")
FISH_BASE = "https://api.fish.audio"
FISH_MODELS = ("s2.1-pro-free", "s2-pro", "s1")

# Tools that only make sense when the model can receive an image.
_VISION_TOOLS = {"screen_process", "close_camera"}


def settings(get_plugin_config: Callable[[str], dict]) -> dict:
    cfg = get_plugin_config(NAMESPACE)
    provider = str(cfg.get("provider") or "deepseek").strip().lower()
    preset = PRESETS.get(provider, PRESETS["deepseek"])
    base = str(cfg.get("base_url") or "").strip().rstrip("/") or preset["base_url"]
    model = str(cfg.get("model") or "").strip() or preset["model"]
    voice = str(cfg.get("tts_voice") or DEFAULT_VOICE).strip() or DEFAULT_VOICE
    engine = str(cfg.get("tts_engine") or "edge").strip().lower()
    fish_model = str(cfg.get("fish_model") or FISH_MODELS[0]).strip() or FISH_MODELS[0]
    vision_model = str(cfg.get("vision_model") or "qwen2.5vl:3b").strip()
    st = {
        "provider": provider if provider in PRESETS else "deepseek",
        "base_url": base, "model": model,
        "api_key": str(cfg.get("api_key") or "").strip(),
        "tts_voice": voice,
        "tts_engine": engine if engine in TTS_ENGINES else "edge",
        "fish_api_key": str(cfg.get("fish_api_key") or "").strip(),
        "fish_voice_id": fish_voice_id(cfg.get("fish_voice_id")),
        "fish_model": fish_model if fish_model in FISH_MODELS else FISH_MODELS[0],
        "vision_model": vision_model,
    }
    st["chain"] = build_chain(st, cfg, get_plugin_config)
    st["router"] = _router_settings(cfg, get_plugin_config)
    # Vision runs on the LOCAL Ollama regardless of which rung answers the
    # conversation: qwen2.5vl reads the screenshot on this machine and the image
    # never leaves it. With qodercli/local as the chain, the primary provider is
    # no longer "custom", so the plugins must ask THIS instead of testing the
    # provider name themselves.
    vision_base = ""
    for rung in st["chain"]:
        if rung.get("provider") == "custom" or ":11434" in str(rung.get("base_url") or ""):
            vision_base = str(rung.get("base_url") or "").rstrip("/")
            break
    st["vision_base_url"] = vision_base or "http://127.0.0.1:11434/v1"
    st["vision_local"] = bool(vision_base) or provider == "custom"
    return st


def _router_settings(cfg: dict, get_plugin_config) -> dict:
    """Config of the small model that decides, per turn, which tools are needed.

    The answer model (a local 7B) is weak at choosing among ~44 tool schemas,
    so a separate, smarter-but-cheap model does only the choosing. The routing
    request is ~1k tokens — far under Groq's per-minute budget, and it answers
    in half a second. The key defaults to the brain's Groq fallback (or the
    groq namespace's key); with no key the router is disabled and main.py falls
    back to its keyword gate.
    """
    provider = str(cfg.get("router_provider") or "groq").strip().lower()
    preset = PRESETS.get(provider, PRESETS["groq"])
    model = str(cfg.get("router_model") or preset["model"]).strip() or preset["model"]
    base = str(cfg.get("router_base_url") or "").strip().rstrip("/") or preset["base_url"]
    key = str(cfg.get("router_api_key") or "").strip()
    if not key and provider == "groq":
        if str(cfg.get("fallback_provider") or "").strip().lower() == "groq":
            key = str(cfg.get("fallback_api_key") or "").strip()
        if not key and get_plugin_config:
            try:
                key = str(get_plugin_config("groq").get("api_key") or "").strip()
            except Exception:
                pass
    return {
        "provider": provider, "model": model, "base_url": base, "api_key": key,
        "enabled": bool(cfg.get("router_enabled", True)) and bool(key),
    }


def _entry(provider: str, model: str = "", base_url: str = "", api_key: str = "") -> dict | None:
    """One rung of the chain, as a full provider record.

    A rung needs a base_url; anything else it does not spell out comes from the
    preset of the same name, so "groq" alone is a complete answer.
    """
    provider = str(provider or "").strip().lower()
    if provider in FALLBACK_NONE:
        return None
    preset = PRESETS.get(provider, {})
    url = (str(base_url or "").strip().rstrip("/")
           or str(preset.get("base_url") or "").rstrip("/"))
    # qodercli is a subprocess (`qoder -p`), not an HTTP endpoint — it is the
    # one rung that is complete without a base_url.
    if not url and provider != "qodercli":
        return None
    return {
        "provider": provider if provider in PRESETS else "custom",
        "base_url": url,
        "model": str(model or "").strip() or str(preset.get("model") or ""),
        "api_key": str(api_key or "").strip(),
    }


def label(rung: dict) -> str:
    """'qoder/deepseek-v4-flash' — how a rung is named in the log."""
    return f"{rung.get('provider', '?')}/{rung.get('model') or '(no model)'}"


def build_chain(st: dict, cfg: dict,
                get_plugin_config: Callable[[str], dict] | None = None) -> list[dict]:
    """The providers to try, in order: the configured one first, then the
    fallbacks.

    The order is decided by what answers usefully, not by where the model runs:
    a local rung that takes fifteen seconds and returns an empty completion for
    a request carrying tools is not the voice of a conversation. chat_stream
    still demotes a rung that provably needs longer than TTFT_BUDGET_S to start
    speaking, because a correct answer that arrives after fourteen seconds of
    silence is not a conversational answer.

    Declared explicitly (both shapes work) in plugin_config.brain:

        "fallback_provider": "groq",          # one rung
        "fallback_model":    "openai/gpt-oss-120b",
        "fallback_api_key":  "gsk_..."

        "fallbacks": [                        # or as many as you like
          {"provider": "groq", "model": "openai/gpt-oss-120b", "api_key": "gsk_..."},
          {"provider": "gemini", "api_key": "AIza..."}
        ]

    With neither declared, a Groq key already saved for the ears (GROQ form)
    becomes the fallback by itself — the same key, the model Groq currently
    serves. A second copy of the key in a second form is how keys drift.
    """
    chain = [_entry(st["provider"], st["model"], st["base_url"], st["api_key"])]
    primary = chain[0] or {}

    declared: list[dict] = []
    raw_list = cfg.get("fallbacks")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                declared.append(item)
            elif isinstance(item, str):
                declared.append({"provider": item})
    if cfg.get("fallback_provider") is not None:
        declared.insert(0, {
            "provider": cfg.get("fallback_provider"),
            "model": cfg.get("fallback_model"),
            "api_key": cfg.get("fallback_api_key"),
            "base_url": cfg.get("fallback_base_url"),
        })

    for item in declared:
        rung = _entry(item.get("provider", ""), item.get("model", ""),
                      item.get("base_url", ""), item.get("api_key", ""))
        if rung:
            chain.append(rung)

    # Nothing declared: pick up a Groq key that was saved for the ears.
    if len(chain) == 1 and primary.get("provider") != "groq" and get_plugin_config:
        try:
            groq_cfg = get_plugin_config("groq")
        except Exception:
            groq_cfg = {}
        key = str((groq_cfg or {}).get("api_key") or "").strip()
        if key:
            fallback = _entry("groq", GROQ_DEFAULT_MODEL, "", key)
            if fallback:
                chain.append(fallback)

    # Dedupe on what actually goes on the wire — a rung repeated is a rung that
    # costs a wasted round trip on every failure.
    out: list[dict] = []
    seen: set[tuple] = set()
    for rung in chain:
        if not rung:
            continue
        key = (rung["base_url"], rung["model"])
        if key in seen:
            continue
        seen.add(key)
        out.append(rung)
    return out or [st]


def fish_model_is_free(st: dict) -> bool:
    """Free Fish Audio models (`s2.1-pro-free`) synthesise with no credit on the
    account. Gating them on a balance would refuse a voice that works."""
    return str(st.get("fish_model") or "").endswith("-free")



def fish_voice_id(raw) -> str:
    """Accept the bare model id or the voice's page URL
    (https://fish.audio/m/<id>/ ...) and return the id."""
    txt = str(raw or "").strip()
    if not txt:
        return ""
    m = re.search(r"[0-9a-fA-F]{32}", txt)
    if m:
        return m.group(0).lower()
    return txt.rstrip("/").split("/")[-1].split("?")[0]


def voice_label(st: dict) -> str:
    if st.get("tts_engine") == "fish":
        return f"Fish Audio {st.get('fish_model')} voice {st.get('fish_voice_id') or '(default)'}"
    return f"Edge TTS {st.get('tts_voice')}"


# ── tools: Gemini declarations → OpenAI tools ────────────────────────────────

# Tool descriptions come from the action and plugin registries and can run to a
# full paragraph (1024 characters). Sent whole, the 34 of them were ~3300 tokens
# of a budget some providers cap at 8000 for the ENTIRE request — prompt, tools
# and answer together. 220 characters keeps the sentence that says when to call
# the tool, which is the part a model actually reads at call time.
_TOOL_DESC_CHARS = 220
# The same argument, one level down: the per-property descriptions inside the
# schemas were another ~2800 tokens of the request (141 of them). What a model
# needs before calling is the type, the name and a hint of the format — 90
# characters hold "AAAA-MM-DD, e.g. 2026-10-29" and little else of use.
_ARG_DESC_CHARS = 90


def _clip_desc(text: str, limit: int = _TOOL_DESC_CHARS) -> str:
    """One clean sentence or two, cut at a word boundary."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.55 else cut).rstrip(" ,;:.") + "…"


def _forget_earlier_turns(messages: list) -> list:
    """The system prompt plus the current turn: what is left when a provider says
    the conversation is too big to accept. The last user message and anything
    after it (tool calls and their answers) has to survive — without it the
    request would have no question in it."""
    system = [m for m in messages if m.get("role") == "system"][:1]
    rest = [m for m in messages if m.get("role") != "system"]
    for i in range(len(rest) - 1, -1, -1):
        if rest[i].get("role") == "user":
            return system + rest[i:]
    return messages


def _lower_schema(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            elif k == "description" and isinstance(v, str):
                out[k] = _clip_desc(v, _ARG_DESC_CHARS)
            elif k in ("properties", "items") or isinstance(v, (dict, list)):
                out[k] = _lower_schema(v)
            else:
                out[k] = v
        return out
    if isinstance(node, list):
        return [_lower_schema(x) for x in node]
    return node


def to_openai_tools(decls: list, include_vision: bool = False) -> list[dict]:
    out = []
    for d in decls:
        if not isinstance(d, dict):
            d = {"name": getattr(d, "name", ""), "description": getattr(d, "description", ""),
                 "parameters": getattr(d, "parameters", None)}
        name = d.get("name")
        if not name or (name in _VISION_TOOLS and not include_vision):
            continue
        params = _lower_schema(d.get("parameters") or {"type": "object", "properties": {}})
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        out.append({"type": "function", "function": {
            "name": name, "description": _clip_desc(d.get("description") or ""),
            "parameters": params}})
    return out


# ── chat ─────────────────────────────────────────────────────────────────────

# A sentence ends at punctuation FOLLOWED BY WHITESPACE. Matching the end of
# the buffer too cut "R$ 381.000,00" into "R$ 381." and "000,00" whenever a
# streamed chunk happened to stop on the dot — the voice then read two
# nonsense numbers. The tail is flushed when the stream ends anyway.
_SENT_END = re.compile(r"(?<=[.!?…])\s+|\n+")


def strip_tool_markup(text: str) -> str:
    """Remove the model's own tool-call syntax from a reply.

    DeepSeek models (the family behind the Qoder CLI) express a tool call with
    a DSML block — `<｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="Glob"> …`. With
    tools disabled for the conversation rung, the model still *wants* one and
    emits that block as ordinary text, which then gets spoken out loud. Nothing
    of it is for the user's ears, and nothing of it is data worth keeping.
    """
    t = str(text or "")
    if not any(m in t for m in ("DSML", "<|", "<｜")):
        return t
    # The block always sits at the END of the reply, and everything inside it is
    # machine text — including the parameter values ("**/*{wake,Wake}*"), which
    # survived a tag-only strip and would have been read aloud. Cut at the
    # opener instead of removing tag by tag.
    opener = re.search(r"<[^>\n]{0,80}(?:DSML|tool_call|invoke\s+name)[^>\n]{0,80}>",
                       t, re.IGNORECASE)
    if opener:
        t = t[:opener.start()]
    t = re.sub(r"<[^>\n]{0,80}DSML[^>\n]{0,80}>", " ", t)
    t = re.sub(r"<[^>\n]{0,40}(?:invoke|parameter|calls|tool_call)[^>\n]{0,80}>",
               " ", t, flags=re.IGNORECASE)
    # Resíduos: `<|/`, `|>`, `｜｜` soltos depois de tirar as tags.
    t = re.sub(r"[<｜|]{1,2}\s*/?\s*(?:DSML|invoke|parameter|calls)?\s*[>｜|]{1,2}",
               " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip(" \t\n-—")


class RequestTooLarge(RuntimeError):
    """The provider refused the request itself for size. The turn can still be
    answered with less context, so the caller retries once without the earlier
    turns before giving up on it."""


class ContextTooLong(RuntimeError):
    """The provider refused the request because the conversation sent is
    bigger than the model's window. The caller drops the history and retries."""


class EmptyReply(RuntimeError):
    """The provider answered 200 and then streamed nothing at all — no text and
    no tool calls. It is a failure, not a reply: with nothing to say there is
    nothing to synthesise, and the turn ends in silence with no error written
    anywhere. Treated like any other failure so the chain moves on."""


def _rung_key(rung: dict) -> str:
    return f"{rung.get('base_url', '')}|{rung.get('model', '')}"


def _is_down(rung: dict) -> bool:
    until = _down_until.get(_rung_key(rung), 0.0)
    if until and time.monotonic() < until:
        return True
    _down_until.pop(_rung_key(rung), None)
    return False


def _note_down(rung: dict, exc: Exception | None = None) -> None:
    """Skip this rung for a while. A 429 carries its own wait, and honouring it
    beats a fixed minute: retrying a rate-limited model early keeps it limited."""
    wait = (_retry_after(exc) if exc else None) or RETRY_AFTER_S
    _down_until[_rung_key(rung)] = time.monotonic() + wait


def _retry_after(exc: Exception) -> float | None:
    """How long a provider asked to be left alone, in seconds (429 only)."""
    r = getattr(exc, "response", None)
    if r is None or getattr(r, "status_code", None) != 429:
        return None
    wait = None
    try:
        raw = (r.headers.get("retry-after") or "").strip()
        wait = float(raw) if raw else None
    except (ValueError, AttributeError):
        wait = None
    if wait is None:
        m = re.search(r"try again in ([\d.]+)\s*(ms|s|m)", r.text or "", re.I)
        if m:
            unit = m.group(2).lower()
            wait = float(m.group(1)) / {"ms": 1000.0, "s": 1.0, "m": 60.0}[unit]
    return max(5.0, min(wait, 900.0)) if wait is not None else None


def _note_up(rung: dict) -> None:
    _down_until.pop(_rung_key(rung), None)


def _order_rungs(chain: list[dict]) -> list[dict]:
    """The chain, sorted for this turn: fast rungs first, then slow ones, then
    the ones cooling down.

    Config order decides between rungs of the same kind, so "local first" holds
    until the local rung has shown it cannot start speaking inside
    TTFT_BUDGET_S. Only rungs that actually answered are measured — an unknown
    rung keeps its place, because the point is to trust the configured order
    until there is evidence against it.
    """
    up = [r for r in chain if not _is_down(r)]
    down = [r for r in chain if _is_down(r)]
    # The qoder CLI is slow to START (it answers whole, ~7 s) but is the smartest
    # rung, and it only ever runs on turns WITHOUT tools. Demoting it behind the
    # local model — which is what the TTFT rule would do after one turn — would
    # quietly throw away the reason it is first. It keeps its configured place;
    # the local fallback still catches it when the CLI is down.
    def _is_cli(r: dict) -> bool:
        return r.get("provider") == "qodercli"
    fast = [r for r in up
            if _is_cli(r) or _ttft.get(_rung_key(r), 0.0) <= TTFT_BUDGET_S]
    slow = [r for r in up
            if not _is_cli(r) and _ttft.get(_rung_key(r), 0.0) > TTFT_BUDGET_S]
    if slow and fast:
        fresh = [r for r in slow if _rung_key(r) not in _demoted]
        for r in fresh:
            _demoted.add(_rung_key(r))
            print(f"[Brain] {label(r)} takes ~{_ttft[_rung_key(r)]:.0f}s to start "
                  f"speaking — {label(fast[0])} goes first from now on "
                  f"(the slow one still answers when that one fails).")
    return fast + slow + down


def _last_user_text(messages: list) -> str:
    """The text of the most recent user turn, flattened."""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        return str(c or "")
    return ""


def chat_stream(messages: list, tools: list | None, st: dict,
                on_sentence: Callable[[str], None],
                should_stop: Callable[[], bool] = lambda: False) -> tuple[str, list[dict]]:
    """Stream one reply, walking the provider chain until one answers.

    The chain (st["chain"]) is built by settings(): the local model first, the
    network fallback behind it. A rung that fails is skipped for RETRY_AFTER_S
    so a proxy that is not running costs one refused connection, once — not one
    on every turn. A rung that answers but starts speaking too late is moved
    behind the fast ones instead (see _order_rungs).

    A retry only happens while NOTHING has been spoken yet. Once the voice has
    said the first sentence of an answer, switching providers would make it
    start over from the beginning, so the error goes up instead; the caller
    reports it and the next turn is answered by the fallback.

    A rung that answers with nothing at all is a FAILURE (see EmptyReply), not
    a reply — otherwise the empty completion ends the turn in silence and the
    fallback, which was standing by, is never asked.
    """
    chain = st.get("chain") or [st]
    order = _order_rungs(chain)
    upward: list[int] = [0]

    def _say(sentence: str) -> None:
        upward[0] += 1
        on_sentence(sentence)

    last: Exception | None = None
    for i, rung in enumerate(order):
        # The `qoder` CLI has no access to this assistant's tools (it only has
        # its own bash/edit/grep). Sending it a tool turn would get a reply that
        # cannot act — so on any turn that CARRIES tools it is skipped and the
        # next rung (the local model, which can call them) answers. Turn
        # without tools (conversation, reasoning) keeps it first: that is where
        # a smarter model pays off and the local 7B is weakest.
        if tools and rung.get("provider") == "qodercli":
            print(f"[Brain] {label(rung)} não tem as ferramentas do assistente — "
                  f"pulando neste turno com ferramentas.")
            continue
        # A short greeting does not deserve a thirty-second agent. "Gino, você
        # está me ouvindo?" is four words of small talk; the CLI thinks before
        # it speaks, and the local model answers the same thing in one second.
        # Anything with substance still goes to the smarter rung.
        if (not tools and rung.get("provider") == "qodercli"
                and len(_last_user_text(messages).split()) <= 6):
            print(f"[Brain] {label(rung)} pulado — turno curto, respondo local.")
            continue
        # A rung that already returned an empty completion to a request carrying
        # tools will do it again: skipping it saves its whole round trip, which
        # for the local proxy is the entire length of a normal sentence.
        if tools and _rung_key(rung) in _empty_with_tools:
            last = EmptyReply(f"{label(rung)} returns empty replies when tools are sent")
            continue
        before = upward[0]
        started = time.monotonic()
        first = [None]

        def _timed(sentence: str, _rung=rung, _first=first) -> None:
            if _first[0] is None:
                _first[0] = time.monotonic() - started
                _ttft[_rung_key(_rung)] = _first[0]
            _say(sentence)

        def _ask(msgs: list) -> tuple[str, list]:
            """One rung, one question — retried with less context, then without
            tools, when the provider refuses the request itself for size."""
            try:
                return _chat_stream_one(msgs, tools, rung, _timed, should_stop)
            except RequestTooLarge:
                shrunk = _forget_earlier_turns(msgs)
                if len(shrunk) != len(msgs):
                    # Nothing was spoken yet, so retrying costs nothing. The
                    # earlier turns are what gets dropped: the answer to the
                    # current question does not depend on the model remembering
                    # yesterday's.
                    print(f"[Brain] {label(rung)} refused the request as too large — "
                          f"sending it again without the earlier turns.")
                    try:
                        return _chat_stream_one(shrunk, tools, rung, _timed, should_stop)
                    except RequestTooLarge:
                        pass          # even the bare turn does not fit
                if not tools:
                    raise
                # What is left is mostly the tool schemas. A conversation-only
                # answer beats silence: on this provider the turn simply cannot
                # reach the tools, and saying so out loud is honest.
                print(f"[Brain] {label(rung)} still refuses the size — asking "
                      f"without tools (conversation only).")
                return _chat_stream_one(msgs, None, rung, _timed, should_stop)

        try:
            try:
                out = _ask(messages)
            except Exception as e:
                # A provider that counts tokens per MINUTE answers 429 and says
                # how long to wait. Waiting those seconds keeps a fast model whose
                # only problem is a quota window; handing the turn to the local
                # proxy instead costs ~40 s of silence for the same answer.
                wait = _retry_after(e)
                if (not wait or wait > RATE_LIMIT_WAIT_S or upward[0] > before
                        or should_stop()):
                    raise
                print(f"[Brain] {label(rung)} is at its per-minute limit — waiting "
                      f"{wait:.0f}s and asking again.")
                time.sleep(wait)
                out = _ask(messages)
            if not out[0].strip() and not out[1]:
                raise EmptyReply("answered with an empty completion")
            _note_up(rung)
            if i:
                print(f"[Brain] answered by {label(rung)} (fallback)")
            return out
        except ContextTooLong:
            raise
        except Exception as e:
            last = e
            if isinstance(e, EmptyReply) and tools and rung.get("provider") == "qoder":
                # Sticky ONLY for the rung whose emptiness is a property of the
                # request shape: the local proxy answers the same request fine
                # without streaming, so skipping it is a saving, not a loss. A
                # network model that returns nothing once — garbage in, a bad
                # second — must not lose every later tool turn: one empty
                # answer is an event, not a diagnosis.
                _empty_with_tools.add(_rung_key(rung))
                print(f"[Brain] {label(rung)} answered nothing to a request with "
                      f"tools — it will be skipped on tool turns from now on.")
            if upward[0] > before or should_stop():
                raise                      # already speaking; a retry would repeat it
            if not isinstance(e, EmptyReply):
                # An empty answer is not a sick provider: cooling the rung down
                # for a minute would hand the next turn — probably a fine one —
                # to a fallback that may not fit the request at all.
                _note_down(rung, e)
            nxt = order[i + 1] if i + 1 < len(order) else None
            where = f"{label(rung)} failed" if not nxt else f"{label(rung)} failed — trying {label(nxt)}"
            print(f"[Brain] {where}: {type(e).__name__}: {str(e)[:160]}")
    raise last if last else RuntimeError("no provider answered")


def warm_up(st: dict) -> None:
    """Measure the primary rung once, during startup, in the background.

    The chain order is decided by measurement — so without this the user's FIRST
    sentence pays for the measurement and only the second one is fast. Startup
    already spends seconds loading the speech model and opening the microphone,
    which is exactly the window in which a silent probe costs nothing.
    """
    chain = st.get("chain") or []
    if not chain:
        return
    rung = chain[0]
    if _rung_key(rung) in _ttft:
        return
    started = time.monotonic()
    try:
        _chat_stream_one(
            [{"role": "system", "content": "Reply with the single word OK."},
             {"role": "user", "content": "Ready?"}],
            None, rung, lambda _s: None, lambda: False)
    except Exception as e:
        print(f"[Brain] warm-up of {label(rung)} failed: {type(e).__name__}: {str(e)[:120]}")
        return
    _ttft[_rung_key(rung)] = time.monotonic() - started
    elapsed = _ttft[_rung_key(rung)]
    behind = chain[1] if len(chain) > 1 else None
    # The qoder CLI answers whole, so its "time to first sentence" is the whole
    # reply — but it is the smartest rung and only runs on tool-free turns.
    # Demoting it here would silently undo the point of putting it first.
    if elapsed > TTFT_BUDGET_S and behind and rung.get("provider") != "qodercli":
        # Announce it here as well, so _order_rungs does not repeat the same
        # sentence on the first turn.
        _demoted.add(_rung_key(rung))
        print(f"[Brain] {label(rung)} takes {elapsed:.1f}s to start speaking — "
              f"{label(behind)} goes first (the slow one still answers when that "
              f"one fails).")
    else:
        print(f"[Brain] {label(rung)} ready in {elapsed:.1f}s.")


def _payload(messages: list, tools: list | None, st: dict, stream: bool) -> dict:
    """The chat-completions body, shared by the streaming and the one-shot
    request so the two can never drift apart."""
    payload = {"model": st["model"], "messages": messages, "stream": stream,
               "max_tokens": MAX_TOKENS, "temperature": 0.6}
    # gpt-oss reasons before it answers, and that reasoning is billed and counted
    # against the same allowance as the answer: at the default effort a one-line
    # greeting spent ~240 tokens thinking. "low" answered the same greeting with 4.
    # Only sent to the models known to accept the field — the others 400 on it.
    if "gpt-oss" in str(st.get("model") or ""):
        payload["reasoning_effort"] = "low"
    # Ollama's default window is 4096 tokens — SMALLER than this assistant's own
    # request (system prompt plus 34 tool schemas is ~6.5k). Without a bigger
    # num_ctx the server truncates the conversation before the model reads it.
    # The KV cache for 16k at 7B is ~1 GB, which is what the video memory is for.
    if st.get("provider") == "custom" or ":11434" in str(st.get("base_url") or ""):
        payload["options"] = {"num_ctx": 16384}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _raise_for_status(r) -> None:
    """Turn an HTTP error into the exception the caller knows how to act on."""
    if r.status_code < 400:
        return
    body = r.text[:300]
    low = body.lower()
    if r.status_code == 400 and ("context length" in low or "maximum context" in low
                                or "too many tokens" in low or "token limit" in low):
        raise ContextTooLong(f"{r.status_code}: {body}")
    if r.status_code == 413:
        # Groq's free tier refuses the request itself — prompt, tool schemas and
        # max_tokens together over the per-minute allowance — and says so with
        # 413 and the two numbers. Waiting does not make it fit; sending less does.
        raise RequestTooLarge(f"{r.status_code}: request too large for the provider "
                              f"({body[:220]})")
    raise requests.HTTPError(f"{r.status_code}: {body}", response=r)


def _tool_calls_from(message: dict) -> list[dict]:
    """A non-streamed message's tool calls, in the shape the callers use."""
    out = []
    for c in message.get("tool_calls") or []:
        fn = c.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        out.append({"id": c.get("id") or f"call_{len(out)}", "name": name,
                    "arguments": args if isinstance(args, dict) else {}})
    return out


def _qodercli_turn(messages: list, rung: dict,
                   on_sentence: Callable[[str], None],
                   should_stop: Callable[[], bool]) -> tuple[str, list[dict]]:
    """One turn answered by the `qoder` CLI instead of an HTTP endpoint.

    The CLI answers whole, not token by token, so the reply is split into
    sentences after it arrives — the voice still speaks sentence by sentence,
    it just waits for the whole answer first. No tool calls are returned: this
    rung is only ever used on turns WITHOUT tools (chat_stream skips it when
    tools are sent), because the CLI cannot call this assistant's tools.
    """
    from core import qoder as _q
    system_parts: list[str] = []
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        text = m.get("content")
        if isinstance(text, list):          # multimodal: keep the text bits only
            text = " ".join(str(p.get("text") or "")
                            for p in text if isinstance(p, dict))
        text = str(text or "").strip()
        if not text:
            continue
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            lines.append(f"Usuário: {text}")
        elif role == "assistant":
            lines.append(f"Assistente: {text}")
        elif role == "tool":
            lines.append(f"[resultado] {text}")
    prompt = "\n".join(lines) or "(sem mensagem)"
    qst = _q.settings()
    qst["transport"] = "cli"
    if rung.get("model"):
        qst["model"] = rung["model"]
    # Full tool access belongs to DELEGATION (qoder_agent), not to holding a
    # conversation. With tools enabled the CLI spends the turn deciding whether
    # a greeting needs a shell command — "Gino, você está me ouvindo?" took 30 s.
    # Here its only job is to answer in words.
    qst["tools"] = ""
    qst["permission_mode"] = "default"
    reply = (_q.ask(prompt, st=qst, system="\n\n".join(system_parts) or None,
                    max_tokens=MAX_TOKENS) or "").strip()[:MAX_REPLY_CHARS]
    # The CLI's model writes tool calls as DSML text when it has no tools; that
    # markup must never reach the voice.
    reply = strip_tool_markup(reply)
    if reply and not should_stop():
        for s in _SENT_END.split(reply):
            s = s.strip()
            if s and not should_stop():
                on_sentence(s)
    return reply, []


def _chat_once(messages: list, tools: list | None, st: dict,
               headers: dict, raw_log: bool = False) -> tuple[str, list[dict]]:
    """The same request, without streaming: one JSON reply.

    Used when a gateway streams nothing for a request that carries tools. The
    answer arrives whole, so the voice loses its sentence-by-sentence pacing —
    but a whole answer read aloud is worth more than a perfect silence.

    `raw_log` prints finish_reason and the raw message when the reply comes back
    empty: the UI promises "detalhes no terminal", and without this the terminal
    has nothing to show exactly when it matters."""
    r = requests.post(f"{st['base_url']}/chat/completions",
                      json=_payload(messages, tools, st, stream=False),
                      headers=headers, timeout=TIMEOUT)
    _raise_for_status(r)
    choices = r.json().get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    content = str(message.get("content") or "")[:MAX_REPLY_CHARS]
    calls = _tool_calls_from(message)
    if raw_log and not content and not calls:
        print(f"[Brain] {label(st)} empty reply — finish="
              f"{(choices[0] or {}).get('finish_reason')!r} "
              f"message={json.dumps(message, ensure_ascii=False)[:300]}")
    return content, calls


def _chat_stream_one(messages: list, tools: list | None, st: dict,
                     on_sentence: Callable[[str], None],
                     should_stop: Callable[[], bool]) -> tuple[str, list[dict]]:
    """One provider's streaming reply, in the OpenAI chat-completions dialect.

    Calls on_sentence() for each finished sentence as it arrives; returns
    (full_content, tool_calls) where tool_calls are
    [{"id", "name", "arguments": dict}]. Raises on HTTP errors."""

    if st.get("provider") == "qodercli":
        return _qodercli_turn(messages, st, on_sentence, should_stop)

    payload = _payload(messages, tools, st, stream=True)
    headers = {"Content-Type": "application/json"}
    if st["api_key"]:
        headers["Authorization"] = f"Bearer {st['api_key']}"
    if st["provider"] == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/FatihMakes/Mark-LV"
        headers["X-Title"] = "MARK LV"

    content, buf = "", ""
    frags: dict[int, dict] = {}
    with requests.post(f"{st['base_url']}/chat/completions", json=payload,
                       headers=headers, timeout=TIMEOUT, stream=True) as r:
        _raise_for_status(r)
        for raw in r.iter_lines():
            if should_stop():
                break
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                # Some OpenAI-compatible gateways stream the WHOLE text so far
                # in every chunk instead of the new bit. Appending that would
                # square the reply; keep only what is new.
                if content and len(piece) > len(content) and piece.startswith(content):
                    piece = piece[len(content):]
                if len(content) + len(piece) > MAX_REPLY_CHARS:
                    piece = piece[: max(0, MAX_REPLY_CHARS - len(content))]
                    if not piece:
                        break
                content += piece
                buf += piece
                # hand over finished sentences, keep the open tail
                parts = _SENT_END.split(buf)
                if len(parts) > 1:
                    for s in parts[:-1]:
                        s = s.strip()
                        if s:
                            on_sentence(s)
                    buf = parts[-1]
            for tc in delta.get("tool_calls") or []:
                i = int(tc.get("index", 0))
                f = frags.setdefault(i, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    f["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    f["name"] += fn["name"]
                if fn.get("arguments"):
                    f["arguments"] += fn["arguments"]
    tail = buf.strip()
    if tail and not should_stop():
        on_sentence(tail)
    calls = []
    for i in sorted(frags):
        f = frags[i]
        try:
            args = json.loads(f["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if f["name"]:
            calls.append({"id": f["id"] or f"call_{i}", "name": f["name"],
                          "arguments": args if isinstance(args, dict) else {}})

    # A gateway can accept a request carrying tools and then stream an empty
    # completion: role delta, end_turn, nothing between them. The Qoder proxy
    # does exactly this while answering the same request without streaming, so
    # ask again that way instead of losing the turn.
    if not content and not calls and tools and not should_stop():
        print(f"[Brain] {label(st)} streamed an empty completion for a request "
              f"with tools — asking again without streaming.")
        content, calls = _chat_once(messages, tools, st, headers, raw_log=True)
        for piece in _SENT_END.split(content):
            piece = piece.strip()
            if piece:
                on_sentence(piece)

    # Still nothing: a local model that is being reloaded (context changed,
    # another model took the card) answers the request made in that instant with
    # an empty body. One short pause and one more ask costs a second and turns
    # "ERR: answered with an empty completion" into a normal turn.
    if not content and not calls and not should_stop():
        time.sleep(0.8)
        print(f"[Brain] {label(st)} answered empty — asking one last time.")
        content, calls = _chat_once(messages, tools, st, headers, raw_log=True)
        for piece in _SENT_END.split(content):
            piece = piece.strip()
            if piece:
                on_sentence(piece)

    return content, calls


# ── vision: local model → what is in this image ────────────────────────────

# qwen2.5vl:3b no Ollama vem com num_ctx=4096 — uma screenshot Retina do
# MacBook (5K) estoura isso sozinha (4099 tokens só de imagem), e o modelo
# ou devolve erro 400 ou alucina `?????`. Thumbnailar antes de mandar
# resolve sem exigir mais VRAM: 1280×720 dá ~1200 tokens de visão, sobra
# espaço pra pergunta e resposta.
_VL_MAX_W = 1280
_VL_MAX_H = 720
_VL_JPEG_Q = 82


def _shrink_for_vision(image: bytes, mime: str) -> tuple[bytes, str]:
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image)).convert("RGB")
        img.thumbnail((_VL_MAX_W, _VL_MAX_H), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_VL_JPEG_Q, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  compressão p/ VL falhou ({e}); mandando original")
        return image, mime


def vision_describe(image: bytes, mime: str, question: str, st: dict) -> str:
    """Ask the LOCAL vision model what is in the image.

    Same Ollama, same machine: the screenshot never leaves it, which is the
    whole point of a local brain. `keep_alive` is short on purpose — a VL model
    parked in 8 GB of video memory would evict the conversation model.

    The image is thumbnailed to 1280×720 JPEG first — see the comment above
    for why. Callers pass raw PNG bytes and don't have to think about it.
    """
    import base64
    image, mime = _shrink_for_vision(image, mime)
    payload = {
        "model": st.get("vision_model") or "qwen2.5vl:3b",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(image).decode()}},
        ]}],
        "max_tokens": 400,
        "keep_alive": "120s",
    }
    r = requests.post(st.get("vision_base_url", st["base_url"]).rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {st.get('api_key') or 'ollama'}"},
                      json=payload, timeout=180)
    _raise_for_status(r)
    return str(r.json()["choices"][0]["message"].get("content") or "").strip()


# ── router: a cheap model decides which tools a turn needs ───────────────────

ROUTER_TIMEOUT = 8.0

# The system prompt is deliberately tiny and has no tool SCHEMAS — only names
# and one-liners. That is what keeps the call to ~1k tokens and makes even a
# small model reliable: it is answering a narrow "which of these, if any" "
# question, not driving the tools itself.
_ROUTER_SYSTEM = (
    "You are a router for a voice assistant. Given the user's latest message "
    "and the tools available, decide what the assistant needs to do. Reply with "
    "ONLY a JSON object, no prose: {\"act\": true|false, \"tools\": [names]}. "
    "act=false and tools=[] when the user is only greeting, thanking, declining, "
    "confirming, or commenting on something already done — no computer action "
    "is wanted. When in doubt about whether they want an action, choose "
    "act=false. When act=true, list the one or two tools most likely needed. "
    "ESCALATION: if the request goes beyond what these specific tools can do — "
    "installing software, changing system configuration that needs admin/root, "
    "editing code or config files, writing a script, a multi-step setup, or the "
    "user explicitly says the assistant cannot do it or asks for 'Qoder' / 'o "
    "coder' — choose `root_exec` when a single command run as root would do it "
    "(installing/removing packages, editing /etc, restarting a system service), "
    "otherwise choose `qoder_agent` for code or multi-step work. "
    "ALSO TRANSCRIBE: the user's message arrived through speech recognition and "
    "may contain words it misheard. Copy it into \"text\" with ONLY those words "
    "corrected, using the conversation so far to know what they meant — names, "
    "product names and the vocabulary of the tools above are the usual victims. "
    "Keep the same language, meaning, person, tense and level of detail. Never "
    "answer it, never summarise, never translate, never add a request that is "
    "not there. If nothing is clearly misheard, copy it exactly as it came. "
    "Reply with ONLY: {\"act\": true|false, \"tools\": [names], \"text\": \"...\"} "
    "Tools available:\n"
)


def route_turn(text: str, catalog: str, st: dict,
               context: list[str] | None = None) -> tuple[bool | None, list[str], str]:
    """Ask the router model about one turn. Returns (act, tool_names, text).

    act is True (needs tools, names is the subset), False (conversation only),
    or None (router unavailable — the caller should fall back). Never raises:
    a router that is down must not take the assistant down with it.

    The third value is the user's sentence with the words the recogniser
    misheard corrected — the same request also carries the transcription job,
    so context-aware repair costs no extra round trip. It comes back as the
    original when the model had nothing to fix, or when its rewrite fails the
    sanity check in core.transcript.plausible().
    """
    r = st.get("router") or {}
    if not r.get("enabled") or not r.get("api_key"):
        return None, [], str(text or "")
    system = _ROUTER_SYSTEM + (catalog or "")
    if context:
        system += ("\nCONVERSATION SO FAR (oldest first):\n"
                   + "\n".join(line[:300] for line in context[-6:]) + "\n")
    payload = {
        "model": r["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": str(text or "")[:500]},
        ],
        "max_tokens": 220, "temperature": 0.0, "stream": False,
    }
    if "gpt-oss" in str(r.get("model") or ""):
        payload["reasoning_effort"] = "low"
    try:
        resp = requests.post(r["base_url"].rstrip("/") + "/chat/completions",
                             headers={"Authorization": f"Bearer {r['api_key']}",
                                      "Content-Type": "application/json"},
                             json=payload, timeout=ROUTER_TIMEOUT)
        _raise_for_status(resp)
        content = str(resp.json()["choices"][0]["message"].get("content") or "")
    except Exception as e:
        print(f"[Router] indisponível ({e}); usando fallback do chamador")
        return None, [], str(text or "")
    act, names, fixed = _parse_route(content)
    from core import transcript as _tr
    if not _tr.plausible(text, fixed):
        fixed = str(text or "")
    elif fixed != text:
        print(f"[STT] corrigido pelo contexto: {text[:70]!r} → {fixed[:70]!r}")
    return act, names, fixed


def _parse_route(content: str) -> tuple[bool | None, list[str], str]:
    """Pull {act, tools} out of the router's reply, tolerating ``` fences."""
    import re as _re
    m = _re.search(r"\{.*\}", content or "", _re.S)
    if not m:
        return None, [], ""
    try:
        j = json.loads(m.group(0))
    except Exception:
        return None, [], ""
    if not isinstance(j, dict) or "act" not in j:
        return None, [], ""
    act = bool(j.get("act"))
    raw = j.get("tools")
    names = [str(t).strip() for t in raw if str(t).strip()] if isinstance(raw, list) else []
    fixed = str(j.get("text") or "").strip()
    return act, names, fixed


# ── voice: Microsoft Edge TTS → 24 kHz int16 PCM ─────────────────────────────

def tts_pcm24k(text: str, voice: str) -> bytes:
    """Synthesise `text` and return mono int16 PCM at 24 kHz, the rate the
    playback queue already uses. Needs the `edge-tts` and `miniaudio` packages."""
    import edge_tts
    import miniaudio

    async def _synth() -> bytes:
        comm = edge_tts.Communicate(text, voice)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    loop = asyncio.new_event_loop()
    try:
        mp3 = loop.run_until_complete(_synth())
    finally:
        loop.close()
    if not mp3:
        return b""
    dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
                           nchannels=1, sample_rate=24000)
    return bytes(dec.samples)


# ── voice: Fish Audio → 24 kHz int16 PCM ─────────────────────────────────────

class FishCreditsExhausted(RuntimeError):
    """Fish Audio refused to synthesise because the account has no credits
    left. The caller falls back to another voice and switches brains."""


def _fish_out_of_credits(r) -> bool:
    if r.status_code == 402:
        return True
    if r.status_code in (400, 403):
        t = (r.text or "").lower()
        return any(w in t for w in ("credit", "balance", "insufficient", "quota"))
    return False


def tts_fish_pcm24k(text: str, st: dict) -> bytes:
    """Synthesise `text` with Fish Audio (POST /v1/tts) in the configured voice
    and return mono int16 PCM at 24 kHz. The audio is requested as WAV and
    decoded with miniaudio, so the playback path is the same as Edge's.

    The official SDK sends the body as MessagePack; the API also takes JSON.
    JSON is used here so no extra package is needed."""
    import miniaudio

    key = st.get("fish_api_key") or ""
    if not key:
        raise RuntimeError("Fish Audio API key missing (⚙ → PLUGIN SETTINGS → ECONOMY BRAIN)")
    body = {
        "text": text,
        "format": "wav",
        "sample_rate": 24000,
        "latency": "balanced",
        "chunk_length": 200,
        "normalize": True,
        # Low sampling temperature: the default (0.7) let a cloned voice
        # improvise on long spelled-out numbers, sometimes drifting into
        # another language. Pronunciation is not a place for creativity.
        "temperature": 0.3,
        "top_p": 0.6,
        "repetition_penalty": 1.2,
    }
    if st.get("fish_voice_id"):
        body["reference_id"] = st["fish_voice_id"]
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "model": st.get("fish_model") or FISH_MODELS[0],
    }
    r = requests.post(f"{FISH_BASE}/v1/tts", headers=headers, json=body, timeout=TIMEOUT)
    if r.status_code in (415, 422):
        # Server insisting on MessagePack (the SDK's encoding): retry that way
        # when the package is present.
        try:
            import ormsgpack
            headers["Content-Type"] = "application/msgpack"
            r = requests.post(f"{FISH_BASE}/v1/tts", headers=headers,
                              data=ormsgpack.packb(body), timeout=TIMEOUT)
        except ImportError:
            raise RuntimeError(f"Fish Audio refused the JSON request ({r.status_code}): "
                               f"{r.text[:120]} — run: py -m pip install ormsgpack")
    if _fish_out_of_credits(r):
        raise FishCreditsExhausted(f"Fish Audio: no credits left on the account ({r.status_code})")
    if r.status_code == 401:
        raise RuntimeError("Fish Audio rejected the API key (401)")
    if r.status_code == 404 and st.get("fish_voice_id"):
        raise RuntimeError(f"Fish Audio: voice {st['fish_voice_id']} not found (404)")
    r.raise_for_status()
    data = r.content
    if not data:
        return b""
    dec = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16,
                           nchannels=1, sample_rate=24000)
    return bytes(dec.samples)


_FISH_SPLIT = re.compile(r"(?<=[,;:])\s+")
_FISH_GAP = b"\x00" * int(24000 * 0.22) * 2      # 220 ms of silence between pieces


def _fish_pieces(text: str) -> list[str]:
    """A long sentence with figures goes to Fish in short pieces, split at
    commas — each piece is a thing the voice can say in one breath, and the
    model never has to hold "trezentos e oitenta e um mil, novecentos e
    oitenta e nove reais em 896 vendas, de 170 clientes" in its head at once.
    Short sentences go whole."""
    text = (text or "").strip()
    if len(text) < 70 or not any(ch.isdigit() for ch in text) and " mil" not in text and " reais" not in text:
        return [text] if text else []
    pieces, cur = [], ""
    for part in _FISH_SPLIT.split(text):
        if cur and len(cur) + len(part) < 60:
            cur = f"{cur} {part}"
        else:
            if cur:
                pieces.append(cur)
            cur = part
    if cur:
        pieces.append(cur)
    return pieces


def refresh_fish_voice(st: dict) -> dict:
    """Re-read the Fish voice from config, in place, before each synthesis.

    `st` is a snapshot taken when the economy session connects, and
    synth_pcm24k() receives it for every sentence. Without this the id typed in
    the customise form would only be heard after a restart — the one thing the
    field must not do. A key that is present but empty is meaningful: it means
    "use the model's default voice", so it is copied through as "".
    """
    try:
        from memory.config_manager import get_plugin_config
        cfg = get_plugin_config(NAMESPACE)
    except Exception:
        return st
    if "fish_voice_id" in cfg:
        st["fish_voice_id"] = fish_voice_id(cfg.get("fish_voice_id"))
    model = str(cfg.get("fish_model") or "").strip()
    if model in FISH_MODELS:
        st["fish_model"] = model
    return st


def synth_pcm24k(text: str, st: dict) -> bytes:
    """One sentence → 24 kHz PCM with whichever voice engine is configured."""
    if st.get("tts_engine") == "fish":
        refresh_fish_voice(st)
        pieces = _fish_pieces(text)
        if len(pieces) <= 1:
            return tts_fish_pcm24k(text, st)
        out = bytearray()
        for i, piece in enumerate(pieces):
            if i:
                out.extend(_FISH_GAP)
            out.extend(tts_fish_pcm24k(piece, st))
        return bytes(out)
    return tts_pcm24k(text, st["tts_voice"])


def fish_balance(key: str) -> float | None:
    """Current API credit balance, or None when it cannot be read."""
    try:
        r = requests.get(f"{FISH_BASE}/wallet/self/api-credit",
                         headers={"Authorization": f"Bearer {key}"}, timeout=15)
        if r.ok:
            c = r.json().get("credit")
            return float(c) if c is not None else None
    except Exception:
        pass
    return None


def fish_credits(key: str) -> str:
    """Best-effort balance line for TEST CONNECTION ("" if unavailable)."""
    b = fish_balance(key)
    return f"credits: {b:.2f}" if b is not None else ""


def missing_packages(st: dict | None = None) -> list[str]:
    out = []
    need = [("miniaudio", "miniaudio")]
    if not st or st.get("tts_engine") != "fish":
        need.insert(0, ("edge_tts", "edge-tts"))
    for mod, pkg in need:
        try:
            __import__(mod)
        except Exception:
            out.append(pkg)
    return out


def test_connection(values: dict):
    """⚙ TEST CONNECTION: key accepted, model listed, voice packages present."""
    fake = dict(values)
    st = settings(lambda ns: fake)
    notes = []
    missing = missing_packages(st)
    if missing:
        notes.append("voice packages missing — run: py -m pip install " + " ".join(missing))
    if st["tts_engine"] == "fish":
        if not st["fish_api_key"]:
            return False, "Voice is Fish Audio but its API key is empty (fish.audio → API keys)."
        if not missing:
            try:
                pcm = tts_fish_pcm24k("Teste de voz.", st)
                secs = len(pcm) / 48000.0
                if fish_model_is_free(st):
                    notes.append(f"Fish Audio voice OK ({secs:.1f} s, free model — "
                                 "no credits spent)")
                else:
                    bal = fish_credits(st["fish_api_key"])
                    notes.append(f"Fish Audio voice OK ({secs:.1f} s of audio"
                                 + (f", {bal}" if bal else "") + ")")
            except Exception as e:
                return False, f"Fish Audio voice failed: {e}"
    # Every rung, in the order the brain will use them — so a green button here
    # means "the chain works", not just "the first one does".
    lines: list[str] = []
    first_ok = True
    for i, rung in enumerate(st["chain"]):
        ok, note = _probe(rung)
        prefix = "OK" if i == 0 else "fallback"
        lines.append(f"{prefix} {label(rung)}: {note}")
        if i == 0 and not ok:
            first_ok = False
    if notes:
        lines.append("; ".join(notes))
    return (not missing) and first_ok, "  |  ".join(lines)


def _probe(rung: dict) -> tuple[bool, str]:
    """Can this provider list models and answer a tiny tool call? (ok, note)."""
    headers = {"Authorization": f"Bearer {rung['api_key']}"} if rung.get("api_key") else {}
    try:
        r = requests.get(f"{rung['base_url']}/models", headers=headers, timeout=15)
        if r.status_code == 401:
            return False, "the provider rejected the key (401)"
        r.raise_for_status()
        ids = {m.get("id") for m in (r.json().get("data") or [])}
        model_note = ""
        if ids and rung["model"] not in ids:
            model_note = f"model '{rung['model']}' is not in the provider's list; "
    except requests.ConnectionError:
        return False, "cannot connect (is the server running?)"

    # The list says the key works; only a real reply says the MODEL works for
    # this key (credits, access, tool support). One tiny request, with a tool
    # attached, because that is exactly what the assistant will send.
    probe_tool = [{"type": "function", "function": {
        "name": "noop", "description": "does nothing",
        "parameters": {"type": "object", "properties": {}}}}]
    msgs = [{"role": "system", "content": "Reply with the single word OK."},
            {"role": "user", "content": "Ready?"}]

    def _ask(with_tool: bool) -> tuple[str, list]:
        got: list[str] = []
        content, calls = _chat_stream_one(msgs, probe_tool if with_tool else None,
                                          rung, got.append, lambda: False)
        return (content or " ".join(got)).strip(), calls

    try:
        text, calls = _ask(True)
        if not text and not calls:
            # A model that goes quiet because a tool was offered is still a
            # working model — worth one more ask before calling it broken.
            text, calls = _ask(False)
        if text:
            return True, f"{model_note}answered \"{text[:40]}\""
        if calls:
            return True, f"{model_note}answered with a tool call ({calls[0]['name']})"
        return False, f"{model_note}accepted the request but returned no text"
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:200] if e.response is not None else str(e)[:200]
        except Exception:
            body = str(e)[:200]
        return False, (f"{model_note}the model refused: {body} "
                       "(402 = no credits, 404 = wrong model id)")
    except Exception as e:
        return False, f"{model_note}model test failed: {e}"

