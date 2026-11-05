"""
Owner password for typed authorization — settings form and a status tool.

The voiceprint stays the primary factor. This form lets the owner set a
password that can authorize by text in the UI or in his WhatsApp chat when the
microphone cannot recognize him. The plaintext is never stored: the settings
save path hashes it with PBKDF2 (core/owner_auth.py) before writing
``plugin_config.owner_auth.password_hash``.

The password opens two bounded windows from main.py:
  * the ordinary risky-action window (120 s, same one a spoken "pode" opens);
  * a 10-minute typed-owner session, which also unlocks the guided voice
    enrollment trigger if the old voice profile is too thin.
It never changes restart/power/email/WhatsApp gates by itself.
"""
from __future__ import annotations

from core import owner_auth

PLUGIN = {
    "name": "owner_auth_status",
    "description": (
        "Reports whether the owner's typed-authorization password is "
        "configured. Use when the user asks about authorizing by text, a "
        "password for the assistant, or why a typed command was refused."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

PLUGIN_SETTINGS = {
    "namespace": "owner_auth",
    "title": "OWNER AUTH — SENHA POR TEXTO (UI e WhatsApp)",
    "fields": [
        {"key": "password",
         "label": "Senha de autorização por texto",
         "type": "password",
         "placeholder": "deixe em branco para manter a senha atual"},
    ],
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    if owner_auth.is_configured():
        return ("A senha de autorização por texto está configurada. "
                "Use 'senha <sua senha>' na UI ou no chat direto do WhatsApp "
                "para abrir a autorização.")
    return ("Nenhuma senha de autorização por texto está configurada. "
            "Abra PLUGIN SETTINGS → OWNER AUTH e informe uma senha.")
