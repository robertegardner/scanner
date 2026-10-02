"""/api/status exposes the open call's tgid/talkgroup/radio for p25-recorder."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402


class StatusCallTest(unittest.TestCase):
    def tearDown(self):
        sa.STATE.open_call = None
        sa.STATE.cur_tgid = None
        sa.STATE.last_trunk = 0

    def test_open_call_exposed(self):
        sa.STATE.last_trunk = time.monotonic()
        sa.STATE.cur_tgid = 4229                     # op25 on the voice channel
        sa.STATE.open_call = {"tgid": "4229", "talkgroup": "Jackson PD (Police)", "radio": "87198",
                              "ts": "x", "_private": 1}
        self.assertEqual(sa.status_payload()["call"],
                         {"tgid": "4229", "talkgroup": "Jackson PD (Police)", "radio": "87198"})

    def test_no_call_is_null(self):
        sa.STATE.last_trunk = time.monotonic()
        sa.STATE.open_call = None
        self.assertIsNone(sa.status_payload()["call"])

    def test_lingering_call_after_return_to_control_is_null(self):
        """open_call is kept ~5 s after op25 drops back to control (cur_tgid None);
        it must not be exposed or p25-recorder tags the next call with it."""
        sa.STATE.last_trunk = time.monotonic()
        sa.STATE.cur_tgid = None
        sa.STATE.open_call = {"tgid": "4229", "talkgroup": "Jackson PD (Police)", "radio": "87198"}
        self.assertIsNone(sa.status_payload()["call"])
        self.assertIn("active", sa.status_payload()["current"]["detail"])

    def test_stale_op25_is_null(self):
        sa.STATE.last_trunk = 0
        self.assertIsNone(sa.status_payload()["call"])


if __name__ == "__main__":
    unittest.main()
