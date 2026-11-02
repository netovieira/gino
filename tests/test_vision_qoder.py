"""Vision in the cloud: the image reaches Qoder as a CLI attachment.

Phase 2 moved vision off the local Ollama VL. These tests never touch the
network: the CLI call is mocked. The one test that runs the real CLI is skipped
unless JARVIS_REAL_VISION=1 — an honest skip on a machine without the binary
or without a login, instead of a red suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from core import brain, qoder, vision


class SettingsTest(unittest.TestCase):
    def test_qoder_is_the_default_backend(self):
        st = vision.settings({})
        self.assertEqual(st["vision_provider"], "qoder")
        self.assertTrue(st["vision_enabled"])
        self.assertEqual(st["vision_qoder_model"], "Qwen3.8-Flash")
        self.assertEqual(st["vision_timeout"], vision.DEFAULT_TIMEOUT)

    def test_off_disables_vision(self):
        st = vision.settings({"vision_provider": "off"})
        self.assertFalse(st["vision_enabled"])
        self.assertFalse(vision.enabled({"vision_provider": "off"}))

    def test_local_is_still_a_choice(self):
        st = vision.settings({"vision_provider": "local"})
        self.assertTrue(st["vision_enabled"])
        self.assertTrue(brain._vision_settings(
            {"vision_provider": "local"})["vision_local"])

    def test_legacy_config_without_provider(self):
        # Phase-1 configs only have vision_local; false there meant "no vision".
        self.assertEqual(vision.provider_of({"vision_local": False}), "off")
        self.assertEqual(vision.provider_of({"vision_local": True}), "qoder")
        self.assertEqual(vision.provider_of({}), "qoder")

    def test_unknown_provider_keeps_vision_on(self):
        self.assertEqual(vision.provider_of({"vision_provider": "gpt"}), "qoder")

    def test_timeout_is_clamped(self):
        self.assertEqual(vision.settings({"vision_timeout": 1})["vision_timeout"],
                         vision.TIMEOUT_MIN)
        self.assertEqual(vision.settings({"vision_timeout": 9999})["vision_timeout"],
                         vision.TIMEOUT_MAX)

    def test_brain_settings_carry_one_vision_config(self):
        def plugin_config(namespace):
            return {"vision_provider": "qoder"} if namespace == "brain" else {}
        st = brain.settings(plugin_config)
        self.assertEqual(st["vision_provider"], "qoder")
        self.assertFalse(st["vision_local"])
        # The local backend stays configured for phase 3 to decide on.
        self.assertIn("11434", st["vision_base_url"])


class DescribeTest(unittest.TestCase):
    def _run(self):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stderr="",
            stdout=json.dumps({"type": "result", "subtype": "success",
                               "is_error": False,
                               "result": "um retângulo vermelho com o texto JARVIS"}))

    def test_image_is_attached_and_the_answer_parsed(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            path = cmd[cmd.index("--attachment") + 1]
            seen["attachment"] = path
            seen["bytes"] = open(path, "rb").read()
            return self._run()

        with mock.patch("core.qoder.find_cli", return_value="/usr/bin/qoder"), \
             mock.patch("core.qoder._find_cli", return_value="/usr/bin/qoder"), \
             mock.patch("core.qoder.subprocess.run", side_effect=fake_run) as run:
            text = vision.describe(b"\x89PNG\r\n\x1a\nfake", "image/png",
                                   "O que aparece?", timeout=30)
        self.assertIn("vermelho", text)
        self.assertEqual(run.call_count, 1)
        cmd = seen["cmd"]
        self.assertIn("--attachment", cmd)
        self.assertTrue(seen["attachment"].endswith(".png"))
        self.assertEqual(seen["bytes"], b"\x89PNG\r\n\x1a\nfake")
        self.assertEqual(cmd[cmd.index("--model") + 1], "Qwen3.8-Flash")
        # Vision must not be able to act: no tools in this turn.
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(seen["kwargs"]["timeout"], 30)
        # The temp file does not survive the call.
        self.assertFalse(os.path.exists(seen["attachment"]))

    def test_missing_cli_is_an_honest_failure(self):
        with mock.patch("core.qoder.find_cli", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                vision.describe(b"x", "image/png", "descreva")
        self.assertIn("qoder", str(ctx.exception).lower())

    def test_empty_answer_is_an_error_not_a_blank_description(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stderr="",
                stdout=json.dumps({"type": "result", "subtype": "success",
                                   "is_error": False, "result": "  "}))
        with mock.patch("core.qoder.find_cli", return_value="/usr/bin/qoder"), \
             mock.patch("core.qoder._find_cli", return_value="/usr/bin/qoder"), \
             mock.patch("core.qoder.subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                vision.describe(b"x", "image/png", "descreva")

    def test_large_screenshot_is_shrunk(self):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (4000, 2200), (255, 255, 255)).save(buf, format="PNG")
        data, suffix = vision._prepare(buf.getvalue(), "image/png")
        self.assertEqual(suffix, ".jpg")
        img = Image.open(io.BytesIO(data))
        self.assertLessEqual(max(img.size), vision.MAX_EDGE)


class DispatchTest(unittest.TestCase):
    def test_qoder_is_the_route(self):
        st = brain._vision_settings({"vision_provider": "qoder"})
        with mock.patch("core.vision.describe",
                        return_value="um retângulo") as describe:
            out = brain.vision_describe(b"img", "image/png", "pergunta", st)
        self.assertEqual(out, "um retângulo")
        describe.assert_called_once()
        self.assertEqual(describe.call_args.kwargs["model"], "Qwen3.8-Flash")

    def test_local_route_keeps_the_ollama_backend(self):
        st = brain._vision_settings({"vision_provider": "local"})
        with mock.patch("core.brain._vision_describe_local",
                        return_value="tela local") as local:
            out = brain.vision_describe(b"img", "image/png", "pergunta", st)
        self.assertEqual(out, "tela local")
        local.assert_called_once()

    def test_off_raises_instead_of_answering_from_imagination(self):
        st = brain._vision_settings({"vision_provider": "off"})
        with self.assertRaises(RuntimeError) as ctx:
            brain.vision_describe(b"img", "image/png", "pergunta", st)
        self.assertIn("desligada", str(ctx.exception).lower())


class CliEnvTest(unittest.TestCase):
    def test_sdk_session_variables_do_not_reach_the_cli(self):
        fake = {"PATH": "/usr/bin", "QODER_AGENT_SDK_ENTRYPOINT": "x",
                "QODER_SDK_AUTH_PAYLOAD_FILE": "y", "QODER_CLI": "/bin/qoder"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = qoder.cli_env()
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["QODER_CLI"], "/bin/qoder")
        self.assertNotIn("QODER_AGENT_SDK_ENTRYPOINT", env)
        self.assertNotIn("QODER_SDK_AUTH_PAYLOAD_FILE", env)


@unittest.skipUnless(
    os.environ.get("JARVIS_REAL_VISION") == "1" and qoder.find_cli(),
    "visão real: precisa do qoder CLI e de JARVIS_REAL_VISION=1")
class RealVisionTest(unittest.TestCase):
    """Evidence, not a regression test: a real PNG through the real CLI."""

    def test_red_rectangle_and_text_are_seen(self):
        from PIL import Image, ImageDraw
        box = tempfile.mkdtemp()
        path = os.path.join(box, "shot.png")
        img = Image.new("RGB", (520, 300), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 300, 240], fill=(220, 30, 30), outline=(0, 0, 0), width=3)
        d.text((340, 120), "JARVIS", fill=(0, 0, 0))
        img.save(path)
        desc = vision.describe(open(path, "rb").read(), "image/png",
                               "Descreva a cor, a forma e o texto da imagem em "
                               "uma frase, em português.")
        low = desc.lower()
        self.assertIn("vermelh", low)
        self.assertIn("jarvis", low)


if __name__ == "__main__":
    unittest.main()
