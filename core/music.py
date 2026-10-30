"""
Background music — a YouTube track or mix played under JARVIS's voice.

Two uses, both configured in config/api_keys.json:

    "morning_music_url": the track that opens the first launch of the day.
        The greeting waits until the music has reached MORNING_SYNC_MS, so the
        voice lands on the music's opening rather than over a silent intro.
    "focus_music_url":   the track the Focus Protocol builds a playlist from
        (YouTube's own mix for that track, so it keeps going).
    "music_volume":      0..1, the level while JARVIS is quiet (default 0.35);
        while it speaks the level drops by MUSIC_DUCK so the voice stays on top.
    "focus_water_minutes": how often the Focus Protocol reminds you to drink
        water (default 30).

Streams are resolved with yt-dlp (already a dependency) and played by the HUD's
QtMultimedia player, so no new package is needed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_URL = "https://www.youtube.com/watch?v=XgWUDbYfNe4"
MORNING_SYNC_MS = 5000        # the greeting starts when the music is here
MUSIC_DUCK = 0.40             # volume factor while JARVIS speaks
DEFAULT_VOLUME = 0.35
DEFAULT_WATER_MIN = 30

_CFG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
_VID = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def _cfg() -> dict:
    try:
        data = json.loads(_CFG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def morning_url() -> str:
    v = _cfg().get("morning_music_url", DEFAULT_URL)
    return str(v or "").strip()          # "" disables the morning music


def focus_url() -> str:
    return str(_cfg().get("focus_music_url") or DEFAULT_URL).strip()


def cookies_browser() -> str:
    """Browser whose YouTube login yt-dlp may borrow ("edge", "chrome",
    "firefox"; "" = none). YouTube sometimes answers "Sign in to confirm
    you're not a bot" to anonymous requests; a logged-in browser's cookies
    get past that. Set as "music_cookies_browser" in config/api_keys.json."""
    v = str(_cfg().get("music_cookies_browser") or "").strip().lower()
    return v if v in ("edge", "chrome", "firefox", "brave", "chromium", "opera", "vivaldi") else ""


def _ydl_opts(use_cookies: bool = True, **extra) -> dict:
    """yt-dlp options.

    `use_cookies` is not cosmetic. SEARCH runs better signed in (personalised
    results, no "confirm you're not a bot" page), but PLAYBACK breaks when the
    browser's YouTube session is attached: the signed-in web client answers with
    formats that require a PO token and yt-dlp cannot select any of them —
    "Requested format is not available" on every video, verified here on the
    same URL that plays fine anonymously. So search borrows the login and the
    actual stream does not.
    """
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 15, "retries": 2, "extractor_retries": 1,
            # Only MP4/AAC audio: it is what the Windows media backend demuxes
            # without fuss. WebM/Opus streams came back as "Demuxing failed".
            "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/best[ext=mp4]/bestaudio",
            # Clients that are not asked for a sign-in as often as the web one.
            "extractor_args": {"youtube": {"player_client": ["default", "android", "ios"]}}}
    b = cookies_browser()
    if b and use_cookies:
        opts["cookiesfrombrowser"] = (b,)
    opts.update(extra)
    return opts


def search(query: str, limit: int = 5) -> list[dict]:
    """[{title, url, duration, channel}] straight from YouTube, signed in.

    extract_flat is what makes it cheap and what makes the login usable: the
    entries come back as titles and URLs without yt-dlp touching media formats,
    which is the part the signed-in session breaks (see _ydl_opts).
    """
    import yt_dlp
    q = str(query or "").strip()
    if not q:
        return []
    opts = _ydl_opts(extract_flat="in_playlist", noplaylist=False,
                     playlistend=int(limit))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{int(limit)}:{q}", download=False) or {}
    except Exception as e:
        print(f"[Music] busca falhou ({str(e)[:120]})")
        return []
    out: list[dict] = []
    for e in (info.get("entries") or []):
        if not isinstance(e, dict):
            continue
        vid = e.get("id") or video_id(e.get("url") or "")
        if not vid:
            continue
        out.append({
            "title": str(e.get("title") or ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "channel": str(e.get("uploader") or e.get("channel") or ""),
            "duration": e.get("duration"),
        })
    return out


def volume() -> float:
    try:
        return max(0.0, min(1.0, float(_cfg().get("music_volume", DEFAULT_VOLUME))))
    except Exception:
        return DEFAULT_VOLUME


def water_minutes() -> int:
    try:
        return max(5, int(_cfg().get("focus_water_minutes", DEFAULT_WATER_MIN)))
    except Exception:
        return DEFAULT_WATER_MIN


def video_id(url: str) -> str:
    m = _VID.search(url or "")
    return m.group(1) if m else ""


def mix_url(url: str) -> str:
    """YouTube's auto-generated mix for a track: an endless playlist seeded by it."""
    vid = video_id(url)
    return f"https://www.youtube.com/watch?v={vid}&list=RD{vid}" if vid else url


