"""
core/audio_devices.py — pick which microphone and which speakers JARVIS uses.

WHY
    Both audio streams in main.py were opened without a `device=` argument, so
    they always took whatever the operating system called "default". On a laptop
    with a built-in mic, a webcam mic and a headset that is a coin toss — and on
    Windows the default *moves on its own* the moment you plug a headset in.
    "JARVIS can't hear me" almost always means "JARVIS is listening to the
    monitor's microphone".

WHY NAMES, NOT INDICES
    sounddevice identifies devices by integer index, and those indices shift
    whenever a device appears or disappears. Storing index 3 means that after
    unplugging a USB interface the saved setting silently points at something
    else. We store the device *name* and resolve it to an index at open time.

WHY THIS IS CACHED
    `sd.query_devices()` talks to the host audio API and can take a few hundred
    milliseconds on a Windows machine with many endpoints. Mark LV learned this
    lesson the expensive way — a 2.1-second `openwakeword` import on the Qt
    thread made the settings drawer look like it was broken. So the list is
    fetched once on a background thread at startup and served from cache.
"""

from __future__ import annotations

import contextlib
import threading
import time

# The label shown for "let the OS decide", and the value stored in config for
# it. Empty string, so an untouched install and a deliberately-default install
# are the same thing — nothing changes for anyone who never opens the picker.
DEFAULT_LABEL = "System default"
DEFAULT_VALUE = ""

_cache: dict[str, list[str]] | None = None
_cache_lock = threading.Lock()

# PortAudio's PulseAudio backend aborts the process
# ("Assertion 'm->n_waiting > 0' failed ... pa_threaded_mainloop_wait") when two
# threads enumerate/open streams at the same time. That is exactly what happens
# at startup: prefetch() enumerates on its own thread while _listen_audio()
# resolves the saved microphone on the event-loop thread. Every sounddevice
# call that enumerates or opens a probe stream goes through this lock.
_enum_lock = threading.RLock()

# Which host API each direction settled on, so resolve() opens the same endpoint
# the picker listed. Filled in by _query().
_chosen_api: dict = {"input": None, "output": None}

# Last device table handed out by PortAudio — (devices, host API names). Kept so
# a name can be resolved to an index WHILE streams are running, when enumerating
# again is exactly what trips the PulseAudio assertion described above. Written
# only under `_enum_lock`.
_table_cache: tuple[list, list] | None = None

# How many audio streams the caller currently has open. The multi-microphone
# path cannot ask PortAudio anything while a stream is live (opening a probe
# stream next to a running one is the abort this module exists to avoid), so it
# has to be told. `live_stream()` is that channel, and it is a counter because
# the microphone and the speaker streams overlap.
_live_streams = 0
_live_lock = threading.Lock()


@contextlib.contextmanager
def live_stream():
    """Declare that one or more audio streams are open inside this block.

    Use it around any code that keeps an InputStream/OutputStream alive. While
    it runs, resolve_many() serves indices from the cached device table and
    skips the `_usable` probe — it never enumerates and never opens a stream.
    """
    global _live_streams
    with _live_lock:
        _live_streams += 1
    try:
        yield
    finally:
        with _live_lock:
            _live_streams = max(0, _live_streams - 1)


def streams_live() -> bool:
    """True while any caller is inside a `live_stream()` block."""
    return _live_streams > 0


# ── Why the raw list is unusable, and what is filtered out ───────────────────
#
# `sd.query_devices()` returns one entry per (device × host API), not one per
# device. Measured on a normal Windows machine: 41 entries for what the Windows
# sound settings show as 4 microphones and 4 speakers. The same Realtek
# microphone appears four times — once each under MME, DirectSound, WASAPI and
# WDM-KS — and none of the four is labelled to say which is which.
#
# Handing that to a person is not a choice, it is a quiz. So the list is reduced
# the way the operating system's own settings panel does it:
#
#   1. ONE host API per direction — chosen by measurement, not by reasoning.
#      See the preference note below.
#   2. No pseudo-devices. "Microsoft Sound Mapper", "Primary Sound Driver",
#      ALSA's "default"/"sysdefault"/"dmix" are aliases for "whatever the OS
#      picks" — which is precisely the "System default" entry already at the top
#      of the list. Offering them again as if they were hardware is noise.
#   3. No zero-channel or unnamed entries (WASAPI reports one of each).
#   4. Deduplicated by name.
#
# Nothing is hidden that a person could actually want: the same hardware is
# still there, listed once, under the name their operating system uses for it.

