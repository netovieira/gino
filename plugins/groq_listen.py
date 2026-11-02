"""
Groq listening — settings form and a status tool.

This plugin exists for its PLUGIN_SETTINGS: it puts the Groq key, the Whisper
model and the language into ⚙ → PLUGIN SETTINGS → GROQ, with a TEST CONNECTION
button, without any edit to the core. The actual listening lives in
core/groq_stt.py and is switched on with the "LISTEN WITH" button in the ⚙
drawer (or "stt_engine": "groq" in config/api_keys.json).

Get a key at https://console.groq.com/keys (free tier is enough).
"""
from core.groq_stt import MODELS, NAMESPACE, test_connection

PLUGIN = {
    "name": "groq_listen_status",
    "description": (
        "Reports whether the assistant is currently transcribing the user's speech with "
        "Whisper on Groq (text in) or hearing it natively through Gemini Live "
        "(audio in), and whether a Groq key is configured. Use when the user asks "
        "how they are being heard or whether Groq listening is on. Switching is "
        "done from the gear menu, not by this tool."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "GROQ (listening with Whisper)",
    "fields": [
        {"key": "api_key", "label": "Groq API key", "type": "password",
         "placeholder": "gsk_... from console.groq.com/keys"},
        {"key": "model", "label": "Whisper model", "type": "choice",
         "options": list(MODELS), "default": MODELS[0]},
        {"key": "language", "label": "Language (pt, en, es... or auto)", "type": "text",
         "default": "pt", "placeholder": "pt"},
        # Audio kept before the first phoneme (core/groq_stt ring). Text on
        # purpose: the settings form has no numeric field, and the listener
        # accepts "1.5" the same way it accepts a JSON number.
        {"key": "preroll_s", "label": "Pre-roll before speech (seconds)",
         "type": "text", "default": "1.5", "placeholder": "1.5"},
    ],
    "action": {"label": "TEST CONNECTION", "run": test_connection},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    from memory.config_manager import get_plugin_config, get_stt_engine
    engine = get_stt_engine()
    has_key = bool((get_plugin_config(NAMESPACE).get("api_key") or "").strip())
    if engine == "groq":
        return ("Listening through Whisper on Groq: your speech is transcribed "
                "locally segmented and sent to Gemini as text."
                if has_key else
                "Groq listening is selected but no Groq key is configured, so "
                "Gemini Live is hearing the microphone directly.")
    return ("Listening natively through Gemini Live. "
            + ("A Groq key is configured; switch with LISTEN WITH in the gear menu."
               if has_key else "No Groq key is configured."))
