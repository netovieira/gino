"""Resolving several microphones at once, without touching PortAudio twice.

`sd` is replaced by a fake module: these tests must never open a real device or
enumerate a real host API. What they pin down is the contract the multi-
microphone path depends on — one enumeration for N names, no probe while a
stream is live, and a graceful None instead of an exception when there is
nothing cached.
"""
from __future__ import annotations

import sys
import time
import types
import unittest

import numpy as np

from core import audio_devices

DEVICES = [
    {"name": "Webcam C920", "hostapi": 0,
     "max_input_channels": 2, "max_output_channels": 0},
    {"name": "EDIFIER USB", "hostapi": 0,
     "max_input_channels": 1, "max_output_channels": 2},
    {"name": "Built-in Mic", "hostapi": 0,
     "max_input_channels": 1, "max_output_channels": 0},
]


class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        return None

    def stop(self):
        return None

    def close(self):
        return None

    def write(self, _data):
        time.sleep(0.02)          # a real sink is rate-limited


class _FakeInputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        callback = self.kwargs.get("callback")
        if callback is None:
            return
        block = np.zeros((1024, 1), dtype=np.int16)
        for _ in range(4):        # > rate*secs*0.3 at the patched probe time
            callback(block, 1024, None, None)

    def stop(self):
        return None

    def close(self):
        return None


class FakeSoundDevice(types.ModuleType):
    """Just enough of sounddevice for core.audio_devices, with counters."""

    def __init__(self, devices=None, apis=("PulseAudio",), refuse=()):
        super().__init__("sounddevice")
        self.devices = list(DEVICES if devices is None else devices)
        self.apis = list(apis)
        self.refuse = set(refuse)
        self.queries = 0
        self.opens: list[dict] = []

    def query_devices(self):
        self.queries += 1
        return [dict(device) for device in self.devices]

    def query_hostapis(self):
        return [{"name": name} for name in self.apis]

    def InputStream(self, **kwargs):
        self.opens.append(dict(kwargs))
        if kwargs.get("device") in self.refuse:
            raise RuntimeError("device busy")
        return _FakeInputStream(**kwargs)

    def RawOutputStream(self, **kwargs):
        self.opens.append(dict(kwargs))
        if kwargs.get("device") in self.refuse:
            raise RuntimeError("device busy")
        return _FakeStream(**kwargs)


class AudioDevicesTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeSoundDevice()
        sys.modules["sounddevice"] = self.fake
        self._saved = (audio_devices._cache, audio_devices._table_cache,
                       dict(audio_devices._chosen_api),
                       dict(audio_devices._probe_results),
                       audio_devices._live_streams)
        audio_devices._cache = None
        audio_devices._table_cache = None
        audio_devices._chosen_api = {"input": None, "output": None}
        audio_devices._probe_results.clear()
        audio_devices._live_streams = 0
        self._saved_probe = dict(audio_devices._PROBE_SECONDS)
        audio_devices._PROBE_SECONDS.update({"input": 0.01, "output": 0.01})

    def tearDown(self) -> None:
        (audio_devices._cache, audio_devices._table_cache,
         audio_devices._chosen_api, audio_devices._probe_results,
         audio_devices._live_streams) = self._saved
        audio_devices._PROBE_SECONDS.clear()
        audio_devices._PROBE_SECONDS.update(self._saved_probe)
        sys.modules.pop("sounddevice", None)