# ── Preference order, which is only a starting point ─────────────────────────
#
# Two earlier versions of this file were wrong in the same way: they decided
# which host API to use by reasoning about it instead of measuring it.
#
#   v1 ranked APIs by how clean their device names were and picked WASAPI.
#      WASAPI in shared mode does not resample — the hardware runs at 48 kHz,
#      this app streams 16 kHz in and 24 kHz out, and every open failed with
#      "Invalid sample rate". The picker looked right and did nothing.
#
#   v2 added a rate check and picked DirectSound, which passes that check on
#      both sides. PortAudio's DirectSound *output* is a silent sink: the
#      stream opens, every write returns success in ~0 ms, and nothing is ever
#      heard. Same failure, one layer deeper.
#
# So this list is a preference, not a promise. Which API actually gets used is
# decided below by _usable() (open it for real) and _transport_works() (does
# audio actually move), per direction. On this platform that lands on
# DirectSound for the microphone and MME for the speakers — a split that no
# amount of reasoning would have produced.
_PREFERRED_APIS = {
    "Windows": ("directsound", "mme", "wasapi"),
    # macOS has only Core Audio, so there is nothing to disambiguate.
    "Darwin":  ("core audio",),
    # PulseAudio/PipeWire present one clean endpoint per device; raw ALSA
    # presents dozens of routing permutations of the same card.
    "Linux":   ("pulse", "pipewire", "jack", "alsa"),
}

# ── "It opens" is not "it works" ─────────────────────────────────────────────
#
# Opening a stream successfully proves nothing. Measured, writing 2.0 s of audio:
#
#     device=None (MME)                    2.02 s   consumed in real time
#     DirectSound, any output device       0.00 s   swallowed instantly
#
# No flag or capability field reports this. The only thing that separates a real
# sink from a fake one is whether it consumes audio at the rate audio is
# consumed at — so that is what gets measured, once per host API per direction,
# on the background thread at startup, using silence.
#
# Each direction is probed **the way main.py actually uses it**. That is not a
# detail: DirectSound input passes a callback stream and fails a blocking read,
# so an earlier version of this probe rejected a microphone that works perfectly
# in the app. Probe the mode you ship, not the mode that is easier to write.
_PROBE_SECONDS = {"output": 0.6, "input": 0.35}

# Cache: {(api_name_or_None, kind): bool}
_probe_results: dict = {}


def _transport_works(idx: int, kind: str, api_key) -> bool:
    """Does this host API actually move audio, or only pretend to?

    Probed once per API per direction and cached. Output writes silence, so the
    probe is inaudible; input reads and discards."""
    if api_key in _probe_results:
        return _probe_results[api_key]

    ok = False
    try:
        import sounddevice as sd
        rate = _RATES.get(kind, 16000)
        secs = _PROBE_SECONDS.get(kind, 0.5)

        # Each direction is probed the way main.py actually uses it. That is not
        # a detail: DirectSound input passes a callback stream and fails a
        # blocking read, so probing the wrong mode rejected a microphone that
        # works perfectly in the app.
        if kind == "output":
            # main.py writes with stream.write() — a real sink is rate-limited
            # by the hardware clock, a fake one swallows the buffer instantly.
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx)
            st.start()
            t0 = time.monotonic()
            st.write(bytes(int(rate * secs) * 2))   # silence — inaudible
            elapsed = time.monotonic() - t0
            st.stop(); st.close()
            ok = elapsed > secs * 0.5
            if not ok:
                print(f"[Audio] output: host API reports success but moves no "
                      f"audio ({elapsed*1000:.0f} ms for {secs*1000:.0f} ms) "
                      f"— skipping it")
        else:
            # main.py reads through a callback — count what arrives.
            frames = [0]

            def _cb(indata, n, *_a):
                frames[0] += n

            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx, callback=_cb)
            st.start()
            time.sleep(secs)
            st.stop(); st.close()
            ok = frames[0] > rate * secs * 0.3
            if not ok:
                print(f"[Audio] input: host API delivered {frames[0]} frames in "
                      f"{secs*1000:.0f} ms — skipping it")
    except Exception as e:
        print(f"[Audio] {kind} transport probe failed: {e}")
        ok = False

    _probe_results[api_key] = ok
    return ok


