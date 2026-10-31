"""The assistant's own notebook for the continuous conversation.

The model already sees a [CONTINUIDADE] block each turn; this tool is how it
writes to that notebook while talking: a subject ended at 17:32, a lasting note
for later, the subject it is now on. It is silent bookkeeping, not something to
announce.
"""
from core import continuity


def continuity_tool(parameters: dict, player=None, speak=None,
                    session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "note").strip().lower()
    text = str(p.get("text") or "").strip()
    topic = str(p.get("topic") or "").strip()
    if action in ("note", "nota", "anotar", "lembrar"):
        return continuity.note(text)
    if action in ("open_topic", "abrir_assunto", "subject"):
        return continuity.open_topic(text or topic)
    if action in ("close_topic", "encerrar_assunto", "close"):
        return continuity.close_topic(topic, text)
    if action in ("recap", "resumo", "retomar"):
        return continuity.recap()
    return "Use action='note', 'open_topic', 'close_topic' ou 'recap'."


TOOL = {
    "name": "continuity",
    "description": (
        "Your own notebook for this continuous conversation. Use it silently "
        "when a subject clearly ends (action='close_topic', optional topic name "
        "and a one-line summary), when something worth keeping for later comes "
        "up (action='note'), when a new subject starts (action='open_topic'), or "
        "to recall the thread (action='recap'). This is how the conversation "
        "survives trimming and restarts; do not announce it. Never store "
        "passwords, keys or tokens."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "'note' | 'open_topic' | 'close_topic' | 'recap'"
            },
            "text": {
                "type": "STRING",
                "description": "The note, the subject name, or the closing "
                               "summary — short, in the user's language."
            },
            "topic": {
                "type": "STRING",
                "description": "Optional subject name for close_topic; defaults "
                               "to the current subject."
            },
        },
        "required": ["action"],
    },
    "handler": continuity_tool,
    "behavior": "NON_BLOCKING",
    "scheduling": "WHEN_IDLE",
}
