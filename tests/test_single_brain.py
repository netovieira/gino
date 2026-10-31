"""With Qoder Live on, every injected turn belongs to the one Qoder session."""
from __future__ import annotations

import threading
import unittest

import main


class FakeQueue:
    def __init__(self):
        self.items = []
    def put_nowait(self, item):
        self.items.append(item)


class FakeOwner:
    def __init__(self, enabled=True, ready=True):
        self._qoder_live_enabled = enabled
        self._ready = ready
        self._brain_q = FakeQueue()
        self._qoder_turn_backlog = []
        self.turns = []
        self.recorded = []
        self._qoder_lock = threading.RLock()

    def _qoder_live_ready(self):
        return self._ready

    def _qoder_turn(self, text, pcm=None, retry=False):
        self.turns.append(text)

    def _memory_record_user(self, text):
        self.recorded.append(text)

    def _qoder_hold(self, text, pcm=None, reason=""):
        self._qoder_turn_backlog.append({"text": text, "pcm": pcm})


async def _send(owner, text):
    session = main._EconomySession(owner)
    await session.send_client_content(
        turns={"role": "user", "parts": [{"text": text}]},
        turn_complete=True)


class SingleBrainTest(unittest.IsolatedAsyncioTestCase):
    async def test_ready_turn_goes_to_qoder_not_local(self):
        owner = FakeOwner()
        await _send(owner, "bom dia")
        self.assertEqual(owner.turns, ["bom dia"])
        self.assertEqual(owner.recorded, ["bom dia"])
        self.assertEqual(owner._brain_q.items, [])

    async def test_warming_turn_is_held_for_qoder(self):
        owner = FakeOwner(ready=False)
        await _send(owner, "bom dia")
        self.assertEqual(owner.turns, [])
        self.assertEqual([i["text"] for i in owner._qoder_turn_backlog],
                         ["bom dia"])
        self.assertEqual(owner._brain_q.items, [])

    async def test_flag_off_keeps_the_old_economy_path(self):
        owner = FakeOwner(enabled=False, ready=False)
        await _send(owner, "bom dia")
        self.assertEqual(owner._brain_q.items, ["bom dia"])
        self.assertEqual(owner.turns, [])


if __name__ == "__main__":
    unittest.main()
