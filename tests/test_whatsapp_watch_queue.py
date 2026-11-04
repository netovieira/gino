"""A thread de dispatch do neonize não faz trabalho lento.

O observador de core/whatsapp roda na MESMA thread que entrega as mensagens de
todas as contas. Se ele transcrever uma nota de voz ali, um ffmpeg de 90 s
segura a fila inteira. Aqui o item só é enfileirado; o worker da conta faz o
trabalho, em ordem de chegada e sem duplicar transcrição/resposta.
"""
from __future__ import annotations

import threading
import time

import pytest

from core import actiongate, whatsapp_watch

ACCOUNT = "principal"
OTHER = "trabalho"
CHAT = "5511999999999"
SLOW_S = 1.0


def wa_item(item_id: str, chat: str = CHAT, **overrides) -> dict:
    item = {
        "ts": int(time.time()),
        "chat": chat,
        "name": "Vida",
        "from_me": False,
        "group": False,
        "text": "Gino, tudo bem?",
        "id": item_id,
        "media": None,
    }
    item.update(overrides)
    return item


def _wait_for(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def watcher():
    actiongate.close()
    whatsapp_watch.reset()
    order: list[str] = []
    started: dict[str, float] = {}
    guard = threading.Lock()

    def handler(account: str, item: dict) -> bool:
        with guard:
            order.append(f"{account}:{item.get('id')}")
            started[item.get("id")] = time.monotonic()
        if str(item.get("id") or "").startswith("slow"):
            time.sleep(SLOW_S)
        return True

    whatsapp_watch.start(handler, logger=lambda _m: None,
                         assistant_name="Gino")
    try:
        yield order, started, guard
    finally:
        whatsapp_watch.stop()
        whatsapp_watch.reset()
        actiongate.close()


def test_slow_media_item_returns_from_dispatch_immediately(watcher):
    """Handler dormindo (download/STT lento) não prende a thread do neonize."""
    order, _started, guard = watcher
    begin = time.monotonic()
    whatsapp_watch._dispatch(ACCOUNT, wa_item("slow-1"))
    elapsed = time.monotonic() - begin

    assert elapsed < SLOW_S * 0.7, (
        f"dispatch esperou {elapsed:.2f}s por um item de {SLOW_S:.1f}s")
    with guard:
        assert order == [f"{ACCOUNT}:slow-1"], "o item ainda está no worker"

    # As mensagens seguintes continuam entrando na fila sem esperar o lento.
    begin = time.monotonic()
    whatsapp_watch._dispatch(ACCOUNT, wa_item("fast-2"))
    whatsapp_watch._dispatch(ACCOUNT, wa_item("fast-3"))
    assert time.monotonic() - begin < SLOW_S * 0.7

    assert _wait_for(lambda: len(order) == 3)
    with guard:
        assert order == [f"{ACCOUNT}:slow-1", f"{ACCOUNT}:fast-2",
                         f"{ACCOUNT}:fast-3"]


def test_fifo_order_is_preserved_per_account(watcher):
    order, _started, guard = watcher
    ids = ["fifo-1", "fifo-2", "fifo-3", "fifo-4"]
    for item_id in ids:
        whatsapp_watch._dispatch(ACCOUNT, wa_item(item_id))

    assert _wait_for(lambda: len(order) == len(ids))
    with guard:
        assert order == [f"{ACCOUNT}:{item_id}" for item_id in ids]


def test_duplicate_event_is_transcribed_and_answered_once(watcher):
    order, _started, guard = watcher
    item = wa_item("dup-1", media={"kind": "mensagem de voz", "ext": ".ogg"})
    whatsapp_watch._dispatch(ACCOUNT, item)
    whatsapp_watch._dispatch(ACCOUNT, dict(item))
    whatsapp_watch._dispatch(ACCOUNT, dict(item))

    assert _wait_for(lambda: len(order) >= 1)
    time.sleep(0.15)
    with guard:
        assert order == [f"{ACCOUNT}:dup-1"]


def test_each_account_has_its_own_worker(watcher):
    """Nota lenta na principal não atrasa uma mensagem da conta de trabalho."""
    order, started, guard = watcher
    whatsapp_watch._dispatch(ACCOUNT, wa_item("slow-p", chat="5511888888888"))

    begin = time.monotonic()
    whatsapp_watch._dispatch(OTHER, wa_item("fast-t", chat="5511777777777"))
    elapsed = time.monotonic() - begin

    assert elapsed < SLOW_S * 0.7
    with guard:
        assert f"{OTHER}:fast-t" in order, "a outra conta parou junto"
        assert f"{ACCOUNT}:slow-p" in order
    assert started["fast-t"] < started["slow-p"] + SLOW_S

    assert _wait_for(lambda: len(order) == 2)
