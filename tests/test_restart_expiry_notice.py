"""Expiração de pedido estacionado vira aviso ao dono, não silêncio (defeito 2).

O pedido estacionado às 11:44 expirou às 13:21 de 05/11/2026 sem nenhuma fala;
o dono ficou acreditando que o reinício pedido ia acontecer. Agora a
detecção de expiração dispara a frase de aviso pelo canal ligado em ``bind``.
"""
from __future__ import annotations

import time
from unittest import mock

from core.restart import CONFIRM_TIMEOUT_SECONDS, RestartManager


def _manager_with_expired_request():
    notices: list[str] = []
    manager = RestartManager(dry_run=True, logger=mock.Mock())
    manager.bind(expire_cb=notices.append)
    manager.request_assistant(reason="melhoria testada")
    # Envelhece o pedido além do TTL sem dormir: a expiração é checada em
    # qualquer leitura de estado.
    manager._pending.at = time.monotonic() - CONFIRM_TIMEOUT_SECONDS - 5
    return manager, notices


def test_expiry_of_parked_request_notifies_the_owner():
    manager, notices = _manager_with_expired_request()

    assert manager.pending() is None  # a leitura detecta e marca a expiração
    assert len(notices) == 1
    frase = notices[0]
    assert "expirou sem execução" in frase
    assert "criador" in frase
    assert "do assistente" in frase
    assert any(c.isdigit() for c in frase)  # hora do pedido presente


def test_expired_notice_carries_the_parking_hour():
    manager = RestartManager(dry_run=True, logger=mock.Mock())
    from core.restart import _PendingRequest
    at = time.monotonic() - 3600  # parado há uma hora
    esperado = time.strftime("%H:%M", time.localtime(
        time.time() - (time.monotonic() - at)))
    p = _PendingRequest(kind="pc", reason="x", phrase="confirmo reiniciar",
                        at=at)
    frase = manager.expired_notice(p)
    assert "do computador" in frase
    assert esperado in frase


def test_notice_fires_once_per_request_even_if_status_is_read_again():
    manager, notices = _manager_with_expired_request()
    manager.pending()
    manager.pending()  # já não há nada pendente; não repete o aviso
    assert len(notices) == 1


def test_broken_notice_callback_never_breaks_expiry():
    manager = RestartManager(dry_run=True, logger=mock.Mock())
    manager.bind(expire_cb=mock.Mock(side_effect=RuntimeError("sem boca")))
    manager.request_assistant()
    manager._pending.at = time.monotonic() - CONFIRM_TIMEOUT_SECONDS - 1
    assert manager.pending() is None  # expirou normalmente apesar do erro
