"""Physical microphone identity and the AEC follow file (core/mic_route.py).

All parsing is pure: the fixtures below are the real shapes from this machine
(``pactl list short sources`` and the owner's 50-echo-cancel.conf), so the
tests never touch PulseAudio or the owner's configuration.
"""
from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import mic_route

C920_NODE = "alsa_input.usb-046d_HD_Pro_Webcam_C920_FED801EF-02.analog-stereo"
EDIFIER_NODE = ("alsa_input.usb-EDFIER_EDIFIER_W820NB_Plus_USBCompositeDevic-00"
                ".mono-fallback")

SOURCES = (
    "38\techo_cancel_source\tPipeWire\tfloat32le 2ch 32000Hz\tRUNNING\n"
    f"64\t{C920_NODE}\tPipeWire\ts16le 2ch 32000Hz\tRUNNING\n"
    f"8422\t{EDIFIER_NODE}\tPipeWire\ts16le 1ch 48000Hz\tSUSPENDED\n"
    "68\talsa_output.pci-0000_0c_00.4.analog-stereo.monitor\tPipeWire\t"
    "s32le 2ch 48000Hz\tSUSPENDED\n"
)

LEGACY_CONF = f'''
context.modules = [
    {{ name = libpipewire-module-echo-cancel
        flags = [ nofail ]
        args = {{
            library.name   = aec/libspa-aec-webrtc
            audio.rate     = 32000
            monitor.mode   = true
            audio.channels = 2
            capture.props = {{
                node.name     = "echo_cancel_capture"
                target.object = "{C920_NODE}"
            }}
            source.props = {{
                node.name        = "echo_cancel_source"
                node.description = "Microfone AEC (C920)"
                media.class      = "Audio/Source"
            }}
        }}
    }}
]
'''


class MicRouteParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = mic_route.parse_sources(SOURCES)
        self.chains = mic_route.parse_filter_chains(LEGACY_CONF)

    def test_sources_are_parsed_with_rate_and_monitor_flag(self):
        names = [row["name"] for row in self.sources]
        self.assertIn("echo_cancel_source", names)
        self.assertIn(C920_NODE, names)
        self.assertEqual(len(self.sources), 4)
        monitor = next(r for r in self.sources if r["name"].endswith(".monitor"))
        self.assertTrue(monitor["monitor"])
        edifier = next(r for r in self.sources if r["name"] == EDIFIER_NODE)
        self.assertEqual(edifier["rate"], 48000)

    def test_filter_chain_exposes_capture_target_and_virtual_source(self):
        self.assertEqual(len(self.chains), 1)
        chain = self.chains[0]
        self.assertEqual(chain["node_name"], "echo_cancel_source")
        self.assertEqual(chain["capture_node"], "echo_cancel_capture")
        self.assertEqual(chain["target"], C920_NODE)
        self.assertEqual(chain["rate"], 32000)
        self.assertEqual(chain["channels"], 2)

    def test_aec_source_resolves_to_its_physical_hardware(self):
        route = mic_route.physical_source(
            "echo_cancel_source", sources=self.sources, chains=self.chains,
            default=EDIFIER_NODE)
        self.assertEqual(route["source"], "echo_cancel_source")
        self.assertEqual(route["physical"], C920_NODE)
        self.assertEqual(route["key"], mic_route.slug(C920_NODE))
        self.assertTrue(route["aec"])
        # A profile key must be stable under the index's own slug rule.
        from core import speaker_id
        self.assertEqual(speaker_id._slugify(route["key"]), route["key"])

    def test_plain_source_keeps_its_own_identity(self):
        route = mic_route.physical_source(
            EDIFIER_NODE, sources=self.sources, chains=self.chains,
            default=EDIFIER_NODE)
        self.assertEqual(route["physical"], EDIFIER_NODE)
        self.assertEqual(route["key"], mic_route.slug(EDIFIER_NODE))
        self.assertFalse(route["aec"])

    def test_default_source_when_config_is_empty(self):
        route = mic_route.physical_source(
            "", sources=self.sources, chains=self.chains, default=EDIFIER_NODE)
        self.assertEqual(route["physical"], EDIFIER_NODE)
        self.assertEqual(route["key"], mic_route.slug(EDIFIER_NODE))

    def test_default_source_that_is_aec_resolves_through_the_chain(self):
        route = mic_route.physical_source(
            "", sources=self.sources, chains=self.chains,
            default="echo_cancel_source")
        self.assertEqual(route["physical"], C920_NODE)
        self.assertTrue(route["aec"])

    def test_aec_description_also_resolves(self):
        route = mic_route.physical_source(
            "Microfone AEC (C920)", sources=self.sources, chains=self.chains)
        self.assertEqual(route["physical"], C920_NODE)

    def test_unknown_name_falls_back_to_its_own_slug(self):
        route = mic_route.physical_source(
            "Microfone Fantasma", sources=self.sources, chains=self.chains)
        self.assertEqual(route["key"], "microfone-fantasma")

    def test_garbage_never_raises(self):
        self.assertEqual(mic_route.parse_sources(""), [])
        self.assertEqual(mic_route.parse_filter_chains(""), [])
        self.assertEqual(mic_route.slug(None), "")


class MicRouteManagedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_managed_conf_targets_the_hardware_and_keeps_a_stable_node(self):
        result = mic_route.write_managed_conf(
            EDIFIER_NODE, directory=self.dir, rate=48000, channels=1,
            short="EDIFIER")
        self.assertTrue(result["changed"])
        path = Path(result["path"])
        self.assertEqual(path.name, mic_route.MANAGED_CONF_NAME)
        text = path.read_text(encoding="utf-8")
        self.assertIn(f'target.object = "{EDIFIER_NODE}"', text)
        self.assertIn(f'node.name        = "{mic_route.MANAGED_NODE}"', text)
        self.assertIn("audio.rate     = 48000", text)
        self.assertIn("audio.channels = 1", text)
        self.assertIn("audio.position = [ MONO ]", text)
        # Writing it again with identical content is a no-op (no daemon churn).
        again = mic_route.write_managed_conf(
            EDIFIER_NODE, directory=self.dir, rate=48000, channels=1,
            short="EDIFIER")
        self.assertFalse(again["changed"])

    def test_retarget_creates_one_backup_of_our_own_file(self):
        mic_route.write_managed_conf(C920_NODE, directory=self.dir, short="C920")
        mic_route.write_managed_conf(EDIFIER_NODE, directory=self.dir, short="EDF")
        backups = list(self.dir.glob(f"{mic_route.MANAGED_CONF_NAME}.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(C920_NODE, backups[0].read_text(encoding="utf-8"))
        # The owner's legacy file is never created or touched by us.
        self.assertFalse((self.dir / mic_route.LEGACY_CONF_NAME).exists())

    def test_monitor_and_empty_targets_are_refused(self):
        with self.assertRaises(ValueError):
            mic_route.write_managed_conf("", directory=self.dir)
        with self.assertRaises(ValueError):
            mic_route.write_managed_conf(
                "alsa_output.usb-x.analog-stereo.monitor", directory=self.dir)

    def test_follow_never_rewrites_a_legacy_chain(self):
        legacy = self.dir / mic_route.LEGACY_CONF_NAME
        legacy.write_text(LEGACY_CONF, encoding="utf-8")
        result = mic_route.follow_managed(
            "echo_cancel_source", target=EDIFIER_NODE, directory=self.dir,
            restart=False)
        self.assertFalse(result["written"])
        self.assertIn("não gerenciado", result["reason"])
        self.assertEqual(legacy.read_text(encoding="utf-8"), LEGACY_CONF)
        self.assertFalse((self.dir / mic_route.MANAGED_CONF_NAME).exists())

    def test_follow_retargets_the_managed_chain(self):
        mic_route.write_managed_conf(C920_NODE, directory=self.dir, short="C920")
        result = mic_route.follow_managed(
            mic_route.MANAGED_NODE, target=EDIFIER_NODE, directory=self.dir,
            restart=False)
        self.assertTrue(result["written"])
        text = (self.dir / mic_route.MANAGED_CONF_NAME).read_text(encoding="utf-8")
        self.assertIn(EDIFIER_NODE, text)
        self.assertNotIn(C920_NODE, text)


class MicRouteMultiModuleTest(unittest.TestCase):
    """Several echo-cancel modules in the ONE managed file.

    The first module must keep the bare node name: ``input_device =
    jarvis_aec_source`` and the voice profile key are saved in the owner's
    config and must not move when a second microphone is added.
    """

    SECOND = "alsa_input.usb-046d_C925-00.analog-stereo"
    THIRD = "alsa_input.pci-0000_00_1f.3.analog-stereo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_names_are_bare_first_then_indexed(self):
        self.assertEqual(mic_route.managed_node_names(1), ["jarvis_aec_source"])
        self.assertEqual(mic_route.managed_node_names(3),
                         ["jarvis_aec_source", "jarvis_aec_source_2",
                          "jarvis_aec_source_3"])
        self.assertEqual(mic_route.managed_capture_names(2),
                         ["jarvis_aec_capture", "jarvis_aec_capture_2"])
        self.assertEqual(mic_route.managed_node_names(0), ["jarvis_aec_source"])

    def test_three_microphones_render_three_modules(self):
        text = mic_route.render_managed_conf(
            [C920_NODE, self.SECOND, self.THIRD],
            rate=[32000, 48000, 16000],
            channels=[2, 1, 1],
            short=["C920", "C925", "Interno"],
        )
        self.assertEqual(text.count("libpipewire-module-echo-cancel"), 3)
        self.assertEqual(text.count("context.modules"), 1)
        self.assertIn(f'target.object = "{C920_NODE}"', text)
        self.assertIn(f'target.object = "{self.SECOND}"', text)
        self.assertIn(f'target.object = "{self.THIRD}"', text)
        self.assertIn('node.name        = "jarvis_aec_source"', text)
        self.assertIn('node.name        = "jarvis_aec_source_2"', text)
        self.assertIn('node.name        = "jarvis_aec_source_3"', text)
        self.assertIn('node.name     = "jarvis_aec_capture_2"', text)
        self.assertIn("audio.rate     = 48000", text)
        self.assertIn("audio.channels = 1", text)
        self.assertIn("Microfone AEC (JARVIS: C925)", text)
        # One module per microphone, never a shared channel list.
        chains = mic_route.parse_filter_chains(text)
        self.assertEqual([c["target"] for c in chains],
                         [C920_NODE, self.SECOND, self.THIRD])
        self.assertEqual([c["rate"] for c in chains], [32000, 48000, 16000])
        self.assertEqual([c["channels"] for c in chains], [2, 1, 1])

    def test_single_microphone_output_is_unchanged(self):
        text = mic_route.render_managed_conf(C920_NODE, short="C920")
        self.assertEqual(text.count("libpipewire-module-echo-cancel"), 1)
        self.assertNotIn("jarvis_aec_source_2", text)
        self.assertIn('node.name        = "jarvis_aec_source"', text)
        self.assertIn('node.name     = "jarvis_aec_capture"', text)
        self.assertIn("Microfone AEC (JARVIS: C920)", text)

    def test_write_renders_all_modules_and_is_idempotent(self):
        result = mic_route.write_managed_conf(
            [C920_NODE, self.SECOND], directory=self.dir,
            rate=[32000, 48000], channels=[2, 1], short=["C920", "C925"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["targets"], [C920_NODE, self.SECOND])
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertEqual(text.count("libpipewire-module-echo-cancel"), 2)
        again = mic_route.write_managed_conf(
            [C920_NODE, self.SECOND], directory=self.dir,
            rate=[32000, 48000], channels=[2, 1], short=["C920", "C925"])
        self.assertFalse(again["changed"])

    def test_duplicate_hardware_collapses_to_one_module(self):
        text = mic_route.render_managed_conf(
            [C920_NODE, C920_NODE, self.SECOND])
        self.assertEqual(len(mic_route.parse_filter_chains(text)), 2)

    def test_any_monitor_in_the_list_is_refused(self):
        monitor = "alsa_output.usb-x.analog-stereo.monitor"
        for targets in ([""], [C920_NODE, monitor], []):
            with self.assertRaises(ValueError):
                mic_route.write_managed_conf(targets, directory=self.dir)
        self.assertFalse((self.dir / mic_route.MANAGED_CONF_NAME).exists())

    def test_managed_flag_covers_indexed_nodes_and_not_the_legacy_file(self):
        mic_route.write_managed_conf([C920_NODE, self.SECOND],
                                     directory=self.dir)
        (self.dir / mic_route.LEGACY_CONF_NAME).write_text(
            LEGACY_CONF, encoding="utf-8")
        chains = mic_route.read_filter_chains(self.dir)
        managed = [c for c in chains if c["managed"]]
        self.assertEqual(len(managed), 2)
        self.assertEqual({c["node_name"] for c in managed},
                         {"jarvis_aec_source", "jarvis_aec_source_2"})
        legacy = [c for c in chains if not c["managed"]]
        self.assertEqual([c["node_name"] for c in legacy], ["echo_cancel_source"])

    def test_node_name_predicates(self):
        self.assertTrue(mic_route.is_managed_node("jarvis_aec_source"))
        self.assertTrue(mic_route.is_managed_node("jarvis_aec_source_2"))
        self.assertFalse(mic_route.is_managed_node("jarvis_aec_source_x"))
        self.assertFalse(mic_route.is_managed_node("echo_cancel_source"))
        self.assertTrue(mic_route.is_aec_source("jarvis_aec_source_3"))
        self.assertTrue(mic_route.is_aec_source("echo_cancel_source"))

    def test_indexed_source_resolves_to_its_own_hardware(self):
        text = mic_route.render_managed_conf([C920_NODE, self.SECOND])
        chains = mic_route.parse_filter_chains(text)
        route = mic_route.physical_source(
            "jarvis_aec_source_2", sources=self._sources(), chains=chains,
            default=self.SECOND)
        self.assertEqual(route["source"], "jarvis_aec_source_2")
        self.assertEqual(route["physical"], self.SECOND)
        self.assertEqual(route["key"], mic_route.slug(self.SECOND))
        self.assertTrue(route["aec"])

    def _sources(self):
        return mic_route.parse_sources(
            "38\tjarvis_aec_source\tPipeWire\tfloat32le 2ch 32000Hz\tRUNNING\n"
            "39\tjarvis_aec_source_2\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING\n"
            f"64\t{C920_NODE}\tPipeWire\ts16le 2ch 32000Hz\tRUNNING\n"
            f"65\t{self.SECOND}\tPipeWire\ts16le 1ch 48000Hz\tSUSPENDED\n"
        )

    @contextlib.contextmanager
    def _no_pactl(self):
        """follow_managed queries the live server; these tests never do."""
        with mock.patch.object(mic_route, "list_sources", return_value=[]), \
                mock.patch.object(mic_route, "default_source", return_value=""):
            yield

    def test_follow_retargets_only_the_matching_module(self):
        mic_route.write_managed_conf([C920_NODE, self.SECOND],
                                     directory=self.dir, short=["C920", "C925"])
        with self._no_pactl():
            result = mic_route.follow_managed(
                mic_route.MANAGED_NODE, target=self.THIRD,
                directory=self.dir, restart=False)
        self.assertTrue(result["written"])
        self.assertEqual(result["targets"], [self.THIRD, self.SECOND])
        text = (self.dir / mic_route.MANAGED_CONF_NAME).read_text(encoding="utf-8")
        self.assertIn(self.THIRD, text)
        self.assertIn(self.SECOND, text)
        self.assertNotIn(C920_NODE, text)
        chains = mic_route.parse_filter_chains(text)
        self.assertEqual(chains[0]["node_name"], mic_route.MANAGED_NODE)
        self.assertEqual(chains[1]["node_name"], "jarvis_aec_source_2")

    def test_follow_retargets_the_second_module_too(self):
        mic_route.write_managed_conf([C920_NODE, self.SECOND],
                                     directory=self.dir)
        with self._no_pactl():
            result = mic_route.follow_managed(
                "jarvis_aec_source_2", target=self.THIRD,
                directory=self.dir, restart=False)
        self.assertTrue(result["written"])
        self.assertEqual(result["targets"], [C920_NODE, self.THIRD])

    def test_follow_refuses_a_monitor_and_an_aec_target(self):
        mic_route.write_managed_conf([C920_NODE, self.SECOND],
                                     directory=self.dir)
        for bad in ("alsa_output.x.monitor", "jarvis_aec_source",
                    "jarvis_aec_source_2"):
            with self._no_pactl():
                result = mic_route.follow_managed(
                    mic_route.MANAGED_NODE, target=bad, directory=self.dir,
                    restart=False)
            self.assertFalse(result["written"])
            self.assertIn("microfone físico", result["reason"])

    def test_follow_collapses_a_duplicated_target(self):
        mic_route.write_managed_conf([C920_NODE, self.SECOND],
                                     directory=self.dir)
        with self._no_pactl():
            result = mic_route.follow_managed(
                "jarvis_aec_source_2", target=C920_NODE,
                directory=self.dir, restart=False)
        # Both modules would capture the same hardware: one module survives.
        self.assertEqual(result["targets"], [C920_NODE])


if __name__ == "__main__":
    unittest.main()
