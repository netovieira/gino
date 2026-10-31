"""Fallbacks when the bridge or sidecar is unhealthy."""
from __future__ import annotations

import unittest

import main
from core import health


class FakeLive:
    def is_ready(self):
        return True


class FakeBridge:
    def __init__(self, running):
        self._running = running
    def is_running(self):
        return self._running


class ResilienceTest(unittest.TestCase):
    def test_bridge_down_forces_local_fallback(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_live = FakeLive()
        obj._bridge = FakeBridge(False)
        self.assertFalse(main.JarvisLive._qoder_live_ready(obj))
        self.assertFalse(health.snapshot()["bridge"]["ok"])

    def test_ready_only_when_both_are_up(self):
        obj = object.__new__(main.JarvisLive)
        obj._qoder_live = FakeLive()
        obj._bridge = FakeBridge(True)
        self.assertTrue(main.JarvisLive._qoder_live_ready(obj))


if __name__ == "__main__":
    unittest.main()
