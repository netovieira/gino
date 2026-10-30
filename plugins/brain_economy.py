"""
Economy brain — settings form and a status tool.

This plugin exists for its PLUGIN_SETTINGS: it puts the chat provider, key,
model and the Edge TTS voice into ⚙ → PLUGIN SETTINGS → ECONOMY BRAIN, with a
TEST CONNECTION button. The brain itself lives in core/brain.py and is
switched on with the BRAIN button in the ⚙ drawer (or "brain_engine":
"economy" in config/api_keys.json). It needs Groq listening for the ears
(⚙ → PLUGIN SETTINGS → GROQ) and two packages for the voice:

    py -m pip install edge-tts miniaudio
"""
from core.brain import (DEFAULT_VOICE, EDGE_VOICES, FALLBACK_PROVIDERS, FISH_MODELS,
                        NAMESPACE, PROVIDERS, TTS_ENGINES, label, test_connection,
                        voice_label)

PLUGIN = {
    "name": "brain_status",
    "description": (
        "Reports which brain is answering right now: Gemini Live (native audio) "
        "or the economy brain (a chat model such as DeepSeek, Groq's Llama or a "
        "Gemini text model writing the replies, read aloud by Microsoft Edge TTS "
        "or a Fish Audio voice), and which "
        "provider and model are configured. Use when the user asks what model "
        "or AI is answering, or whether economy mode is on. Switching is done "
        "from the gear menu, not by this tool."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "ECONOMY BRAIN (local first, then a fallback — Qoder / Groq / DeepSeek)",
    "fields": [
        {"key": "provider", "label": "Primary provider (the local one goes first)",
         "type": "choice", "options": list(PROVIDERS), "default": "qoder"},
        {"key": "base_url", "label": "Primary base URL (blank = provider default)",
         "type": "text", "placeholder": "http://127.0.0.1:3000/v1"},
        {"key": "api_key", "label": "Primary API key", "type": "password",
         "placeholder": "the Qoder proxy key, or sk-... / gsk_..."},
        {"key": "model", "label": "Primary model (blank = provider default)", "type": "text",
         "placeholder": "deepseek-v4-flash · openai/gpt-oss-120b · deepseek-chat"},
        {"key": "fallback_provider", "label": "Fallback provider (answers only if the primary fails)",
         "type": "choice", "options": list(FALLBACK_PROVIDERS), "default": "groq"},
        {"key": "fallback_model", "label": "Fallback model (blank = provider default)",
         "type": "text", "placeholder": "openai/gpt-oss-120b"},
        {"key": "fallback_api_key", "label": "Fallback API key (blank = reuse the GROQ form key)",
         "type": "password", "placeholder": "gsk_..."},
        {"key": "tts_engine", "label": "Voice engine", "type": "choice",
         "options": list(TTS_ENGINES), "default": "edge"},
        {"key": "tts_voice", "label": "Voice (Edge TTS, free)", "type": "choice",
         "options": list(EDGE_VOICES), "default": DEFAULT_VOICE},
        {"key": "fish_api_key", "label": "Fish Audio API key", "type": "password",
         "placeholder": "fish.audio → API keys"},
        {"key": "fish_voice_id", "label": "Fish Audio voice (ID or page URL)", "type": "text",
         "default": "5566fffe8b8046bc85e576aa9813b9bd",
         "placeholder": "https://fish.audio/m/<id>  or the 32-character id"},
        {"key": "fish_model", "label": "Fish Audio model (s2.1-pro-free needs no credits)",
         "type": "choice", "options": list(FISH_MODELS), "default": FISH_MODELS[0]},
    ],
    "action": {"label": "TEST CONNECTION", "run": test_connection},
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    from memory.config_manager import get_brain_engine, get_plugin_config
    from core.brain import settings
    st = settings(get_plugin_config)
    chain = " → ".join(label(r) for r in st["chain"])
    if get_brain_engine() == "economy":
        return (f"The economy brain is answering, read aloud by {voice_label(st)}. "
                f"The chain is {chain}: the first one is local if it is the Qoder "
                "proxy, the rest only answer if it fails. Gemini Live is not in use.")
    return ("Gemini Live is answering natively. The economy brain is configured for "
            f"{chain}; switch with the BRAIN button in the gear menu.")

