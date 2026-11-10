"""
The restart tool: own-process restart is a power, the machine is a gate.

The actual protocol lives in core/restart.py. Since the owner's order of
10/11/2026 restarting the assistant's OWN process needs no phrase — this tool
calls ``RestartManager.self_restart``, which carries the guards (never kill a
running background task, never cut an answer in flight, one restart per five
minutes) and writes the trail. The COMPUTER is unchanged: ``request_pc`` parks
the request and only the owner's exact phrase runs it. This file is a thin,
safe shell around that manager so both the Live model (through auto-discovery)
and the Qoder bridge can reach it.

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
    deferred_fn = getattr(manager, "deferred_self_restart", None)
    deferred = deferred_fn() if callable(deferred_fn) else None
    if deferred:
        return (f"An assistant PROCESS restart is postponed ({deferred['guard']}) "
                f"and will run by itself when that ends. Nothing has restarted yet.")
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
        # Dono, 10/11/2026: reiniciar o PRÓPRIO PROCESSO é poder do
        # assistente. Nenhuma frase, nenhuma janela de confirmação. A
        # origem da trilha é sempre "iniciativa propria": uma chamada de
        # ferramenta nunca prova que foi o dono quem falou.
        return _manager.self_restart(reason=reason)
    if action in _PC_ACTIONS:
        # A máquina não mudou: estaciona e só sai com a frase exata dele.
        return _manager.request_pc(reason=reason)
    if action in _CONFIRM_ACTIONS:
        # Confirmation belongs to the owner's factors, not to the model.
        # The voice gate and the verified typed password call
        # RestartManager.confirm (directly or through main's password
        # grant); this tool never confirms on the user's behalf.
        return ("Restart confirmation is owner-factor-only: the owner must "
                "say the phrase out loud or type his password. Nothing was "
                "confirmed.")
    if action in _CANCEL_ACTIONS:
        return _manager.cancel()
    if action in _STATUS_ACTIONS:
        return _status_text(_manager)

    return (f"Unknown action '{action}'. Use action='assistant', 'pc', "
            f"'confirm', 'cancel' or 'status'. Nothing was done.")


TOOL = {
    "name": "restart_assistant",
    "description": (
        "Two different powers, one tool. action='assistant' restarts YOUR OWN "
        "PROCESS: since the owner's order of 10/11/2026 that is your own power, "
        "so it runs without any phrase and without asking — yours to take when "
        "a test needs a clean boot, and anyone who asks for it triggers the "
        "same thing. It never restarts the machine. It is postponed (and "
        "happens by itself later) if a background task is still running or the "
        "current answer has not finished leaving; one restart per five minutes. "
        "action='pc' is the COMPUTER and has not changed: it only parks a "
        "request, and nothing runs until the owner says the exact phrase "
        "('confirmo reiniciar o computador') or types his password. That "
        "confirmation is owner-factor-only (decision of 06/11/2026) — you "
        "cannot confirm it yourself, and you must never claim the machine "
        "restarted. action='cancel' drops a pending PC request; "
        "action='status' reports what is pending or postponed. Tell the owner "
        "in one short sentence which of the two you did."
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
