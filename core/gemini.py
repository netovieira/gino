"""
One place where the assistant's one-shot Gemini calls are made.

WHY THIS EXISTS
    The live conversation runs on the Live API and is not what this file is
    about. Everything else — reading a screenshot, parsing a flight page,
    turning a request into a shell command, working out which WhatsApp button
    answers a call — was a separate `genai.Client(...)` built at the point of
    use, with the model name written inline. Sixteen files did it, twenty-six
    times, and not one of them set a timeout.

    That is not tidiness, it is three real faults:

    NO TIMEOUT.  The SDK waits forever by default. `gemini-flash-latest` spent
    an afternoon returning 504 DEADLINE_EXCEEDED, and every one of those calls
    became an unbounded hang — measured at ten seconds of silence while a phone
    rang, and worse elsewhere, because nothing was there to give up.

    NO FALLBACK.  One hardcoded alias meant that when that alias was unwell,
    the feature was simply gone. A ladder costs nothing when the first rung
    works and saves the feature when it does not.

    NO SINGLE PLACE TO CHANGE.  A new model release meant editing sixteen files
    and hoping none were missed.

THE LIVE MODEL DOES THIS WORK, AND IT LEADS THE LADDER
    Not the user's conversation — a separate, throwaway session per call, so
    nothing a plugin asks is ever heard by the person at the microphone.

    It leads because of quota. This is a voice assistant; the Live API is the
    dependency it already has, and it draws on a different pool from the text
    models. On the free tier it is the TEXT pool that runs out, and when it does
    every side call fails and the feature behind it dies with it. Putting Live
    first means ordinary use stops spending the pool that runs dry.

    The reply arrives through output_transcription, because these models refuse
    response_modalities=["TEXT"] with a 1007 — they only speak. That sounds
    fatal for structured output and is not: the transcription is the model's own
    text of what it said, and it returned "Mum ❤ click here for contact info",
    indented Python inside markdown fences, and src/utils/helpers_v2.py
    character for character.

    It cannot carry grounding metadata, so grounded web search stays on REST.

THE LADDER, MEASURED
    Live, one throwaway session:
        connect                     0.24s
        short structured JSON       1.7 - 2.8s
        2300 characters of code     3.39s, not truncated
        three concurrent sessions   all fine, 4.77s wall clock
    REST text models, same prompt, same afternoon:
        gemini-2.5-flash-lite       0.76s   ...then 429, quota exhausted
        gemini-2.5-flash            0.80s   ...then 429
        gemini-flash-lite-latest    2.58s
        gemini-flash-latest         504, every time
    So REST is two to four times quicker while it lasts, and the whole point of
    the ladder is that it does not last. Pinned REST names sit behind Live;
    rolling `-latest` aliases sit behind those, because they were the ones
    having a bad day.

    Where the answer depends only on stable input, cache it and neither pool is
    touched twice: `plugins/_whatsapp_core.py` is the worked example — one
    request per WhatsApp language for the lifetime of the install.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_KEY_FILE = _BASE / "config" / "api_keys.json"

# Ladders, tried left to right. Change a model HERE and the whole app follows.
FAST = "fast"      # short classification, extraction, one-line decisions
SMART = "smart"    # reasoning, generation, long documents, images
SEARCH = "search"  # grounded search — REST only, see below

# A rung that means "ask the Live model instead", through a short throwaway
# session rather than the REST text API.
#
# WHY IT LEADS
#     This is a voice assistant: the Live API is the dependency it already has,
#     and it draws on a DIFFERENT quota pool from the text models. On the free
#     tier the text pool is the one that runs out — an afternoon of ordinary use
#     exhausts it, and when it does, every one of these side calls fails and the
#     feature behind it dies. The Live pool is untouched by that.
#
# WHAT IT COSTS, MEASURED
#     connect                      0.24s
#     short structured JSON        1.7 - 2.8s   (REST: 0.76s)
#     2300 characters of code      3.39s, not truncated
#     three concurrent sessions    all fine, 4.77s wall clock
#     So it is two to four times slower than REST when REST is available, and
#     infinitely faster than REST when REST is out of quota.
#
# THE THING WORTH KNOWING
#     These models only speak — response_modalities=["TEXT"] is refused with a
#     1007. The reply comes back through output_transcription, which sounds like
#     it would mangle anything structured. It does not: it is the model's own
#     text of what it said, and it survived "Mum ❤ click here for contact info",
#     indented Python inside markdown fences, and src/utils/helpers_v2.py
#     character for character. That is what makes this usable at all.
#
#     What it cannot carry is grounding metadata, so grounded web search stays
#     on REST — see SEARCH.
LIVE = "live"

# Every model this key can reach, in the order to try them. A rung that runs
# out of quota is skipped for a while and the next one answers — which is the
# whole point: one model a day means one daily limit, a ladder means the sum of
# them. Names verified against the live model list rather than guessed.
# Order is measured, not assumed. Timed against this key, same prompt:
#     gemini-3.5-flash-lite      0.56s      gemini-2.5-flash        0.67s
#     gemini-3.1-flash-lite      0.60s      gemini-2.5-flash-lite   0.74s
#     gemini-flash-lite-latest   0.60s      gemini-3.5-flash        1.13s
#     gemini-3-flash-preview     504, after 14.7s of waiting
#     gemini-3.6-flash           504, after 12.0s
#     gemini-flash-latest        503 UNAVAILABLE
# The three that fail sit at the BOTTOM rather than being deleted: they are
# real quota when they are healthy, and a rung that is down is set aside by the
# cooldown after one attempt instead of being paid for on every call.
_LADDERS = {
    FAST: (LIVE,
           "gemini-2.5-flash-lite", "gemini-3.5-flash-lite",
           "gemini-3.1-flash-lite", "gemini-flash-lite-latest",
           "gemini-2.5-flash", "gemini-3.5-flash",
           "gemini-3.6-flash", "gemini-3-flash-preview"),
    SMART: (LIVE,
            "gemini-2.5-flash", "gemini-3.5-flash",
            "gemini-2.5-flash-lite", "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-flash-latest"),
    # Grounded search needs response.candidates[...].grounding_metadata, which a
    # Live turn does not produce. REST only, and it says so rather than silently
    # returning an answer with no sources behind it.
    # gemini-3.8-flash is deliberately NOT here: at $0.75/M input it costs
    # several times 2.5 Flash for the same one-shot jobs. Opt in with
    # "gemini_text_models": ["gemini-3.8-flash"] in config/api_keys.json.
    SEARCH: ("gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-flash-lite",
             "gemini-flash-latest"),
}

# The conversation's own model, and ONE careful fallback behind it.
#
# Deliberately not a long ladder. The Live quota is not the one that runs out —
# the text models are — and Live models are not interchangeable the way text
# models are: they differ in which config fields they accept and in what they
# can do, so falling through a list of them risks connecting to something that
# behaves like a different assistant. The key also offers transcribe-live,
# live-translate and a robotics streaming model, none of which are assistants
# at all.
#
# So there are two: the current one, and the native-audio model this assistant
# used before it, which is known to work here. A mismatch in config is already
# survivable — the connect loop drops the tuning and proactive-audio fields and
# reconnects when the server rejects them.
LIVE_MODELS = (
    # Gemini 3.8 Live (GA, Sept 2026) is the audio-to-audio model Google's docs
    # name as the replacement for the 3.1 preview; the two older ones stay as
    # the fallback for a key that cannot use it yet.
    "models/gemini-3.8-live",
    "models/gemini-3.1-flash-live-preview",
    "models/gemini-2.5-flash-native-audio-preview-12-2025",
)


# ── User overrides from config/api_keys.json ─────────────────────────────────
# Two optional keys let a newer model be tried without editing this file:
#
#   "gemini_live_models": ["gemini-3.8-flash-live-preview"]
#       Put in FRONT of LIVE_MODELS for the voice session. Must be a Live /
#       native-audio model; a name the key cannot use is a 404 on connect, and
#       main.py's ladder then steps down to the shipped models by itself.
#
#   "gemini_text_models": ["gemini-3.8-flash"]
#       Put in FRONT of every one-shot ladder (search, vision, summaries). An
#       unknown name is set aside by the cooldown after one failed call.
#
# Names may be given with or without the "models/" prefix.
def _user_models(key: str, prefix: str) -> tuple:
    try:
        import json
        data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        raw = data.get(key)
    except Exception:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    out = []
    for m in raw:
        m = str(m or "").strip()
        if not m:
            continue
        m = m.removeprefix("models/")
        out.append(prefix + m)
    return tuple(out)


_u_live = _user_models("gemini_live_models", "models/")
if _u_live:
    LIVE_MODELS = _u_live + tuple(m for m in LIVE_MODELS if m not in _u_live)
    print(f"[Gemini] Live models from config first: {', '.join(_u_live)}")

_u_text = _user_models("gemini_text_models", "")
if _u_text:
    for _tier, _ladder in list(_LADDERS.items()):
        _head = _ladder[:1] if _ladder and _ladder[0] == LIVE else ()
        _rest = tuple(m for m in _ladder[len(_head):] if m not in _u_text)
        _LADDERS[_tier] = _head + _u_text + _rest
    print(f"[Gemini] Text models from config first: {', '.join(_u_text)}")

# The Live model to use for one-shot calls. main.py owns the real one; this is
# only the fallback for when this module is imported without it (tests).
_LIVE_FALLBACK = "models/gemini-3.8-live"

# How many one-shot Live sessions may exist at once.
#
# THE USER'S CONVERSATION OUTRANKS EVERY SIDE CALL.
# Nothing here can reach the microphone or the speaker — main.py has exactly one
# `.receive()` and it is bound to its own session, and audio only reaches the
# speaker through that one loop — so a side call cannot answer the user or talk
# over the reply. Verified alongside a live main session: four side calls fired
# while it was connected, each got its own answer, and the main session replied
# correctly both before and after with no errors.
#
# What a side call CAN do is take up a concurrent-session slot. That is the one
# way it could hurt the conversation, so it is capped, and a call that cannot
# get a slot quickly does not queue behind the others — it falls to the REST
# rung, which is what the ladder is for.
# Three, from the measurement: four side calls plus the conversation ran
# together without complaint, so three leaves the conversation a slot in hand
# while still covering any burst this app actually produces — tool calls run one
# after another, and the screen agent's loop is sequential.
_LIVE_SLOTS = threading.BoundedSemaphore(3)
_LIVE_SLOT_WAIT = 3.0

_ONE_SHOT_SYSTEM = (
    "You are a data-processing function, not an assistant and not in a "
    "conversation. There is no person listening to you. Produce exactly the "
    "output the request asks for and nothing else: no greeting, no "
    "acknowledgement, no 'Understood', no explanation, no closing remark, no "
    "restating of the question. If the request asks for JSON, emit only the "
    "JSON. If it asks for code, emit only the code. If it asks for one word, "
    "emit that one word. Preserve the exact spelling, punctuation, capitals "
    "and whitespace of anything you are asked to copy or return."
)

# Milliseconds. Not a preference: the API rejects anything under ten seconds
# with "Minimum allowed deadline is 10s", so this is the tightest bound it will
# accept. Callers with a long job (a whole document, a big image) pass more.
DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 10_000

_key_lock = threading.Lock()
_cached_key: str | None = None

# A rung that answered 429 is out of quota, and on the free tier it will stay
# that way for a while. Retrying it on every single call is a wasted round trip
# in front of every request the assistant makes — measured on this key, the
# lite rung was 429ing continuously, so every call was paying for it before
# reaching the model that could actually answer. Remembering that for a few
# minutes turns the ladder from a cost into a saving.
_COOLDOWN_SECONDS = 300
# A model that does not exist, or that this key may not use, is not coming back
# in five minutes. Retrying it on every call is a wasted round trip in front of
# everything the assistant does, so it is set aside for the session instead.
_GONE_SECONDS = 6 * 60 * 60
# Overloaded or timing out. Usually passes, so a shorter rest than "gone" —
# but long enough that the fourteen-second wait is paid once, not repeatedly.
_UNAVAILABLE_SECONDS = 30 * 60
_cooldown: dict[str, float] = {}
_cool_lock = threading.Lock()


def _cool(model: str, seconds: float = _COOLDOWN_SECONDS) -> None:
    with _cool_lock:
        _cooldown[model] = time.monotonic() + seconds


def is_quota_error(err: str) -> bool:
    return "429" in err or "RESOURCE_EXHAUSTED" in err


def is_unavailable_error(err: str) -> bool:
    """The model is up but not answering — overloaded, or a deadline expired.

    Worth its own case because of what it costs: a 504 from one of these took
    fourteen seconds to arrive. Retrying that on every call puts the wait in
    front of everything the assistant does, so a rung that times out is rested
    like an exhausted one — for less long, since it is usually passing.
    """
    low = err.lower()
    return ("503" in err or "504" in err
            or "unavailable" in low or "deadline_exceeded" in low)


def is_gone_error(err: str) -> bool:
    """The model is not there, or not ours to use — a different thing from busy."""
    low = err.lower()
    return ("404" in err or "not found" in low or "is not supported" in low
            or "permission" in low or "403" in err)


def live_model() -> str:
    """The Live model to open the conversation with: the first rung that is not
    resting. If every one is resting the ladder is used from the top anyway —
    refusing to connect at all is never the better answer."""
    for m in LIVE_MODELS:
        if not _cooling(m):
            return m
    return LIVE_MODELS[0]


def note_live_failure(model: str, err: str) -> bool:
    """Record why a Live model failed. True when it is worth trying the next.

    Only quota and availability move the ladder along. A bad API key or a
    dropped network is not the model's fault, and stepping down the ladder for
    those would work through every model and reach the same wall four times.
    """
    if is_quota_error(err):
        _cool(model, _COOLDOWN_SECONDS)
        print(f"[Gemini] Live model {model} is out of quota — "
              f"switching for {_COOLDOWN_SECONDS // 60} minutes.")
        return True
    if is_gone_error(err):
        _cool(model, _GONE_SECONDS)
        print(f"[Gemini] Live model {model} is unavailable to this key — "
              f"setting it aside.")
        return True
    return False


def _cooling(model: str) -> bool:
    with _cool_lock:
        until = _cooldown.get(model, 0.0)
        if until and time.monotonic() < until:
            return True
        _cooldown.pop(model, None)
        return False


# ── Several Gemini accounts, one assistant ───────────────────────────────────
# config/api_keys.json may carry more than one key:
#
#   "gemini_api_key":  "AIza...primary",
#   "gemini_api_keys": ["AIza...second", "AIza...third"]
#
# They are used in that order. When the active one runs out of quota or
# prepaid credit (or is rejected), main.py calls rotate_key() and reconnects on
# the next; every one-shot call follows, because api_key() always answers with
# the active key. With a single key, rotation is a no-op and the model ladder
# does what it did before.
_key_list: list[str] = []
_key_idx: int = 0


def _load_keys() -> list[str]:
    """Primary first, then the fallbacks, in this order of sources:
    "gemini_api_key" (the key the setup screen writes), the ⚙ → PLUGIN
    SETTINGS → GEMINI ACCOUNTS form (plugin_config.gemini.secondary_key /
    tertiary_key), then the hand-edited "gemini_api_keys" list."""
    try:
        data = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = [data.get("gemini_api_key")]
    pc = data.get("plugin_config")
    g = pc.get("gemini") if isinstance(pc, dict) else None
    if isinstance(g, dict):
        raw += [g.get("secondary_key"), g.get("tertiary_key")]
    extra = data.get("gemini_api_keys")
    if isinstance(extra, str):
        extra = [extra]
    if isinstance(extra, list):
        raw += extra
    out: list[str] = []
    for k in raw:
        k = str(k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def api_keys(refresh: bool = False) -> list[str]:
    """Every configured key, primary first. Cached; never raises."""
    global _key_list
    with _key_lock:
        if not _key_list or refresh:
            fresh = _load_keys()
            if fresh != _key_list:
                _key_list = fresh
        return list(_key_list)


# Primary-first with rest periods. The primary is always preferred; a fallback
# is used ONLY while the primary (and any fallback before it) is resting after
# a quota, credit or rejection failure. Rest periods, in seconds:
KEY_REST_QUOTA = 30 * 60        # 429 / RESOURCE_EXHAUSTED: per-minute or daily limits
KEY_REST_CREDIT = 2 * 60 * 60   # prepaid balance gone: worth a retry, but not often
KEY_REST_REJECTED = 6 * 60 * 60 # "API key not valid": will not fix itself soon
_key_rest: dict[str, float] = {}   # key → monotonic time its rest ends


def _resting(key: str) -> bool:
    until = _key_rest.get(key, 0.0)
    if until and time.monotonic() < until:
        return True
    _key_rest.pop(key, None)
    return False


def api_key(refresh: bool = False) -> str:
    """The key to use NOW: the first configured one that is not resting — so
    the primary, unless it recently failed. Cached; never raises."""
    global _cached_key, _key_idx
    keys = api_keys(refresh=refresh or not _key_list)
    with _key_lock:
        if not keys:
            _cached_key = ""
            return ""
        for i, k in enumerate(keys):
            if not _resting(k):
                _key_idx, _cached_key = i, k
                return k
        # Everyone is resting: the primary is the best bad option, and trying
        # it clears its rest if it answers.
        _key_rest.pop(keys[0], None)
        _key_idx, _cached_key = 0, keys[0]
        return keys[0]


def key_count() -> int:
    return len(api_keys())


def key_index() -> int:
    """1-based position of the active key, for log lines."""
    return (_key_idx % max(1, key_count())) + 1


def rotate_key(reason: str = "") -> int:
    """The active key just failed: rest it and hand over to the next one that
    is not resting. Returns the new key's 1-based index, or 0 when there is
    no other key to go to (single key, or every other key is resting too)."""
    global _key_idx, _cached_key
    keys = api_keys(refresh=True)
    if len(keys) < 2:
        return 0
    low = (reason or "").lower()
    rest = (KEY_REST_REJECTED if "reject" in low or "invalid" in low else
            KEY_REST_CREDIT if "credit" in low else KEY_REST_QUOTA)
    with _key_lock:
        cur = _cached_key if _cached_key in keys else keys[_key_idx % len(keys)]
        _key_rest[cur] = time.monotonic() + rest
        nxt = next(((i, k) for i, k in enumerate(keys) if not _resting(k)), None)
        if nxt is None:
            return 0
        _key_idx, _cached_key = nxt
        n = _key_idx + 1
    which = "primary" if n == 1 else f"fallback {n - 1}"
    print(f"[Gemini] Account {keys.index(cur) + 1} resting for {rest // 60} min"
          f" — switching to {which} (account {n} of {len(keys)})"
          + (f" — {reason}" if reason else ""))
    return n


def key_status() -> str:
    """One line for logs: which account is active and which are resting."""
    keys = api_keys()
    if not keys:
        return "no Gemini key configured"
    bits = []
    for i, k in enumerate(keys):
        tag = "primary" if i == 0 else f"fallback {i}"
        if _resting(k):
            mins = int((_key_rest[k] - time.monotonic()) // 60) + 1
            bits.append(f"{tag}: resting {mins} min")
        else:
            bits.append(f"{tag}: {'active' if k == _cached_key else 'ready'}")
    return "; ".join(bits)


def is_credit_error(err: str) -> bool:
    """The account's prepaid balance is gone — a billing wall, not a quota."""
    low = err.lower()
    return ("prepayment" in low or "credits are depleted" in low
            or "billing" in low or "1011" in err)


