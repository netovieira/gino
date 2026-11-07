"""
Background-music control by voice.

"pausa a música", "continua", "para a música", "próxima", "abaixa a música",
"aumenta a música", "o que está tocando?" — all for the music JARVIS itself
plays (the morning track and the Focus Protocol playlist). The actual player
lives in the HUD; this plugin only relays through JarvisLive.
"""

PLUGIN = {
    "name": "music_control",
    "description": (
        "Controls the background music that the assistant itself is ALREADY playing "
        "(the morning track or the Focus Protocol playlist): pause, resume, "
        "stop, next track, make it quieter/louder (volume), or say what is "
        "playing (status). Use this ONLY for the transport of music that is "
        "already running — 'pausa/para/continua a música', 'próxima', "
        "'abaixa/aumenta a música', 'o que está tocando'. This tool never opens "
        "an app: 'abre/toca o YouTube Music' is the installed PWA and belongs to "
        "`media_control` (action='play'), which launches the app window (not a "
        "Chrome tab). To PLAY a specific "
        "song the user asks for ('toca X', 'bota uma música do Y', 'coloca "
        "um funk'), use `youtube_video` with action='play' and query=<a "
        "música>, NOT this tool. Only use computer_settings for the system "
        "volume when the user explicitly says 'volume do computador'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "pause | resume | stop | next | volume | status"},
            "value": {"type": "STRING",
                      "description": "For volume: 'up', 'down', or a number 0-100"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = (parameters.get("action") or "status").strip().lower()
    value = (parameters.get("value") or "").strip().lower() or None
    hook = getattr(player, "on_music_control", None) if player is not None else None
    if not callable(hook):
        return "Music control is not available in this build."
    try:
        res = str(hook(action, value))
    except Exception as e:
        return f"Music control failed: {e}"
    parts = res.split("|")
    state = parts[0] if parts else "stopped"
    title = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""
    what = f" ({title})" if title and title != "(next)" else ""
    if action == "status":
        if state == "stopped":
            return "No music is playing."
        return f"Music is {state}{what}" + (", Focus Protocol playlist." if mode == "focus" else ".")
    if state == "stopped" and action != "stop":
        return "There is no music playing right now."
    if action == "pause":
        return f"Music paused{what}."
    if action == "resume":
        return f"Music resumed{what}."
    if action == "stop":
        return "Music stopped." + (" Focus Protocol ended." if mode == "focus" else "")
    if action == "next":
        return "Next track." if mode == "focus" else "Only the Focus Protocol has a playlist; this is a single track."
    if action == "volume":
        pct = parts[3] if len(parts) > 3 else "?"
        return f"Music volume set to {pct}%."
    return res