def _display_name(name: str, devices) -> str:
    """MME truncates device names to 31 characters, so the API that actually
    carries the audio may not be the one that can spell. If another host API
    knows a longer name that starts with this one, show that instead — the user
    reads 'Realtek HD Audio 2nd output (Realtek(R) Audio)' while the stream runs
    on the endpoint called 'Realtek HD Audio 2nd output (Re'."""
    if len(name) < 30:
        return name
    best = name
    for dev in devices:
        other = (dev.get("name") or "").strip()
        if len(other) > len(best) and other.startswith(name):
            best = other
    return best

# The rates the app opens its streams at. Defaults match main.py; main.py calls
# configure() at startup with its own constants so the two can never drift apart
# and silently reintroduce the bug above.
_RATES = {"input": 16000, "output": 24000}


def configure(input_rate: int, output_rate: int) -> None:
    """Tell this module the sample rates the audio streams will use, so the
    picker can rule out devices that cannot be opened at them.

    Drops any cached list: which devices are usable depends on the rate, so a
    list built under the old rates would be stale."""
    global _cache
    _RATES["input"]  = int(input_rate)
    _RATES["output"] = int(output_rate)
    with _cache_lock:
        _cache = None


def _usable(idx: int, kind: str) -> bool:
    with _enum_lock:
        return _usable_serialized(idx, kind)


def _usable_serialized(idx: int, kind: str) -> bool:
    """Can this device actually be opened at the rate we need?

    Deliberately opens a real stream rather than asking
    `check_output_settings`, because that function lies: it passed for an MME
    endpoint that then failed to open with "The specified format is not
    supported or cannot be translated" [MME error 32]. Opening and immediately
    closing costs milliseconds and is the only answer that holds."""
    st = None
    try:
        import sounddevice as sd
        rate = _RATES.get(kind, 16000)
        if kind == "input":
            st = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                blocksize=1024, device=idx,
                                callback=lambda *_a: None)
        else:
            st = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16",
                                    blocksize=1024, device=idx)
        st.start()
        return True
    except Exception:
        return False
    finally:
        if st is not None:
            try:
                st.stop(); st.close()
            except Exception:
                pass

# Aliases for "the default device" and internal routing endpoints. Matched
# case-insensitively as substrings against the device name.
_PSEUDO_DEVICES = (
    "sound mapper",        # Windows MME
    "primary sound",       # Windows DirectSound ("Primary Sound Capture Driver")
    "sysdefault",          # ALSA
    "default",             # ALSA / PulseAudio alias
    "dmix", "dsnoop",      # ALSA software mixing plugins
    "surround",            # ALSA channel-layout permutations of one card
    "samplerate", "speexrate", "upmix", "vdownmix", "null",
)


def _is_pseudo(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _PSEUDO_DEVICES)


def _enumerate() -> tuple[list, list]:
    """(devices, host API names) straight from PortAudio, and cache the table.

    Callers must already hold `_enum_lock` — see the note at the top of this
    file. Never raises by itself: the import/queries are the caller's problem.
    """
    global _table_cache
    import sounddevice as sd

    devices = list(sd.query_devices())
    try:
        apis = [a.get("name", "") for a in sd.query_hostapis()]
    except Exception:
        apis = []
    _table_cache = (devices, apis)
    return devices, apis


def _cached_table() -> tuple[list, list] | None:
    """The last enumeration, or None. Never touches PortAudio."""
    return _table_cache


def _query() -> dict[str, list[str]]:
    with _enum_lock:
        return _query_serialized()


