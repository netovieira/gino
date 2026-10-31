"""Metrics, health and the runtime_status voice tool."""
from __future__ import annotations

import unittest

from core import health, metrics
from actions import runtime_status


class RuntimeStatusTest(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        health.set_component("qoder_live", True, "ready")
        health.set_component("bridge", True, "127.0.0.1:8011")

    def test_metrics_and_health_are_reported(self):
        metrics.inc("qoder.turns")
        metrics.inc("qoder.done")
        metrics.observe("qoder.ttft_s", 2.4)
        metrics.observe("qoder.ttft_s", 3.0)
        snap = metrics.snapshot()
        self.assertEqual(snap["counters"]["qoder.turns"], 1)
        self.assertEqual(snap["observations"]["qoder.ttft_s"]["count"], 2)
        self.assertAlmostEqual(snap["observations"]["qoder.ttft_s"]["avg"], 2.7)
        text = runtime_status.runtime_status({})
        self.assertIn("qoder_live: ok", text)
        self.assertIn("bridge: ok", text)
        self.assertIn("qoder.turns=1", text)

    def test_tool_metadata_is_valid(self):
        tool = runtime_status.TOOL
        self.assertEqual(tool["name"], "runtime_status")
        self.assertEqual(tool["parameters"]["type"], "OBJECT")
        self.assertTrue(callable(tool["handler"]))


if __name__ == "__main__":
    unittest.main()
