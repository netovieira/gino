"""media_control tests — MPRIS transport, YouTube Music opening, volume, status.

Every subprocess and network call is mocked: nothing here touches the real
session bus, audio sink or browser.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from actions import media_control as mc


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


BUSCTL_OUT = "\n".join([
    "org.mpris.MediaPlayer2.chrome.instance4242 1000 chrome user - -",
    ":1.55 1000 mpris-proxy user - -",
    "org.mpris.MediaPlayer2.playerctld 1000 playerctld user - -",
    "org.mpris.MediaPlayer2.spotify 2000 spotify user - -",
    "com.example.Unrelated 3000 other user - -",
]) + "\n"

SPOTIFY = "org.mpris.MediaPlayer2.spotify"


class ToolDeclarationTests(unittest.TestCase):
    def test_tool_shape(self):
        tool = mc.TOOL
        self.assertEqual(tool["name"], "media_control")
        self.assertIs(tool["handler"], mc.media_control)
        props = tool["parameters"]["properties"]
        self.assertEqual(
            props["action"]["enum"],
            ["play", "pause", "resume", "next", "previous", "stop",
             "volume", "status"],
        )
        self.assertIn("query", props)
        self.assertIn("value", props)


class PlayersTests(unittest.TestCase):
    def test_parses_bus_names_and_ignores_proxies(self):
        with patch.object(mc.subprocess, "run", return_value=_cp(stdout=BUSCTL_OUT)) as run:
            found = mc.players()
        self.assertEqual(found, ["org.mpris.MediaPlayer2.chrome.instance4242", SPOTIFY])
        self.assertEqual(run.call_args.args[0],
                         ["busctl", "--user", "list", "--no-legend"])

    def test_busctl_failure_returns_empty(self):
        with patch.object(mc.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(mc.players(), [])

    def test_busctl_nonzero_returns_empty(self):
        with patch.object(mc.subprocess, "run", return_value=_cp(returncode=1)):
            self.assertEqual(mc.players(), [])


class TransportTests(unittest.TestCase):
    def test_transport_methods_are_sent_without_reading_state(self):
        expected = {"pause": "Pause", "resume": "Play", "next": "Next",
                    "previous": "Previous", "stop": "Stop"}
        for action, method in expected.items():
            with self.subTest(action=action):
                with patch.object(mc, "players", return_value=[SPOTIFY]), \
                        patch.object(mc.subprocess, "run", return_value=_cp()) as run:
                    out = mc.media_control({"action": action})
                self.assertIn(method, out)
                self.assertEqual(run.call_count, 1)  # one gdbus call, no status first
                cmd = run.call_args.args[0]
                self.assertEqual(
                    cmd,
                    ["gdbus", "call", "--session", "--dest", SPOTIFY,
                     "--object-path", "/org/mpris/MediaPlayer2",
                     "--method", f"org.mpris.MediaPlayer2.Player.{method}"],
                )

    def test_no_player_is_a_clear_error_and_sends_nothing(self):
        with patch.object(mc, "players", return_value=[]), \
                patch.object(mc.subprocess, "run") as run:
            out = mc.media_control({"action": "pause"})
        self.assertIn("Nenhum player aberto", out)
        run.assert_not_called()

    def test_failed_transport_reports_the_error_text(self):
        failed = _cp(returncode=1, stderr="No such name")
        with patch.object(mc, "players", return_value=[SPOTIFY]), \
                patch.object(mc.subprocess, "run", return_value=failed):
            out = mc.media_control({"action": "next"})
        self.assertIn("No such name", out)


class PlayTests(unittest.TestCase):
    def test_play_with_no_query_and_no_player_opens_youtube_music_visibly(self):
        with patch.object(mc, "players", return_value=[]), \
                patch.object(mc, "_chrome_bin", return_value="google-chrome"), \
                patch.object(mc.subprocess, "Popen") as popen:
            out = mc.media_control({"action": "play"})
        self.assertIn("YouTube Music", out)
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd, ["google-chrome", "--new-window",
                               "https://music.youtube.com"])

    def test_play_with_no_query_and_a_player_sends_play(self):
        with patch.object(mc, "players", return_value=[SPOTIFY]), \
                patch.object(mc.subprocess, "run", return_value=_cp()) as run, \
                patch.object(mc.subprocess, "Popen") as popen:
            out = mc.media_control({"action": "play"})
        self.assertIn("Play", out)
        popen.assert_not_called()
        self.assertTrue(run.call_args.args[0][-1].endswith(".Play"))

    def test_play_query_scrape_failure_opens_search_url(self):
        with patch.object(mc.requests, "get", side_effect=RuntimeError("offline")), \
                patch.object(mc, "_chrome_bin", return_value="google-chrome"), \
                patch.object(mc.subprocess, "Popen") as popen:
            out = mc.media_control({"action": "play", "query": "daft punk"})
        self.assertIn("tela", out)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd, ["google-chrome", "--new-window",
                               "https://music.youtube.com/search?q=daft+punk"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_play_query_skips_shorts_and_opens_the_watch_url(self):
        html = (
            '"videoId":"aaaaaaaaaaa"'
            '{"url":"/shorts/aaaaaaaaaaa"}'
            '"videoId":"bbbbbbbbbbb"'
        )
        response = MagicMock(text=html)
        with patch.object(mc.requests, "get", return_value=response) as get, \
                patch.object(mc, "_chrome_bin", return_value="google-chrome"), \
                patch.object(mc.subprocess, "Popen") as popen:
            out = mc.media_control({"action": "play", "query": "one more time"})
        self.assertIn("YouTube Music", out)
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd, ["google-chrome", "--new-window",
                               "https://music.youtube.com/watch?v=bbbbbbbbbbb"])
        response_url = get.call_args.args[0]
        self.assertIn("music.youtube.com/search?q=one+more+time", response_url)

    def test_open_falls_back_to_xdg_open_without_chrome(self):
        with patch.object(mc, "_chrome_bin", return_value=""), \
                patch.object(mc.subprocess, "Popen") as popen:
            out = mc.media_control({"action": "play", "query": "x"})
        self.assertTrue(popen.called)
        self.assertEqual(popen.call_args.args[0][0], "xdg-open")


class VolumeTests(unittest.TestCase):
    def test_volume_up_uses_wpctl(self):
        with patch.object(mc.subprocess, "run", return_value=_cp()) as run:
            out = mc.media_control({"action": "volume", "value": "up"})
        self.assertIn("aumentado", out)
        self.assertEqual(run.call_args.args[0],
                         ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%+"])

    def test_volume_down_uses_wpctl(self):
        with patch.object(mc.subprocess, "run", return_value=_cp()) as run:
            mc.media_control({"action": "volume", "value": "down"})
        self.assertEqual(run.call_args.args[0],
                         ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"])

    def test_mute_toggles_with_wpctl(self):
        with patch.object(mc.subprocess, "run", return_value=_cp()) as run:
            mc.media_control({"action": "volume", "value": "mute"})
        self.assertEqual(run.call_args.args[0],
                         ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"])

    def test_absolute_volume_uses_a_fraction(self):
        with patch.object(mc.subprocess, "run", return_value=_cp()) as run:
            out = mc.media_control({"action": "volume", "value": "55"})
        self.assertIn("55%", out)
        self.assertEqual(run.call_args.args[0],
                         ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.55"])

    def test_wpctl_missing_falls_back_to_pactl(self):
        with patch.object(mc.subprocess, "run",
                          side_effect=[FileNotFoundError, _cp()]) as run:
            out = mc.media_control({"action": "volume", "value": "mute"})
        self.assertIn("Mudo", out)
        self.assertEqual(run.call_args.args[0],
                         ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])

    def test_invalid_volume_value_is_rejected(self):
        with patch.object(mc.subprocess, "run") as run:
            out = mc.media_control({"action": "volume", "value": "loud"})
        self.assertIn("0 a 100", out)
        run.assert_not_called()


class StatusTests(unittest.TestCase):
    def test_status_lists_players_and_reads_metadata(self):
        metadata = ("({'mpris:trackid': <objectpath '/x'>, "
                    "'xesam:artist': <['Daft Punk']>, "
                    "'xesam:title': <'One More Time'>},)")
        with patch.object(mc, "players", return_value=[SPOTIFY]), \
                patch.object(mc.subprocess, "run", return_value=_cp(stdout=metadata)) as run:
            out = mc.media_control({"action": "status"})
        self.assertIn(SPOTIFY, out)
        self.assertIn("One More Time", out)
        self.assertIn("Daft Punk", out)
        self.assertIn("Player.Metadata", run.call_args.args[0][-1])

    def test_status_with_no_players(self):
        with patch.object(mc, "players", return_value=[]):
            out = mc.media_control({"action": "status"})
        self.assertIn("Nenhum player", out)

    def test_status_survives_metadata_failure(self):
        with patch.object(mc, "players", return_value=[SPOTIFY]), \
                patch.object(mc.subprocess, "run", return_value=_cp(returncode=1)):
            out = mc.media_control({"action": "status"})
        self.assertIn(SPOTIFY, out)


class RobustnessTests(unittest.TestCase):
    def test_unknown_action_is_explained(self):
        out = mc.media_control({"action": "explode"})
        self.assertIn("desconhecida", out)

    def test_handler_never_raises_when_players_explodes(self):
        with patch.object(mc, "players", side_effect=RuntimeError("bus down")):
            out = mc.media_control({"action": "pause"})
        self.assertIn("Falha", out)


if __name__ == "__main__":
    unittest.main()
