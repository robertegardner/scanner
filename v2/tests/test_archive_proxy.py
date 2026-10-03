"""scanner-api reverse-proxies /archive + /api/archive/* to p25-archive, with the
off-LAN write guard applied BEFORE anything is forwarded."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scanner_api as sa  # noqa: E402

SEEN = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        SEEN.append((self.command, self.path, self.headers.get("Range"), body))
        if self.path.startswith("/api/archive/clip/"):
            self.send_response(206)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Range", "bytes 0-3/10")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"abcd")
            return
        if self.path.startswith("/api/archive/talkgroups.csv"):
            data = b"tgid,label\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="x.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(201 if self.command == "POST" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _reply


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls._url = sa.ARCHIVE_URL
        sa.ARCHIVE_URL = f"http://127.0.0.1:{cls.up.server_address[1]}"
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), sa.Handler)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.api.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        sa.ARCHIVE_URL = cls._url
        cls.api.shutdown(); cls.up.shutdown()

    def setUp(self):
        SEEN.clear()

    def req(self, method, path, headers=None, body=None):
        r = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_get_with_range_passthrough(self):
        code, h, body = self.req("GET", "/api/archive/clip/7", {"Range": "bytes=0-3"})
        self.assertEqual((code, body, h["Content-Range"], h["Accept-Ranges"]),
                         (206, b"abcd", "bytes 0-3/10", "bytes"))
        self.assertEqual(SEEN[0][:3], ("GET", "/api/archive/clip/7", "bytes=0-3"))

    def test_page_and_query_string(self):
        code, _, body = self.req("GET", "/archive?t=1&span=3600")
        self.assertEqual(code, 200)
        self.assertEqual(SEEN[0][1], "/archive?t=1&span=3600")

    def test_offlan_non_admin_post_blocked_before_forwarding(self):
        code, _, _ = self.req("POST", "/api/archive/incidents",
                              {"X-Real-IP": "203.0.113.9", "X-authentik-groups": "family",
                               "Content-Type": "application/json"}, {"name": "x"})
        self.assertEqual(code, 403)
        self.assertEqual(SEEN, [])

    def test_offlan_non_admin_delete_blocked(self):
        code, _, _ = self.req("DELETE", "/api/archive/incidents/1",
                              {"X-Real-IP": "203.0.113.9", "X-authentik-groups": "family"})
        self.assertEqual(code, 403)
        self.assertEqual(SEEN, [])

    def test_lan_post_and_delete_forwarded_with_body(self):
        code, _, _ = self.req("POST", "/api/archive/incidents",
                              {"Content-Type": "application/json"}, {"name": "Crash"})
        self.assertEqual(code, 201)
        self.assertEqual(json.loads(SEEN[0][3]), {"name": "Crash"})
        code, _, _ = self.req("DELETE", "/api/archive/incidents/1",
                              {"X-Real-IP": "203.0.113.9", "X-authentik-groups": "homelab-admin"})
        self.assertEqual((code, SEEN[1][0]), (200, "DELETE"))

    def test_upstream_down_is_502(self):
        saved = sa.ARCHIVE_URL
        sa.ARCHIVE_URL = "http://127.0.0.1:1"
        try:
            self.assertEqual(self.req("GET", "/api/archive/calls")[0], 502)
        finally:
            sa.ARCHIVE_URL = saved

    def test_content_disposition_forwarded(self):
        code, h, body = self.req("GET", "/api/archive/talkgroups.csv")
        self.assertEqual(code, 200)
        self.assertEqual(h.get("Content-Disposition"), 'attachment; filename="x.csv"')

    def test_non_archive_paths_untouched(self):
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)
        self.assertEqual(SEEN, [])


if __name__ == "__main__":
    unittest.main()
