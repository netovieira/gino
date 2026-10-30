"""
"bom dia" — replay the morning routine on demand.

The first launch of the day plays the morning track, greets, checks the
YouTube channel and reads the AI news; later launches only greet. This tool
lets the user ask for the full routine again at any time:

    "<assistant name>, bom dia"   → handled locally when listening with Groq
                                     (never reaches the model); otherwise the
                                     model calls this tool.

Nothing to configure.
"""

PLUGIN = {
    "name": "morning_briefing",
    "description": (
        "Replays the user's full morning routine: the morning music, the greeting, "
        "the check of their YouTube channel and the AI news. Call it whenever the "
        "user greets you with 'bom dia' / 'good morning' (e.g. '<nome>, bom dia'), "
        "even if the briefing was already given today. After calling it, say "
        "NOTHING — the routine itself greets the user."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    hook = getattr(player, "on_good_morning", None) if player is not None else None
    if callable(hook):
        try:
            if hook("tool"):
                return ("Morning routine started: it plays the music, greets and reads "
                        "the news by itself. Reply with nothing at all — not one word.")
        except Exception as e:
            return f"Could not start the morning routine: {e}"
    return "The morning routine is not available right now (no live session)."