def _query_serialized() -> dict[str, list[str]]:
    """Return {'input': [names...], 'output': [names...]}. Never raises.

    Only real, selectable devices — see the note above. Callers must already
    hold `_enum_lock` (sounddevice/PulseAudio are not safe to enumerate from
    two threads at once)."""
    out: dict[str, list[str]] = {"input": [], "output": []}
    try:
        import platform

        devices, apis = _enumerate()

        preferred = _PREFERRED_APIS.get(platform.system(), ())

        def _collect(api_filter, kind) -> list[tuple[int, str]]:
            """(index, name) for named, non-pseudo devices on one side that can
            be opened at the rate that side runs at."""
            chan = "max_input_channels" if kind == "input" else "max_output_channels"
            found, seen = [], set()
            for idx, dev in enumerate(devices):
                name = (dev.get("name") or "").strip()
                if not name or _is_pseudo(name) or name in seen:
                    continue
                if dev.get(chan, 0) <= 0:
                    continue
                if api_filter is not None:
                    api = apis[dev["hostapi"]].lower() if dev.get("hostapi", -1) < len(apis) else ""
                    if api_filter not in api:
                        continue
                if not _usable(idx, kind):
                    continue
                seen.add(name)
                found.append((idx, name))
            return found

        # Each direction picks its own host API. They are genuinely different
        # problems — on Windows the microphone works on DirectSound while the
        # speakers only work on MME — and a single global choice cannot be right
        # for both.
        for kind in ("input", "output"):
            for api_filter in list(preferred) + [None]:
                found = _collect(api_filter, kind)
                if not found:
                    continue
                # One probe per API per direction, cached, on this thread.
                if not _transport_works(found[0][0], kind, (api_filter, kind)):
                    continue
                _chosen_api[kind] = api_filter
                out[kind] = [_display_name(n, devices) for _i, n in found]
                break
            if out[kind]:
                print(f"[Audio] {kind}: using "
                      f"{_chosen_api[kind] or 'any host API'} "
                      f"({len(out[kind])} devices)")
        return out

    except Exception as e:
        print(f"[Audio] Device enumeration failed: {e}")
    return out


def prefetch() -> None:
    """Warm the cache on a background thread. Called once at startup so the
    settings drawer never pays for enumeration on the Qt thread."""
    def _work():
        global _cache
        # Hold the lock through the cache write: a thread that waits for the
        # enumeration to finish must find the result already published, not
        # start a second enumeration right after the first one releases.
        with _enum_lock:
            result = _query()
            with _cache_lock:
                _cache = result
        print(f"[Audio] {len(result['input'])} input / "
              f"{len(result['output'])} output devices found")
    threading.Thread(target=_work, daemon=True, name="audio-devices").start()


def list_devices(kind: str, refresh: bool = False) -> list[str]:
    """Device names for 'input' or 'output'. Falls back to a synchronous query
    if the prefetch has not landed yet — correctness over the cache."""
    global _cache
    with _cache_lock:
        cached = None if refresh else _cache
    if cached is None:
        with _enum_lock:
            # The prefetch may have published the cache while we waited.
            with _cache_lock:
                cached = None if refresh else _cache
            if cached is None:
                cached = _query()
                with _cache_lock:
                    _cache = cached
    return list(cached.get(kind, []))


def resolve(name: str, kind: str):
    with _enum_lock:
        return _resolve_serialized(name, kind)


def resolve_many(
    names,
    kind: str = "input",
    *,
    probe: bool | None = None,
    refresh: bool = False,
) -> list:
    """Resolve SEVERAL saved device names under one enumeration lock.

    One stream per microphone means the app resolves N names at the same
    instant, at startup and again whenever the device set changes. Calling
    resolve() in a loop would enumerate once per name and, worse, would let
    another thread enumerate between them — the PortAudio/PulseAudio assertion
    at the top of this file. So every name here is resolved against ONE device
    table, taken once, under one acquisition of `_enum_lock`.

    `probe` controls the `_usable` open-and-close check:
      * None (default) — probe only when no stream is live;
      * True/False — force it. A live stream still wins: probing is skipped,
        because opening a probe stream next to a running one is the abort this
        module exists to avoid.

    `refresh` is honoured only when nothing is streaming: with a live stream
    this NEVER enumerates again, whatever the caller asked for, because that
    enumeration is the crash. In that case indices come from the device table
    cached by the startup prefetch; if there is none, every name degrades to
    the system default (None) instead of taking the process down.

    Returns a list aligned with `names`: an index, or None for "system default"
    (unknown, empty, or not resolvable right now).
    """
    wanted = [str(name or "") for name in names]
    if not wanted:
        return []

    with _enum_lock:
        live = streams_live()
        do_probe = (not live) if probe is None else bool(probe)

        if live:
            if do_probe:
                print("[Audio] resolve_many: probe ignorado com stream ativo "
                      "(abrir um stream ao lado de outro derruba o processo)")
            do_probe = False
            table = _cached_table()
            if table is None:
                print("[Audio] resolve_many: sem tabela de dispositivos em "
                      "cache e streams ativos — usando o padrão do sistema")
                return [None] * len(wanted)
            if refresh:
                print("[Audio] resolve_many: refresh ignorado com streams "
                      "ativos — usando a tabela em cache")
            return [_resolve_serialized(name, kind, table=table, probe=False,
                                        warm=False)
                    for name in wanted]

        # Nothing is streaming: one enumeration serves every name. list_devices
        # warms the cache and picks the host API (a no-op once warm). A cold
        # cache was just enumerated by that call, so its table is reused; a
        # warm one is refreshed once, because the prefetch may be minutes old
        # and a device plugged since would shift every index after it.
        with _cache_lock:
            warm = _cache is not None
        try:
            list_devices(kind)
            table = _enumerate() if warm else _cached_table()
            if table is None:
                table = _enumerate()
        except Exception as e:
            print(f"[Audio] resolve_many: device table unavailable ({e}) — "
                  "using system default")
            return [None] * len(wanted)
        return [_resolve_serialized(name, kind, table=table, probe=do_probe)
                for name in wanted]


