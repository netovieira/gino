"""Named play requests run locally, without waking the conversational model."""
from __future__ import annotations

import threading
import unittest

import main


class FakeUI:
    def write_log(self, _text):
        pass

    def music_state(self):
        return ("stopped", "")


class PlayGateTest(unittest.TestCase):
    def _obj(self, calls):
        obj = object.__new__(main.JarvisLive)
        obj.ui = FakeUI()
        obj._say_now = lambda text, log=True: None
        obj._play_calls = calls
        return obj

    def test_named_play_is_parsed(self):
        self.assertEqual(main.JarvisLive._clean_play_query("Tiago York"), "Tiago York")
        self.assertEqual(main.JarvisLive._clean_play_query("a música Bohemian Rhapsody"),
                         "Bohemian Rhapsody")
        self.assertEqual(main.JarvisLive._clean_play_query("uma música do Queen"),
                         "Queen")
        self.assertEqual(main.JarvisLive._clean_play_query("isso"), "isso")

    def test_play_request_runs_the_action_directly(self):
        calls = []
        done = threading.Event()

        def fake_action(params, player=None, **kwargs):
            calls.append(params)
            done.set()
            return "ok"

        original = main._PLAY_ACTION
        main._PLAY_ACTION = fake_action
        try:
            obj = self._obj(calls)
            hit = main.JarvisLive._music_gate(obj, "Coloca Tiago York pra tocar")
            self.assertTrue(hit)
            self.assertTrue(done.wait(2.0))
            self.assertEqual(calls[0], {"action": "play", "query": "Tiago York"})
        finally:
            main._PLAY_ACTION = original

    def test_transport_words_are_not_treated_as_songs(self):
        obj = self._obj([])
        self.assertFalse(main.JarvisLive._music_gate(obj, "toca mais alto"))
        self.assertFalse(main.JarvisLive._music_gate(obj, "toca aí"))


if __name__ == "__main__":
    unittest.main()
