"""Aviso do dono no WhatsApp: gancho na thread do neonize e gaveta do despertar.

O core/whatsapp roda o gancho `_whatsapp_owner_wake` na thread do neonize. Aqui
o gancho é chamado de uma thread de verdade, o loop é falso (só registra o
`call_soon_threadsafe`) e a gaveta é a do core (`_owner_arrived`), como em
produção. Nenhum teste toca rede, disco ou o áudio da sala.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

import main
from core import whatsapp

OWNER_CHAT = "5517981792464"


class FakeUI:
    def __init__(self):
        self.muted = False
        self.state = ""
        self.lines = []

    def write_log(self, text):
        self.lines.append(text)

    def set_state(self, state):
        self.state = state


class FakeLoop:
    """Registra o agendamento; o teste executa no "loop" quando quiser."""

    def __init__(self):
        self.scheduled = []
        self.closed = False

    def call_soon_threadsafe(self, fn, *args):
        self.scheduled.append((fn, args))

    def is_closed(self):
        return self.closed

    def run(self):
        pending, self.scheduled = self.scheduled, []
        for fn, args in pending:
            fn(*args)
        return len(pending)


def owner_item(text: str, message_id: str) -> dict:
    return {
        "account": "trabalho",
        "chat": OWNER_CHAT,
        "name": "Anthero",
        "from_me": True,
        "group": False,
        "text": text,
        "id": message_id,
        "media": None,
    }


class OwnerWakeTest(unittest.TestCase):
    def setUp(self):
        whatsapp.drain_owner_pending()
        self._hooks_before = list(whatsapp._alert_hooks)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for hook in list(whatsapp._alert_hooks):
            if hook not in self._hooks_before:
                whatsapp.remove_owner_wake_hook(hook)
        whatsapp.drain_owner_pending()

    def _obj(self, awake: bool = True) -> main.JarvisLive:
        obj = object.__new__(main.JarvisLive)
        obj._awake = awake
        obj._asst_name = "Gino"
        obj._groq = None
        obj.ui = FakeUI()
        obj.session = object()
        obj._loop = FakeLoop()
        obj._inject_user_turn = mock.Mock(return_value=True)
        return obj

    @staticmethod
    def _from_neonize_thread(obj, account: str, item: dict) -> bool:
        """Roda o gancho na thread do neonize, como o core faz."""
        result: list[bool] = []
        thread = threading.Thread(
            target=lambda: result.append(
                obj._whatsapp_owner_wake(account, dict(item))))
        thread.start()
        thread.join(5)
        return bool(result and result[0])

    @staticmethod
    def _deliver_on_neonize_thread(obj, account: str, item: dict) -> None:
        """Entrega pelo caminho real do core: gaveta quando o gancho diz False."""
        whatsapp.add_owner_wake_hook(obj._whatsapp_owner_wake)
        thread = threading.Thread(target=whatsapp._owner_arrived,
                                  args=(account, dict(item)))
        thread.start()
        thread.join(5)

    def test_drawer_drains_on_wake_with_a_single_announcement(self):
        obj = self._obj(awake=False)
        whatsapp.add_owner_wake_hook(obj._whatsapp_owner_wake)
        whatsapp._owner_arrived("trabalho",
                                owner_item("cheguei em casa", "WA-1"))
        whatsapp._owner_arrived("trabalho",
                                owner_item("traz o carregador", "WA-2"))

        self.assertEqual(len(whatsapp.pending_owner()), 2)
        self.assertEqual(obj._loop.scheduled, [])

        obj.wake(reason="palavra de ativação")

        self.assertTrue(obj._awake)
        self.assertEqual(whatsapp.pending_owner(), [])
        obj._inject_user_turn.assert_called_once()
        line = obj._inject_user_turn.call_args[0][0]
        self.assertIn("cheguei em casa", line)
        self.assertIn("traz o carregador", line)
        self.assertIn("2 mensagens", line)
        self.assertTrue(any("gaveta entregue" in text
                            for text in obj.ui.lines))

    def test_awake_hook_announces_and_keeps_nothing_in_the_drawer(self):
        obj = self._obj()
        whatsapp.add_owner_wake_hook(obj._whatsapp_owner_wake)

        whatsapp._owner_arrived("trabalho",
                                owner_item("olha o pedido novo", "WA-3"))

        # O core entregou: o gancho aceitou e a gaveta ficou vazia.
        self.assertEqual(whatsapp.pending_owner(), [])
        self.assertEqual(len(obj._loop.scheduled), 1)
        self.assertEqual(obj._loop.run(), 1)
        obj._inject_user_turn.assert_called_once()
        line = obj._inject_user_turn.call_args[0][0]
        self.assertIn("olha o pedido novo", line)
        self.assertIn("uma mensagem", line)

    def test_asleep_hook_returns_false_and_the_item_stays_in_the_drawer(self):
        obj = self._obj(awake=False)

        self._deliver_on_neonize_thread(
            obj, "trabalho", owner_item("acorda", "WA-4"))

        self.assertEqual([q["id"] for q in whatsapp.pending_owner()], ["WA-4"])
        self.assertEqual(obj._loop.scheduled, [])
        obj._inject_user_turn.assert_not_called()

    def test_muted_mic_still_delivers_the_owner_message(self):
        # Ordem do dono (07/11/2026): o mute da UI fecha só o microfone. A
        # mensagem escrita dele no WhatsApp tem resposta na hora, mute ou não.
        obj = self._obj()
        obj.ui.muted = True

        self._deliver_on_neonize_thread(
            obj, "trabalho", owner_item("preciso de você", "WA-5"))

        self.assertEqual(whatsapp.pending_owner(), [])
        self.assertEqual(len(obj._loop.scheduled), 1)
        obj._loop.run()
        obj._inject_user_turn.assert_called_once()
        line = obj._inject_user_turn.call_args[0][0]
        self.assertIn("preciso de você", line)

    def test_no_session_returns_false_without_draining(self):
        obj = self._obj()
        obj.session = None

        self._deliver_on_neonize_thread(
            obj, "trabalho", owner_item("e aí", "WA-6"))

        self.assertEqual([q["id"] for q in whatsapp.pending_owner()], ["WA-6"])

    def test_wake_without_session_does_not_lose_the_drawer(self):
        obj = self._obj(awake=False)
        whatsapp.add_owner_wake_hook(obj._whatsapp_owner_wake)
        whatsapp._owner_arrived("trabalho",
                                owner_item("guardado", "WA-7"))
        obj.session = None

        obj.wake(reason="palavra de ativação")

        self.assertEqual([q["id"] for q in whatsapp.pending_owner()], ["WA-7"])
        obj._inject_user_turn.assert_not_called()

    def test_hook_is_thread_safe_and_never_touches_the_voice_sink(self):
        obj = self._obj()
        obj.voice_sink = mock.Mock()
        whatsapp.add_owner_wake_hook(obj._whatsapp_owner_wake)

        self._from_neonize_thread(
            obj, "trabalho", owner_item("mensagem direta", "WA-8"))
        obj._loop.run()

        obj.voice_sink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