def _accept_any(_idx: int, _kind: str) -> bool:
    """Stand-in for _usable() when probing is not allowed (streams are live)."""
    return True


def _resolve_serialized(
    name: str,
    kind: str,
    *,
    table: tuple[list, list] | None = None,
    probe: bool = True,
    warm: bool = True,
):
    """Turn a saved device name into something sounddevice accepts.

    Returns None for "system default" — which is also what we return when the
    saved device is gone, because a missing headset must degrade to the built-in
    speakers, not to a crash on startup.

    Candidates are walked in the same host-API order the picker used, so a name
    the user chose from the WASAPI list resolves to the WASAPI endpoint. Without
    that ordering a full name would fall through to MME's truncated copy of the
    same device — which happens to work, but means the setting quietly refers to
    a different endpoint than the one on screen.

    `table` is a pre-taken (devices, apis) pair so a batch can share one
    enumeration; `probe=False` accepts a candidate without opening it, for the
    case where a stream is already running; `warm=False` skips the host-API
    lookup, which would enumerate on a cold cache."""
    wanted = (name or "").strip()
    if not wanted or wanted == DEFAULT_LABEL:
        return None

    try:
        import platform

        devices, apis = table if table is not None else _enumerate()
        accept = _usable if probe else _accept_any

        want_in  = (kind == "input")
        chan_key = "max_input_channels" if want_in else "max_output_channels"

        def _candidates(api_filter):
            for idx, dev in enumerate(devices):
                if dev.get(chan_key, 0) <= 0:
                    continue
                if api_filter is not None:
                    api = apis[dev["hostapi"]].lower() if dev.get("hostapi", -1) < len(apis) else ""
                    if api_filter not in api:
                        continue
                yield idx, (dev.get("name") or "").strip()

        # The API the picker settled on for this direction comes first — the
        # endpoint that was listed must be the endpoint that gets opened, or the
        # setting means something different from what it says. list_devices()
        # populates it; calling it here is a no-op once the cache is warm.
        if warm:
            list_devices(kind)
        chosen = _chosen_api.get(kind)
        orders = ([chosen] if chosen is not None else []) \
            + [a for a in _PREFERRED_APIS.get(platform.system(), ()) if a != chosen] \
            + [None]

        # A candidate only counts if it can be opened at the rate this side runs
        # at. The prefix match matters because the API that carries the audio is
        # not always the one that can spell: MME truncates names to 31 characters
        # while DirectSound and WASAPI do not, so the name shown in the picker
        # can be longer than the name of the endpoint it actually opens.
        for api_filter in orders:
            partial = None
            for idx, dev_name in _candidates(api_filter):
                if dev_name == wanted:
                    if accept(idx, kind):
                        return idx
                    continue
                if partial is None and (dev_name.startswith(wanted[:24])
                                        or wanted.startswith(dev_name[:24])):
                    if accept(idx, kind):
                        partial = idx
            if partial is not None:
                return partial

        print(f"[Audio] Saved {kind} device '{wanted}' cannot be opened at "
              f"{_RATES.get(kind)} Hz on any host API — using system default")
        return None
    except Exception as e:
        print(f"[Audio] resolve({kind}) failed: {e} — using system default")
        return None
