"""
The spoken-confirmation restart gate, as a tool.

The actual protocol lives in core/restart.py: a request is only parked, and
only an explicit "confirmo reiniciar" runs it. This file is a thin, safe shell
around that manager so both the Live model (through auto-discovery) and the
Qoder bridge can reach it.

The module-level manager is bound once by main.py:

    from actions.restart_assistant import bind
    bind(restart_manager)

With nothing bound every call returns a clear error — the tool never falls back
to a restart nobody asked for.
"""
from __future__ import annotations

from typing import Optional

from core.restart import RestartManager

_manager: Optional[RestartManager] = None

_ASSISTANT_ACTIONS = {
    "assistant", "assistente", "self", "me", "jarvis", "restart_assistant",
}
_PC_ACTIONS = {
    "pc", "computer", "computador", "machine", "restart_pc",
}
_CONFIRM_ACTIONS = {"confirm", "confirmo", "confirmar"}
_CANCEL_ACTIONS = {"cancel", "cancelar", "abort"}
_STATUS_ACTIONS = {"status", "pending", "state"}


def bind(manager: Optional[RestartManager]) -> None:
    """Wire the tool to the app's RestartManager (None unbinds it)."""
    global _manager
    _manager = manager


def _status_text(manager: RestartManager) -> str:
    p = manager.pending()
    if p is not None:
        return (f"The {p['kind']} restart is awaiting the spoken phrase "
                f"'{p['phrase']}' ({p['expires_in']}s left). Nothing has restarted.")
    expired_fn = getattr(manager, "last_expired", None)
    expired = expired_fn() if callable(expired_fn) else None
    if expired:
        return (f"The {expired['kind']} restart request expired "
                f"{expired['expired_seconds_ago']}s ago before it was confirmed. "
                f"Nothing has restarted; ask for it again if still wanted.")
    return "No restart is awaiting confirmation."


def restart_assistant(parameters: dict, player=None, speak=None,
                      session_memory=None) -> str:
    """Model-facing handler. Only ever returns words, never a side effect of
    its own: the manager decides what a request and a confirmation mean."""
    if _manager is None:
        return ("Restart is not available: no RestartManager is bound to this "
                "tool. Nothing was done.")

    p = parameters or {}
    action = str(p.get("action") or "status").strip().lower()
    reason = str(p.get("reason") or "").strip()

    if action in _ASSISTANT_ACTIONS:
        return _manager.request_assistant(reason=reason)
    if action in _PC_ACTIONS:
        return _manager.request_pc(reason=reason)
    if action in _CONFIRM_ACTIONS:
        # Confirmation belongs to the owner's voice, not to the model. The
        # voice gate calls RestartManager.confirm directly; this tool never
        # confirms on the user's behalf.
        return ("Restart confirmation is voice-only: the owner must say the "
                "phrase out loud. Nothing was confirmed.")
    if action in _CANCEL_ACTIONS:
        return _manager.cancel()
    if action in _STATUS_ACTIONS:
        return _status_text(_manager)

    return (f"Unknown action '{action}'. Use action='assistant', 'pc', "
            f"'confirm', 'cancel' or 'status'. Nothing was done.")


TOOL = {
    "name": "restart_assistant",
    "description": (
        "Restart the assistant itself or the computer. Never happens on the "
        "first call: action='assistant' or action='pc' only parks a request and "
        "asks the owner to say the phrase out loud ('confirmo reiniciar'). The "
        "confirmation is voice-only; you cannot confirm it yourself. Never "
        "claim a restart happened before the owner speaks the phrase. "
        "action='cancel' drops a pending request; action='status' reports one. "
        "Never use the PC restart as part of an automatic improvement."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "'assistant' | 'pc' | 'cancel' | 'status'"
                ),
            },
            "reason": {
                "type": "STRING",
                "description": "Short reason for the restart (optional).",
            },
        },
        "required": ["action"],
    },
    "handler": restart_assistant,
}