def client(timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """A configured genai.Client with a deadline on it. Raises if there is no
    key, because a caller that cannot work without one should say so."""
    from google import genai
    from google.genai import types as gtypes

    key = key or api_key()
    if not key:
        raise RuntimeError("no Gemini API key is configured")
    return genai.Client(
        api_key=key,
        http_options=gtypes.HttpOptions(timeout=max(MIN_TIMEOUT_MS, int(timeout_ms))),
    )


class _Reply:
    """What a Live turn hands back, shaped like the REST response's `.text` so
    every existing call site keeps working unchanged."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _live_model() -> str:
    """Whatever main.py is running, so upgrading the assistant upgrades this."""
    return getattr(sys.modules.get("main"), "LIVE_MODEL", None) or _LIVE_FALLBACK


def _to_live_parts(contents) -> list:
    """REST `contents` -> Live `parts`. Accepts a bare string, a list of
    strings, and the SDK's Part objects (which is how every image is passed
    here), because those are the three shapes the call sites actually use."""
    import base64

    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append({"text": item})
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None) or "application/octet-stream"
            if isinstance(data, bytes):
                data = base64.b64encode(data).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append({"text": txt})
            continue
        if isinstance(item, dict):
            parts.append(item)
    return parts


async def _live_turn(parts: list, system: str, key: str, timeout_s: float) -> str:
    from google import genai
    from google.genai import types as gtypes

    cl = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    # Silence the persona, or it answers instead of complying.
    #
    # These are conversational models and they behave like it: asked "Reply with
    # one word: ok" a Live turn came back with "Understood." — it treated the
    # instruction as something to acknowledge rather than something to do. The
    # REST models do not, because nobody ever taught them to be in a
    # conversation. Every call through this module wants a value, not a reply,
    # so the session is told what it is before it is told what to do.
    kwargs = {
        "response_modalities": ["AUDIO"],
        "output_audio_transcription": {},
        "system_instruction": _ONE_SHOT_SYSTEM + (f"\n\n{system}" if system else ""),
    }

    cm = cl.aio.live.connect(model=_live_model(),
                             config=gtypes.LiveConnectConfig(**kwargs))
    session = await asyncio.wait_for(cm.__aenter__(), 30)
    try:
        await session.send_client_content(
            turns={"role": "user", "parts": parts}, turn_complete=True)
        chunks: list[str] = []

        async def drain():
            async for resp in session.receive():
                sc = getattr(resp, "server_content", None)
                if sc and sc.output_transcription and sc.output_transcription.text:
                    chunks.append(sc.output_transcription.text)

        await asyncio.wait_for(drain(), timeout=timeout_s)
        # The transcription can trail the audio turn by a beat; a short second
        # drain stops a reply being cut mid-token. chat_takeover learned this
        # the same way and for the same reason.
        try:
            await asyncio.wait_for(drain(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        return "".join(chunks).strip()
    finally:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


def _live_call(contents, config, timeout_ms: int, key: str):
    """One throwaway Live session, run on its own loop in its own thread.

    A dedicated thread rather than asyncio.run() on the caller's: these are
    invoked from plugin executor threads, from UI worker threads and from
    main.py's own event loop, and asyncio.run() inside a thread that already has
    a running loop raises. Its own thread has no loop to collide with, wherever
    it was called from.

    Reconnecting costs 0.24s, so nothing is kept alive between calls — no
    session lifetime cap to manage, no GoAway to handle, no shared state.
    """
    system = ""
    if config is not None:
        system = getattr(config, "system_instruction", None) or \
            (config.get("system_instruction") if isinstance(config, dict) else "") or ""

    parts = _to_live_parts(contents)
    if not parts:
        return None

    if not _LIVE_SLOTS.acquire(timeout=_LIVE_SLOT_WAIT):
        # Every slot is busy. Do not wait it out: falling to REST costs less
        # than holding a session the user's conversation might want.
        raise RuntimeError("no free Live slot — leaving them for the conversation")

    box: dict = {}

    def runner():
        try:
            box["text"] = asyncio.run(
                _live_turn(parts, str(system), key, max(10.0, timeout_ms / 1000.0)))
        except BaseException as e:                     # noqa: BLE001
            box["error"] = e

    try:
        th = threading.Thread(target=runner, daemon=True, name="gemini-live-oneshot")
        th.start()
        th.join(timeout=max(15.0, timeout_ms / 1000.0 + 20.0))
    finally:
        _LIVE_SLOTS.release()
    if "error" in box:
        raise box["error"]
    text = box.get("text")
    return _Reply(text) if text else None


def _key_usable(key: str) -> bool:
    """Is this a key worth calling Gemini with? A Gemini-free install stores a
    placeholder there ("economy-mode-no-gemini"), and calling with it is a 401
    that takes a second to arrive on every single side call."""
    k = (key or "").strip()
    return len(k) >= 20 and not k.lower().startswith("economy-mode")


def _qoder_call(contents, timeout_ms: int):
    """The rung for an install with no Gemini key: ask the local Qoder proxy.

    Every side call in the app goes through call(), so routing it here means
    the screenshot reader, the file summariser and the dev agent keep working
    when the conversation itself runs on Qoder. Returns a _Reply, or None when
    Qoder is not reachable — the caller then reports what it did before.
    """
    try:
        from core import qoder
    except Exception:
        return None
    prompt = qoder.flatten(contents)
    if not prompt.strip():
        return None
    try:
        text = qoder.ask(prompt, max_tokens=2048, temperature=0.2,
                         timeout=max(30.0, timeout_ms / 1000.0))
    except Exception as e:
        print(f"[Gemini] no Gemini key, and Qoder could not answer: {e}")
        return None
    return _Reply(text) if text else None


def call(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = ""):
    """Run one generation, walking the ladder until one answers.

    Returns the SDK's own response object, so callers that need more than the
    text — grounding metadata, candidates, usage — still get it. Returns None
    when every model on the ladder failed; the reason for each is printed, since
    a silent None during a session nobody can debug is how the original problem
    stayed hidden.
    """
    # `tier` is normally FAST or SMART. Anything else is taken to be an explicit
    # model name — screen_agent lets the user pick one in its settings — and it
    # is tried first, with the reasoning ladder behind it. So a user's choice is
    # honoured, and a user's choice that is having an outage still degrades to
    # something that answers instead of to nothing.
    ladder = _LADDERS.get(tier)
    if ladder is None:
        ladder = (tier,) + tuple(m for m in _LADDERS[SMART] if m != tier)

    resolved_key = key or api_key()
    if not _key_usable(resolved_key):
        reply = _qoder_call(contents, timeout_ms)
        if reply is not None:
            return reply
        print("[Gemini] no Gemini API key is configured")
        return None

    try:
        from core import usage as _usage
        _usage.note("gemini", calls=1)
    except Exception:
        pass

    cl = None
    tried = [m for m in ladder if not _cooling(m)] or list(ladder)
    for model in tried:
        try:
            if model == LIVE:
                reply = _live_call(contents, config, timeout_ms, resolved_key)
                if reply is not None:
                    return reply
                raise RuntimeError("the Live turn came back empty")
            if cl is None:
                cl = client(timeout_ms=timeout_ms, key=resolved_key)
            kwargs = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return cl.models.generate_content(**kwargs)
        except Exception as e:
            msg = str(e)
            if is_quota_error(msg):
                _cool(model)
                print(f"[Gemini] {model}: out of quota — skipping it for "
                      f"{_COOLDOWN_SECONDS // 60} minutes")
            elif is_gone_error(msg):
                _cool(model, _GONE_SECONDS)
                print(f"[Gemini] {model}: unavailable to this key — set aside")
            elif is_unavailable_error(msg):
                _cool(model, _UNAVAILABLE_SECONDS)
                print(f"[Gemini] {model}: not answering — resting it for "
                      f"{_UNAVAILABLE_SECONDS // 60} minutes")
            else:
                print(f"[Gemini] {model}: {type(e).__name__}: {msg[:140]}")
    return None


def text(contents, tier: str = FAST, config=None,
         timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default: str = "") -> str:
    """`call`, reduced to the reply text. `default` when nothing answered."""
    resp = call(contents, tier=tier, config=config,
                timeout_ms=timeout_ms, key=key)
    if resp is None:
        return default
    return (getattr(resp, "text", None) or "").strip() or default


def as_json(contents, tier: str = FAST, config=None,
            timeout_ms: int = DEFAULT_TIMEOUT_MS, key: str = "", default=None):
    """`text`, parsed as JSON, tolerating the fences and prose a model wraps it
    in. `default` when nothing answered or the answer would not parse."""
    raw = text(contents, tier=tier, config=config, timeout_ms=timeout_ms, key=key)
    if not raw:
        return default
    if "{" in raw and "}" in raw:
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
    elif "[" in raw and "]" in raw:
        raw = raw[raw.find("["): raw.rfind("]") + 1]
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[Gemini] reply was not JSON: {e}")
        return default
