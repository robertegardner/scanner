"""scanner-api access-control guard (platform spec 2026-10-01-radio-authentik-access)."""
import os
import sys
import unittest
from email.message import Message

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402


def _hdrs(**kw):
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class AuthzTest(unittest.TestCase):
    def test_direct_lan_call_without_real_ip_is_trusted(self):
        # the .84 /api/scanner proxy and LAN scripts hit :8081 directly
        self.assertFalse(sa.is_forbidden_write("POST", _hdrs()))

    def test_offlan_family_post_forbidden(self):
        h = _hdrs(X_Real_IP="203.0.113.9", X_authentik_username="kid",
                  X_authentik_groups="family|users")
        self.assertTrue(sa.is_forbidden_write("POST", h))

    def test_offlan_admin_post_allowed(self):
        h = _hdrs(X_Real_IP="203.0.113.9", X_authentik_groups="family|homelab-admin")
        self.assertFalse(sa.is_forbidden_write("POST", h))

    def test_lan_ip_with_family_session_is_admin(self):
        h = _hdrs(X_Real_IP="192.168.6.84", X_authentik_groups="family")
        self.assertFalse(sa.is_forbidden_write("POST", h))

    def test_garbage_real_ip_is_untrusted(self):
        self.assertTrue(sa.is_forbidden_write("POST", _hdrs(X_Real_IP="unix:")))

    def test_ipv6_real_ip_parses(self):
        self.assertFalse(sa.auth_context(_hdrs(X_Real_IP="2001:db8::1"))["trusted"])

    def test_get_never_forbidden(self):
        self.assertFalse(sa.is_forbidden_write("GET", _hdrs(X_Real_IP="203.0.113.9")))


if __name__ == "__main__":
    unittest.main()
