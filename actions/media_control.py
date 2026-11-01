"""Control external music players on Linux, or open YouTube Music visibly.

This is deliberately *not* the ``music_control`` plugin: that one drives the
assistant's own HUD playback. This action talks to whatever music app the user
already has open — Spotify, a browser playing YouTube Music, VLC — through
MPRIS on the session bus, and falls back to opening YouTube Music in Chrome
when nothing is open.

No state is checked before or after a transport command: the call is sent and
the message says so. Reading state first was what made the old attempts feel
slow and, worse, claim "nothing is playing" while a player was plainly on
screen. Volume goes through PipeWire's ``wpctl`` where available and ``pactl``
otherwise, again without reading the level back.

Every subprocess call is short-lived, has a timeout, and never raises out of
this module: the caller gets a sentence it can speak instead of a traceback.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from urllib.parse import quote_plus

try:
    import requests
except Exception:  # pragma: no cover - requests is a project dependency
    requests = None

_BUS_NAME_PREFIX = "org.mpris.MediaPlayer2."
_OBJECT_PATH = "/org/mpris/MediaPlayer2"
_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

# Transport names as the user speaks them -> the MPRIS method that sends it.
_TRANSPORT = {
    "pause": "Pause",
    "resume": "Play",
    "next": "Next",
    "previous": "Previous",
    "stop": "Stop",
}

# Chrome under Wayland still takes --new-window; the binary may be any of these.
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chrome", "chromium")

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
_SHORTS_ID_RE = re.compile(r"/shorts/([A-Za-z0-9_-]{11})")

_YT_MUSIC_HOME = "https://music.youtube.com"
_YT_MUSIC_SEARCH = "https://music.youtube.com/search?q={query}"
_YT_MUSIC_WATCH = "https://music.youtube.com/watch?v={video_id}"


def _run(cmd: list[str], timeout: float = 6.0):
    """subprocess.run with the flags every call here wants, and no raising."""
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, check=False)


def players() -> list[str]:
    """MPRIS player bus names currently on the user bus, in bus order.

    ``mpris-proxy`` and ``playerctld`` register names too and would answer
    transport calls that go nowhere the user can see, so they are dropped.
    """
    try:
        result = _run(["busctl", "--user", "list", "--no-legend"], timeout=5.0)
    except Exception:
        return []
    if getattr(result, "returncode", 1) != 0:
        return []

    found: list[str] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        if not name.startswith(_BUS_NAME_PREFIX):
            continue
        lowered = name.lower()
        if "mpris-proxy" in lowered or "playerctld" in lowered:
            continue
        if name not in found:
            found.append(name)
    return found


def mpris_call(player: str, method: str) -> tuple[bool, str]:
    """Send one Player method and return (ok, message). No state is read."""
    method = (method or "").strip()
    if not method:
        return False, "Método MPRIS vazio."
    cmd = [
        "gdbus", "call",
        "--session",
        "--dest", player,
        "--object-path", _OBJECT_PATH,
        "--method", f"{_PLAYER_IFACE}.{method}",
    ]
    try:
        result = _run(cmd, timeout=6.0)
    except Exception as e:
        return False, f"Não consegui falar com {player}: {e}"
    if getattr(result, "returncode", 1) == 0:
        return True, f"{method} enviado para {player}."
    detail = (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip()
    return False, f"{player} recusou {method}" + (f": {detail}" if detail else ".")


def _chrome_bin() -> str:
    """The chrome command name to spawn, or "" when no Chrome is installed."""
    for name in _CHROME_NAMES:
        if shutil.which(name):
            return name
    return ""


def _spawn_visible(url: str) -> bool:
    """Open a URL in a visible Chrome window, else through xdg-open."""
    chrome = _chrome_bin()
    if chrome:
        try:
            subprocess.Popen(
                [chrome, "--new-window", url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _scrape_video_id(query: str) -> str:
    """First non-Shorts video id on a YouTube Music search page, or ""."""
    if requests is None:
        return ""
    url = _YT_MUSIC_SEARCH.format(query=quote_plus(query))
    try:
        response = requests.get(url, headers=_UA, timeout=8)
        html = response.text or ""
    except Exception:
        return ""
    shorts = set(_SHORTS_ID_RE.findall(html))
    seen: set[str] = set()
    for video_id in _VIDEO_ID_RE.findall(html):
        if video_id in seen:
            continue
        seen.add(video_id)
        if video_id in shorts:
            continue
        return video_id
    return ""


def _open_youtube_music(query: str) -> str:
    if not query:
        ok = _spawn_visible(_YT_MUSIC_HOME)
        if ok:
            return "YouTube Music aberto na tela."
        return "Não consegui abrir o YouTube Music na tela."

    video_id = _scrape_video_id(query)
    if video_id:
        url = _YT_MUSIC_WATCH.format(video_id=video_id)
        ok = _spawn_visible(url)
        if ok:
            return f"Abrindo '{query}' no YouTube Music, na tela."
        return f"Achei '{query}', mas não consegui abrir a janela."

    url = _YT_MUSIC_SEARCH.format(query=quote_plus(query))
    ok = _spawn_visible(url)
    if ok:
        return f"Abrindo a busca de '{query}' no YouTube Music, na tela."
    return f"Não consegui abrir a busca de '{query}' no YouTube Music."


def _say(player, speak, message: str) -> str:
    """Best-effort spoken copy; the returned string is the real answer."""
    for fn in (speak, getattr(player, "request_say", None) if player else None):
        if callable(fn):
            try:
                fn(message)
            except Exception:
                pass
            break
    return message


def _play(parameters: dict, player, speak) -> str:
    query = str(parameters.get("query") or "").strip()
    if query:
        return _say(player, speak, _open_youtube_music(query))

    open_players = players()
    if open_players:
        ok, message = mpris_call(open_players[0], "Play")
        if not ok:
            return _say(player, speak, f"Não consegui tocar: {message}")
        return _say(player, speak, message)
    return _say(player, speak, _open_youtube_music(""))


def _transport(action: str, player, speak) -> str:
    open_players = players()
    if not open_players:
        return _say(player, speak, "Nenhum player aberto para controlar.")
    ok, message = mpris_call(open_players[0], _TRANSPORT[action])
    if not ok:
        return _say(player, speak, f"Não consegui {action}: {message}")
    return _say(player, speak, message)


def _volume_command(value: str) -> tuple[list[str] | None, list[str] | None, str]:
    """(wpctl argv, pactl argv, spoken label) for a volume value."""
    raw = (value or "").strip().lower()
    if raw in ("up", "+", "aumentar", "mais"):
        return (
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%+"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"],
            "Volume aumentado.",
        )
    if raw in ("down", "-", "diminuir", "menos"):
        return (
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"],
            "Volume diminuído.",
        )
    if raw in ("mute", "mudo", "toggle"):
        return (
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
            ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
            "Mudo alternado.",
        )

    number = raw[:-1].strip() if raw.endswith("%") else raw
    try:
        level = int(float(number))
    except Exception:
        return None, None, ""
    if level < 0 or level > 100:
        return None, None, ""
    return (
        ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
        f"Volume em {level}%.",
    )


def _volume(value: str, player, speak) -> str:
    wpctl_cmd, pactl_cmd, label = _volume_command(value)
    if wpctl_cmd is None:
        return _say(player, speak,
                    "Diga 'up', 'down', 'mute' ou um volume de 0 a 100.")

    # PipeWire first. A missing binary or a refused call falls through to
    # PulseAudio's pactl; neither the outcome nor the resulting level is read.
    try:
        result = _run(wpctl_cmd, timeout=5.0)
        if getattr(result, "returncode", 1) == 0:
            return _say(player, speak, label)
    except Exception:
        pass

    try:
        result = _run(pactl_cmd, timeout=5.0)
        if getattr(result, "returncode", 1) == 0:
            return _say(player, speak, label)
    except Exception as e:
        return _say(player, speak, f"Não consegui ajustar o volume: {e}")
    return _say(player, speak, "Não consegui ajustar o volume pelo wpctl nem pelo pactl.")


def _metadata(player: str) -> str:
    """'Title — Artist' from one player, best effort, or ""."""
    if not player:
        return ""
    cmd = [
        "gdbus", "call",
        "--session",
        "--dest", player,
        "--object-path", _OBJECT_PATH,
        "--method", f"{_PLAYER_IFACE}.Metadata",
    ]
    try:
        result = _run(cmd, timeout=6.0)
    except Exception:
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    out = result.stdout or ""
    title_match = re.search(r"'xesam:title':\s*<'([^']*)'>", out)
    artists_match = re.search(r"'xesam:artist':\s*<\[(.*?)\]>", out)
    title = title_match.group(1).strip() if title_match else ""
    artists = " ".join(re.findall(r"'([^']*)'", artists_match.group(1))) \
        if artists_match else ""
    if title and artists:
        return f"{title} — {artists}"
    return title or artists


def _status(player, speak) -> str:
    open_players = players()
    if not open_players:
        return _say(player, speak, "Nenhum player MPRIS aberto.")
    first = open_players[0]
    track = _metadata(first)
    if track:
        message = f"Player aberto: {first} — {track}."
    else:
        message = f"Player aberto: {first}."
    if len(open_players) > 1:
        message += f" (+{len(open_players) - 1} outro(s).)"
    return _say(player, speak, message)


def media_control(parameters: dict, player=None, speak=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").strip().lower()
    value = str(params.get("value") or "").strip()

    try:
        if action in ("play", "tocar", "abrir"):
            return _play(params, player, speak)
        if action in _TRANSPORT:
            return _transport(action, player, speak)
        if action == "volume":
            return _volume(value, player, speak)
        if action == "status":
            return _status(player, speak)
        return _say(player, speak,
                    f"Ação desconhecida: '{action}'. Use play, pause, resume, "
                    "next, previous, stop, volume ou status.")
    except Exception as e:  # a tool must always answer, never crash the call
        return _say(player, speak, f"Falha no controle de mídia: {e}")


TOOL = {
    "name": "media_control",
    "description": (
        "Controls music in an app that is ALREADY OPEN on the Linux desktop "
        "(Spotify, Chrome/YouTube Music, VLC, Rhythmbox) through MPRIS, or opens "
        "YouTube Music visibly in Chrome when nothing is open. Use for 'pausa a "
        "música', 'próxima faixa', 'volta', 'para a música', 'aumenta/diminui o "
        "volume', 'mudo', 'que música está tocando?', and for 'toca <música>' "
        "when the user wants it in the external player/YouTube Music. "
        "action='play' with a query searches YouTube Music and opens the first "
        "non-Shorts result in a visible window; without a query it resumes the "
        "open player or opens YouTube Music. Transport and volume commands are "
        "sent without checking the player's state first. Do NOT use this for "
        "the assistant's own HUD music (use music_control) and do not use it to "
        "play a video on the assistant's display (use video_player)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("play, pause, resume, next, previous, stop, "
                                "volume or status."),
                "enum": ["play", "pause", "resume", "next", "previous",
                         "stop", "volume", "status"],
            },
            "query": {
                "type": "STRING",
                "description": ("Song or search text for action='play' "
                                "(e.g. 'Bohemian Rhapsody')."),
            },
            "value": {
                "type": "STRING",
                "description": ("For action='volume': 'up', 'down', 'mute', "
                                "or an absolute level from 0 to 100."),
            },
        },
    },
    "handler": media_control,
}
