"""
Blocked phrases — commands the assistant must ignore even when they carry its name.

For a video, a stream or a recording in the room that keeps saying things
like "<name>, abre o navegador": once a phrase is on the list, any transcript
containing it is dropped on this machine before any model hears it.

    "<name>, bloqueia a frase abre o navegador"
    "<name>, quais frases estão bloqueadas?"
    "<name>, desbloqueia a frase abre o navegador"

The whole utterance must equal the phrase (case, accents, punctuation and
spaces ignored); a longer sentence containing it is NOT blocked. Stored in
config/api_keys.json as "blocked_phrases".

WHY THE RESULT STRINGS SOUND LIKE A REPORT, NOT AN ORDER
    They used to read "Blocked: '<phrase>'. I will ignore it from now on, even
    with my name." — first person, imperative-sounding. That string stays in the
    conversation history, and a later model (the Qoder CLI brain flattens the
    history into plain text) read it as an instruction planted inside the turn
    and answered the user about "hidden notes in the message". Tool output is
    data; it must never read like something the model is being told to obey.

WHY THERE IS A SIZE GUARD
    A 7B hearing music called this tool with a whole song lyric as the phrase,
    over and over, and the list filled with transcriptions nobody asked to
    block. A real blocked phrase is a short command; anything past the limits
    below is noise, and the honest answer is to refuse it instead of storing it.
"""
from __future__ import annotations

# A phrase worth blocking is a command, not a paragraph. Past this it is a
# transcription accident (a lyric, a TV monologue) — refuse rather than store.
MAX_PHRASE_CHARS = 80
MAX_PHRASES = 12

PLUGIN = {
    "name": "phrase_blocklist",
    "description": (
        "Manages the list of blocked phrases: spoken commands the assistant must ignore "
        "from now on even if they include its name — only when the WHOLE utterance "
        "equals the phrase (typically a sentence that keeps coming from a video "
        "or recording in the room). Actions: add a "
        "phrase, remove a phrase, list them. Use for 'bloqueia a frase/comando X', "
        "'ignora quando ouvir X', 'desbloqueia X', 'quais frases estão bloqueadas'. "
        "Call it ONLY when the user is explicitly asking to block or unblock "
        "something — never on ambient speech, music or a transcription you did not "
        "understand."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add | remove | list"},
            "phrase": {"type": "STRING", "description": "The phrase, as the user said it (without the assistant's name is fine)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    from memory.config_manager import get_blocked_phrases, save_blocked_phrases
    action = (parameters.get("action") or "list").strip().lower()
    phrase = (parameters.get("phrase") or "").strip().strip('"\'«»')
    phrases = get_blocked_phrases()
    if action == "list":
        return ("No blocked phrases." if not phrases
                else f"{len(phrases)} blocked phrase(s): " + "; ".join(phrases))
    if not phrase:
        return "Which phrase?"
    if action == "add":
        if any(p.lower() == phrase.lower() for p in phrases):
            return f"Already blocked: '{phrase}'."
        if len(phrase) > MAX_PHRASE_CHARS:
            return (f"Not blocked: a phrase has to be a short command, and that one "
                    f"is {len(phrase)} characters — it reads like a transcription "
                    f"of music or a video, not something to block.")
        if len(phrases) >= MAX_PHRASES:
            return (f"Not blocked: the list is full ({MAX_PHRASES} phrases). Remove "
                    f"one first with 'desbloqueia X'.")
        save_blocked_phrases(phrases + [phrase])
        return (f"Blocked phrase added: '{phrase}' ({len(phrases) + 1} "
                f"blocked now).")
    if action == "remove":
        keep = [p for p in phrases if phrase.lower() not in p.lower()]
        if len(keep) == len(phrases):
            return f"No blocked phrase matches '{phrase}'."
        save_blocked_phrases(keep)
        return f"Blocked phrase removed: '{phrase}' ({len(keep)} left)."
    return "Unknown action."