CACHE_DIR = Path(__file__).resolve().parent.parent / "memory" / "music_cache"


def cached_file(url: str) -> str:
    """Path of an already-downloaded track for this video, or ""."""
    vid = video_id(url)
    if not vid or not CACHE_DIR.exists():
        return ""
    for f in CACHE_DIR.glob(f"{vid}.*"):
        if f.suffix.lower() in (".m4a", ".mp3", ".webm", ".opus", ".ogg") and f.stat().st_size > 50_000:
            return str(f)
    return ""


def download_audio(url: str) -> tuple[str, str]:
    """Download the track's audio once into memory/music_cache and return
    (local_path, title). A local file is the one source QtMultimedia never
    refuses; YouTube's streaming URLs sometimes do ("Could not open file")."""
    import yt_dlp
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    have = cached_file(url)
    opts = _ydl_opts(use_cookies=False, outtmpl=str(CACHE_DIR / "%(id)s.%(ext)s"),
                     skip_download=bool(have))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=not have)
    if info and info.get("entries"):
        info = info["entries"][0]
    title = str((info or {}).get("title") or "")
    path = have or cached_file(url)
    if not path and info:
        for key in ("filepath", "_filename"):
            cand = (info.get("requested_downloads") or [{}])[0].get(key) if info.get("requested_downloads") else info.get(key)
            if cand and Path(cand).exists():
                path = str(cand)
                break
    if not path:
        raise RuntimeError("download produced no file")
    return path, title


def resolve_audio(url: str) -> tuple[str, str]:
    """(audio_stream_url, title) for one video. Raises with a reason."""
    import yt_dlp
    opts = _ydl_opts(use_cookies=False, skip_download=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("entries"):
        info = info["entries"][0]
    if not info:
        raise RuntimeError("nothing found")
    stream = info.get("url")
    ext = str(info.get("ext") or "")
    if not stream or ext not in ("m4a", "mp4", "mp3", ""):
        # Pick an MP4/AAC audio-only format by hand; never WebM/Opus.
        fmts = [f for f in (info.get("formats") or []) if f.get("url")
                and f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
                and (str(f.get("ext") or "") in ("m4a", "mp4") or str(f.get("acodec") or "").startswith("mp4a"))]
        fmts.sort(key=lambda f: (f.get("language_preference") or 0, f.get("abr") or f.get("tbr") or 0))
        stream = fmts[-1]["url"] if fmts else (stream or "")
    if not stream:
        raise RuntimeError("no playable (MP4/AAC) audio stream offered")
    return stream, str(info.get("title") or "")


def resolve_playlist(url: str, limit: int = 25) -> list[tuple[str, str]]:
    """[(video_url, title)] for the mix seeded by `url`; the track itself
    first. Falls back to the single track when the mix cannot be listed."""
    import yt_dlp
    seed = video_id(url)
    out: list[tuple[str, str]] = []
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": True, "playlistend": limit}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(mix_url(url), download=False) or {}
        for e in info.get("entries") or []:
            vid = e.get("id") or video_id(e.get("url") or "")
            if vid:
                out.append((f"https://www.youtube.com/watch?v={vid}", str(e.get("title") or "")))
    except Exception as ex:
        print(f"[Music] mix unavailable ({ex}); looping the track instead")
    if seed:
        out = [(f"https://www.youtube.com/watch?v={seed}", "")] + [o for o in out if video_id(o[0]) != seed]
    return out or [(url, "")]
