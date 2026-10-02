"""Latch / hold_until plumbing for the discone auto-return (platform r2_return.sh)."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402


class LatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._dir, self._env = sa.R2_STATE_DIR, sa.MONITOR_ENV
        sa.R2_STATE_DIR = self.tmp.name
        sa.MONITOR_ENV = os.path.join(self.tmp.name, "monitor.env")
        self._popen, self._active = sa.subprocess.Popen, sa._unit_active
        self.calls = []
        sa.subprocess.Popen = lambda args, **kw: self.calls.append(args)
        self.active = "monitor.service"
        sa._unit_active = lambda u: u == self.active

    def tearDown(self):
        sa.R2_STATE_DIR, sa.MONITOR_ENV = self._dir, self._env
        sa.subprocess.Popen, sa._unit_active = self._popen, self._active
        self.tmp.cleanup()

    def latch(self):
        p = os.path.join(self.tmp.name, "r2-latch.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def state(self, mode, ago):
        with open(os.path.join(self.tmp.name, "r2-state.json"), "w") as f:
            json.dump({"mode": mode, "since": time.time() - ago}, f)

    def test_latched_switch_writes_latch(self):
        ok, _ = sa.r2_set_mode("vdl2", latch=True)
        self.assertTrue(ok)
        self.assertEqual(self.latch(), {"mode": "vdl2", "latched": True, "hold_until": 0.0})
        self.assertEqual(self.calls[-1][-1], "vdl2")

    def test_unlatched_switch_removes_latch(self):
        sa.r2_set_mode("vdl2", latch=True)
        sa.r2_set_mode("acars")
        self.assertIsNone(self.latch())

    def test_p25_clears_latch(self):
        sa.r2_set_mode("acars", latch=True)
        sa.r2_set_mode("p25", latch=True)
        self.assertIsNone(self.latch())

    def test_atc_hold_until_via_monitor_tune(self):
        ok, _ = sa.monitor_tune(125_525_000, "am", 0, latch=False, hold_until=1234.0)
        self.assertTrue(ok)
        self.assertEqual(self.latch(), {"mode": "atc", "latched": False, "hold_until": 1234.0})

    def test_state_reports_return_countdown(self):
        self.state("atc", 600)
        st = sa.r2_state()
        self.assertEqual((st["mode"], st["latched"]), ("atc", False))
        self.assertAlmostEqual(st["returns_at"], time.time() + sa.R2_RETURN_AFTER_SEC - 600, delta=5)

    def test_state_latched_has_no_countdown(self):
        self.state("atc", 600)
        sa.r2_set_mode("atc", 125_525_000, latch=True)
        st = sa.r2_state()
        self.assertTrue(st["latched"])
        self.assertIsNone(st["returns_at"])

    def test_stale_latch_for_other_mode_ignored(self):
        with open(os.path.join(self.tmp.name, "r2-latch.json"), "w") as f:
            json.dump({"mode": "vdl2", "latched": True, "hold_until": 0}, f)
        self.state("atc", 60)
        self.assertFalse(sa.r2_state()["latched"])

    def test_hold_extends_countdown(self):
        self.state("atc", 600)
        hold = time.time() + 7200
        with open(os.path.join(self.tmp.name, "r2-latch.json"), "w") as f:
            json.dump({"mode": "atc", "latched": False, "hold_until": hold}, f)
        self.assertAlmostEqual(sa.r2_state()["returns_at"], hold, delta=1)

    def test_p25_and_idle_have_no_countdown(self):
        self.active = "op25-ems.service"
        self.assertIsNone(sa.r2_state()["returns_at"])
        self.active = ""
        self.assertEqual(sa.r2_state()["mode"], "idle")
        self.assertIsNone(sa.r2_state()["returns_at"])


if __name__ == "__main__":
    unittest.main()
