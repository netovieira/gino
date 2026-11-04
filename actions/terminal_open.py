"""Abre uma janela de terminal interativa vazia, sem rodar comando.

Abrir a janela é ação de interface, não execução: o dono pediu que isso nunca
peça permissão. Quem quer rodar algo usa ``terminal_run``, cuja política de
risco olha o comando (destrutivo continua confirmado).
"""
from __future__ import annotations

from actions import terminal


def terminal_open(parameters, player=None, speak=None, session_memory=None) -> str:
    # terminal.terminal_open drops any ``command``: this tool only opens the
    # window, it never types a model-provided command into it.
    return str(terminal.terminal_open(parameters))


TOOL = {
    "name": "terminal_open",
    "description": (
        "Abre uma janela de terminal interativa vazia (bash), para o dono "
        "usar ou acompanhar. Não executa comando nenhum e não pede "
        "confirmação. Para rodar um comando visível, use terminal_run."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "cwd": {
                "type": "STRING",
                "description": "Pasta onde o bash abre (opcional).",
            },
            "title": {
                "type": "STRING",
                "description": "Título da janela/aba (opcional).",
            },
        },
        "required": [],
    },
    "handler": terminal_open,
    "behavior": "NON_BLOCKING",
    # A boca não anuncia a ação (regra do dono, turno 22). O resultado não
    # reentra na conversa pedindo uma frase.
    "scheduling": "SILENT",
}
