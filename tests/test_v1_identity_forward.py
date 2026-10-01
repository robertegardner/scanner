"""p25.rg2.io V1 UI (read-only mode) must forward the caller's identity to the
.83 scanner-api so its off-LAN write guard can decide (platform spec
2026-10-01-radio-authentik-access; final-review finding #1)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SCANNER_UI_READONLY", "true")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "files" / "opt" / "scanner"))
import app as v1  # noqa: E402


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"ok": True}


class IdentityForwardTest(unittest.TestCase):
    def setUp(self):
        self.client = v1.app.test_client()

    def _post_tune(self, headers):
        with mock.patch.object(v1.requests, "request", return_value=_Resp()) as req:
            self.client.post("/api/monitor/tune", json={"freq": 125.525}, headers=headers)
        return req.call_args.kwargs.get("headers") or {}

    def test_offlan_identity_forwarded(self):
        sent = self._post_tune({"X-Real-IP": "203.0.113.9",
                                "X-authentik-username": "kid",
                                "X-authentik-groups": "family"})
        self.assertEqual(sent.get("X-Real-IP"), "203.0.113.9")
        self.assertEqual(sent.get("X-authentik-username"), "kid")
        self.assertEqual(sent.get("X-authentik-groups"), "family")

    def test_direct_lan_call_forwards_nothing(self):
        self.assertEqual(self._post_tune({}), {})

    def test_unrelated_headers_not_forwarded(self):
        sent = self._post_tune({"X-Real-IP": "203.0.113.9", "Cookie": "a=b",
                                "Authorization": "Basic eA=="})
        self.assertEqual(set(sent), {"X-Real-IP"})


if __name__ == "__main__":
    unittest.main()
