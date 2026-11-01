"""Regressão: uma sessão nova nunca retoma um pedido de reinício antigo.

O loop de 01/11 nascia em três portas: o arquivo pending de um processo
anterior, o prompt de uma sessão retomada e a sessão do Qoder que continuava a
conversa que pedia o reinício. Cada porta tem seu teste aqui.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import main
from core.restart import RestartManager


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class _Restart:
    def __init__(self, pending=None):
        self._pending = pending

    def pending(self):
        return self._pending


def _jarvis(**attrs):
    obj = object.__new__(main.JarvisLive)
    obj._system_prompt_text = "BASE"
    obj._chat_memory = None
    obj._qoder_resume_session = ""
    obj._restart = None
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def _prompt(obj) -> str:
    with mock.patch.object(main, "_sleep",
                           SimpleNamespace(latest_context=lambda: "")):
        return main.JarvisLive._qoder_system_prompt(obj)


def test_boot_prompt_says_no_restart_pending_even_with_old_history():
    obj = _jarvis(_qoder_resume_session="sessao-antiga", _restart=_Restart(None))

    prompt = _prompt(obj)

    assert "[ESTADO NO BOOT]" in prompt
    assert "NÃO há pedido de reinício pendente" in prompt
    assert "Não peça ao dono para confirmar um reinício que já aconteceu" in prompt
    # É o último bloco: vence o "confirme o reinício" do histórico retomado.
    assert prompt.rstrip().endswith("não repita pedidos antigos como se fossem atuais.")


def test_boot_prompt_reports_the_single_pending_request():
    pending = {"kind": "assistant",
               "phrase": "confirmo reiniciar o assistente"}
    obj = _jarvis(_restart=_Restart(pending))

    prompt = _prompt(obj)

    assert "Há UM pedido de reinício aguardando" in prompt
    assert "NÃO há pedido de reinício pendente" not in prompt


def test_stale_pending_file_never_reaches_the_new_session_prompt(tmp_path):
    """A sessão nova pergunta ao manager, e ele descarta o arquivo antigo."""
    state = tmp_path / "restart_pending.json"
    state.write_text(json.dumps({
        "kind": "assistant",
        "reason": "pedido que já foi cumprido",
        "phrase": "confirmo reiniciar o assistente",
        "created_at": time.time() - 3600.0,
    }), encoding="utf-8")
    manager = RestartManager(dry_run=True, logger=mock.Mock(), state_path=state)
    obj = _jarvis(_restart=manager)

    prompt = _prompt(obj)

    assert "NÃO há pedido de reinício pendente" in prompt
    assert not state.exists(), "o prompt novo não pode reler o pedido antigo"


def test_requested_restart_clears_the_resumed_session():
    obj = _jarvis()
    obj._clear_qoder_session = mock.Mock()
    obj._loop = None
    obj.session = None

    with mock.patch.object(main.threading, "Thread", _NoopThread):
        main.JarvisLive._restart_shutdown(obj)

    obj._clear_qoder_session.assert_called_once_with()


def test_clearing_the_session_removes_the_resume_point(tmp_path):
    path = tmp_path / "qoder_session.json"
    path.write_text('{"session": "sessao-antiga"}', encoding="utf-8")
    obj = _jarvis()
    obj._qoder_live = None
    obj._qoder_resume_session = "sessao-antiga"

    with mock.patch.object(main.JarvisLive, "_qoder_session_path",
                           staticmethod(lambda: path)):
        main.JarvisLive._clear_qoder_session(obj)

    assert not path.exists(), "a próxima sessão retomaria o histórico antigo"
    assert obj._qoder_resume_session == ""


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