class ResolveManyTest(AudioDevicesTestBase):
    def test_two_names_share_one_enumeration(self):
        result = audio_devices.resolve_many(["Webcam C920", "Built-in Mic"], "input")
        self.assertEqual(result, [0, 2])
        self.assertEqual(self.fake.queries, 1,
                         "resolve_many must enumerate once, not once per name")
        probed = [open_["device"] for open_ in self.fake.opens][-2:]
        self.assertEqual(probed, [0, 2], "each name is probed exactly once")

    def test_a_warm_table_is_refreshed_once_for_the_batch(self):
        audio_devices.list_devices("input")          # prefetch already landed
        self.fake.queries = 0
        self.fake.devices.append(
            {"name": "Headset USB", "hostapi": 0,
             "max_input_channels": 1, "max_output_channels": 0})
        result = audio_devices.resolve_many(
            ["Headset USB", "Webcam C920"], "input")
        self.assertEqual(result, [3, 0],
                         "a microphone plugged after the prefetch resolves")
        self.assertEqual(self.fake.queries, 1,
                         "the batch refreshes the table once, not per name")

    def test_order_and_duplicates_are_preserved(self):
        result = audio_devices.resolve_many(
            ["Built-in Mic", "Webcam C920", "Built-in Mic"], "input")
        self.assertEqual(result, [2, 0, 2])
        self.assertEqual(self.fake.queries, 1)

    def test_unknown_and_default_names_become_none_in_place(self):
        result = audio_devices.resolve_many(
            ["Webcam C920", "Microfone Fantasma", ""], "input")
        self.assertEqual(result, [0, None, None])
        self.assertEqual(self.fake.queries, 1)

    def test_a_device_that_will_not_open_resolves_to_none(self):
        self.fake.refuse = {1}
        result = audio_devices.resolve_many(
            ["EDIFIER USB", "Webcam C920"], "input")
        self.assertEqual(result, [None, 0])

    def test_empty_batch_does_not_touch_sounddevice(self):
        self.assertEqual(audio_devices.resolve_many([]), [])
        self.assertEqual(self.fake.queries, 0)
        self.assertEqual(self.fake.opens, [])

    def test_probe_false_skips_the_open_and_close(self):
        audio_devices.list_devices("input")          # warm the shared table
        self.fake.opens.clear()
        result = audio_devices.resolve_many(
            ["Webcam C920", "Built-in Mic"], "input", probe=False)
        self.assertEqual(result, [0, 2])
        self.assertEqual(self.fake.opens, [])

    def test_resolve_single_name_still_works(self):
        self.assertEqual(audio_devices.resolve("Built-in Mic", "input"), 2)
        self.assertEqual(audio_devices.resolve("", "input"), None)
        self.assertEqual(
            audio_devices.resolve(audio_devices.DEFAULT_LABEL, "input"), None)


class LiveStreamTest(AudioDevicesTestBase):
    def test_no_probe_while_a_stream_is_live(self):
        audio_devices.list_devices("input")          # prefetch landed
        self.fake.opens.clear()
        self.fake.queries = 0
        with audio_devices.live_stream():
            self.assertTrue(audio_devices.streams_live())
            result = audio_devices.resolve_many(
                ["Webcam C920", "Built-in Mic"], "input")
        self.assertEqual(result, [0, 2])
        self.assertEqual(self.fake.opens, [],
                         "opening a probe stream next to a live one aborts")
        self.assertEqual(self.fake.queries, 0,
                         "a live stream must never trigger enumeration")
        self.assertFalse(audio_devices.streams_live())

    def test_forcing_probe_while_streaming_is_still_refused(self):
        audio_devices.list_devices("input")
        self.fake.opens.clear()
        with audio_devices.live_stream():
            result = audio_devices.resolve_many(
                ["Webcam C920"], "input", probe=True)
        self.assertEqual(result, [0])
        self.assertEqual(self.fake.opens, [],
                         "a live stream wins over an explicit probe=True")

    def test_refresh_is_ignored_while_streaming(self):
        audio_devices.list_devices("input")
        self.fake.queries = 0
        with audio_devices.live_stream():
            audio_devices.resolve_many(["Webcam C920"], "input", refresh=True)
        self.assertEqual(self.fake.queries, 0)

    def test_no_cached_table_degrades_to_the_system_default(self):
        self.assertIsNone(audio_devices._cached_table())
        with audio_devices.live_stream():
            result = audio_devices.resolve_many(
                ["Webcam C920", "Built-in Mic"], "input", refresh=True)
        self.assertEqual(result, [None, None])
        self.assertEqual(self.fake.queries, 0)
        self.assertEqual(self.fake.opens, [])

    def test_counter_survives_an_exception_and_nesting(self):
        with audio_devices.live_stream():
            with audio_devices.live_stream():
                self.assertTrue(audio_devices.streams_live())
            self.assertTrue(audio_devices.streams_live())
        self.assertFalse(audio_devices.streams_live())
        with self.assertRaises(RuntimeError):
            with audio_devices.live_stream():
                raise RuntimeError("boom")
        self.assertFalse(audio_devices.streams_live())


if __name__ == "__main__":
    unittest.main()
