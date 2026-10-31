"""Power manager tests (dry-run; no real shutdown/suspend/lock)."""
from __future__ import annotations

import unittest

from core.power import PowerManager


class PowerManagerTest(unittest.TestCase):
    def _mgr(self):
        logs = []
        return PowerManager(dry_run=True, logger=logs.append,
                            platform_name="Linux"), logs

    def test_shutdown_requires_the_exact_phrase(self):
        mgr, _ = self._mgr()
        out = mgr.request("shutdown")
        self.assertIn("CONFIRMATION_PENDING", out)
        self.assertTrue(mgr.pending())
        wrong = mgr.confirm("pode desligar")
        self.assertIn("Não confirmei", wrong)
        self.assertTrue(mgr.pending())  # kept for another attempt
        ok = mgr.confirm("confirmo desligar")
        self.assertIn("Desligando", ok)
        self.assertIsNone(mgr.pending())

    def test_suspend_and_unlock_phrases(self):
        mgr, _ = self._mgr()
        mgr.request("suspend")
        self.assertIn("Suspendendo", mgr.confirm("confirmo suspender"))
        mgr.request("unlock")
        self.assertIn("desbloqueada", mgr.confirm("confirmo desbloquear"))

    def test_cancel_and_lock(self):
        mgr, _ = self._mgr()
        mgr.request("shutdown")
        self.assertIn("cancelado", mgr.cancel())
        self.assertIsNone(mgr.pending())
        self.assertIn("bloqueada", mgr.lock())

    def test_rate_limit(self):
        mgr, _ = self._mgr()
        mgr.request("shutdown")
        mgr.confirm("confirmo desligar")
        again = mgr.request("shutdown")
        self.assertIn("limitada", again)
        self.assertIsNone(mgr.pending())

    def test_runner_failure_is_reported(self):
        class R:
            returncode = 1
            stderr = "polkit denied"
            stdout = ""
        mgr = PowerManager(dry_run=False, platform_name="Linux",
                           runner=lambda _cmd: R())
        mgr.request("shutdown")
        out = mgr.confirm("confirmo desligar")
        self.assertIn("Não consegui", out)
        self.assertIn("polkit denied", out)

    def test_unlock_uses_the_delegated_callback(self):
        mgr = PowerManager(dry_run=False, platform_name="Linux",
                           unlock_fn=lambda: "Tela desbloqueada pelo cofre.")
        mgr.request("unlock")
        out = mgr.confirm("confirmo desbloquear")
        self.assertIn("cofre", out)


if __name__ == "__main__":
    unittest.main()
