#!/usr/bin/env python3
"""scanner-api — V1-contract bridge for the scanner domain on scanner-compute.

Serves the Android app's (and any other V1 client's) scanner REST contract,
fed by op25's http terminal. Stdlib only — no Flask, no requests.

  GET  /api/status            {"current": {...}|null, "sdr_owner", "upcoming_passes": []}
  GET  /api/calls?limit=N     bare list of call EVENTS (no recordings yet — all
                              file fields null; the app renders rows, taps no-op)
  POST /api/source/moswin     no-op success (op25 IS permanently MOSWIN)
  POST /api/monitor/tune      400 — aviation monitor returns with the Airspy R2
  GET  /api/monitor/squelch   static {"enabled": false, "active_on_monitor": false}
  POST /api/monitor/squelch   503 — no monitor pipeline on scanner v2
  GET  /recordings/ems/...    404 — never referenced (paths are null)

State comes from a 1s poller thread speaking op25's uuid-scoped terminal
protocol (POST update -> trunk_update / change_freq / rx_update messages).
The op25 stderr log is NOT parsed (ANSI spam, no tags, no rotation).

Config via environment (systemd EnvironmentFile=/etc/scanner-compute/scanner-api.env):
  OP25_TERMINAL_URL  default http://127.0.0.1:8080
  API_PORT           default 8081
  TGID_TAGS          default /opt/scanner-compute/moswin-tgid-tags.tsv
  EVENTS_PATH        default /var/lib/scanner-compute/call-events.jsonl
"""
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

OP25_URL    = os.environ.get("OP25_TERMINAL_URL", "http://127.0.0.1:8080")
API_PORT    = int(os.environ.get("API_PORT", "8081"))
TGID_TAGS   = os.environ.get("TGID_TAGS", "/opt/scanner-compute/moswin-tgid-tags.tsv")
EVENTS_PATH = os.environ.get("EVENTS_PATH", "/var/lib/scanner-compute/call-events.jsonl")

POLL_S          = 1.0    # op25 terminal poll cadence
STALE_S         = 30.0   # no trunk_update for this long -> report current: null
CALL_CLOSE_S    = 5.0    # tgid silent for this long -> close the call event
EVENTS_MAX      = 500    # ring buffer size
COMPACT_BYTES   = 1_000_000

_CLIENT_UUID = str(uuid.uuid4())


def log(msg: str) -> None:
    print(msg, flush=True)


def load_tags(path: str) -> dict:
    """tgid -> label from the platform-provisioned TSV (tgid<TAB>label...)."""
    tags = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    tags[parts[0].strip()] = parts[1].strip()
    except OSError as e:
        log(f"tags: could not read {path}: {e}")
    return tags


class State:
    """Shared between the poller thread and HTTP handlers."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_trunk = 0.0          # monotonic ts of last trunk_update seen
        self.cur_tgid = None           # str | None — from change_freq
        self.cur_tag = None
        self.cur_srcaddr = None        # str | None — from trunk_update srcaddr
        self.open_call = None          # dict | None
        self.events = deque(maxlen=EVENTS_MAX)
        self.tags = load_tags(TGID_TAGS)

    # -- event persistence ----------------------------------------------------

    def load_events(self):
        try:
            with open(EVENTS_PATH) as f:
                lines = f.readlines()[-EVENTS_MAX:]
            for line in lines:
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass
            log(f"events: loaded {len(self.events)} from {EVENTS_PATH}")
        except OSError:
            pass

    def persist_event(self, ev: dict):
        try:
            with open(EVENTS_PATH, "a") as f:
                f.write(json.dumps(ev) + "\n")
            if os.path.getsize(EVENTS_PATH) > COMPACT_BYTES:
                with open(EVENTS_PATH, "w") as f:
                    for e in self.events:
                        f.write(json.dumps(e) + "\n")
        except OSError as e:
            log(f"events: persist failed: {e}")

    # -- call-event tracking (caller holds the lock) ---------------------------

    def label_for(self, tgid):
        return self.cur_tag or self.tags.get(str(tgid)) or f"TG {tgid}"

    def open_event(self, tgid):
        self.open_call = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "talkgroup": self.label_for(tgid),
            "tgid": str(tgid),
            "radio": str(self.cur_srcaddr) if self.cur_srcaddr else None,
            "filename": None, "path": None, "size_kb": None, "transcript": None,
            "_last_seen": time.monotonic(),
        }

    def close_event(self):
        if not self.open_call:
            return
        ev = {k: v for k, v in self.open_call.items() if not k.startswith("_")}
        self.open_call = None
        self.events.append(ev)
        self.persist_event(ev)
        log(f"call: {ev['ts']} {ev['talkgroup']} (tg {ev['tgid']})")

    def on_tgid(self, tgid):
        """Called every poll with the current tgid (may be None)."""
        now = time.monotonic()
        if tgid is not None:
            tgid = str(tgid)
            if self.open_call and self.open_call["tgid"] == tgid:
                self.open_call["_last_seen"] = now
                if self.cur_srcaddr and not self.open_call["radio"]:
                    self.open_call["radio"] = str(self.cur_srcaddr)
            else:
                self.close_event()
                self.open_event(tgid)
        elif self.open_call and now - self.open_call["_last_seen"] > CALL_CLOSE_S:
            self.close_event()


STATE = State()


# -- op25 terminal poller ------------------------------------------------------

def poll_op25_once():
    body = json.dumps([{"command": "update", "arg1": 0, "arg2": 0,
                        "uuid": _CLIENT_UUID}]).encode()
    req = urllib.request.Request(OP25_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        msgs = json.loads(resp.read().decode() or "[]")
    if not isinstance(msgs, list):
        return
    with STATE.lock:
        for m in msgs:
            if not isinstance(m, dict):
                continue
            jt = m.get("json_type")
            if jt == "trunk_update":
                STATE.last_trunk = time.monotonic()
                src = m.get("srcaddr")
                if src:  # nonzero radio id of the active call
                    STATE.cur_srcaddr = src
            elif jt == "change_freq":
                # event message: arrives on retune; tgid None = back on control
                STATE.cur_tgid = m.get("tgid")
                STATE.cur_tag = m.get("tag") or None
                if STATE.cur_tgid is None:
                    STATE.cur_srcaddr = None
        STATE.on_tgid(STATE.cur_tgid)


def poller():
    while True:
        try:
            poll_op25_once()
        except Exception as e:  # op25 down/restarting — staleness handles it
            with STATE.lock:
                STATE.on_tgid(None)
            log(f"poll: {e.__class__.__name__}: {e}")
        time.sleep(POLL_S)


# -- HTTP layer -----------------------------------------------------------------

def status_payload() -> dict:
    with STATE.lock:
        fresh = (time.monotonic() - STATE.last_trunk) < STALE_S and STATE.last_trunk > 0
        if not fresh:
            return {"current": None, "sdr_owner": "op25", "upcoming_passes": []}
        if STATE.open_call:
            detail = f"active: {STATE.open_call['talkgroup']}"
        else:
            detail = "monitoring control channel"
        return {"current": {"name": "ems_scanner", "detail": detail},
                "sdr_owner": "op25", "upcoming_passes": []}


def calls_payload(limit: int) -> list:
    with STATE.lock:
        out = list(STATE.events)
        if STATE.open_call:  # live call shows at the top too
            out.append({k: v for k, v in STATE.open_call.items()
                        if not k.startswith("_")})
    return list(reversed(out))[:max(1, limit)]


class Handler(BaseHTTPRequestHandler):
    server_version = "scanner-api/1.0"

    def _send(self, code: int, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # journal noise control
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/status":
            self._send(200, status_payload())
        elif url.path == "/api/calls":
            q = parse_qs(url.query)
            limit = int(q.get("limit", ["15"])[0] or 15)
            self._send(200, calls_payload(limit))
        elif url.path == "/api/monitor/squelch":
            self._send(200, {"enabled": False, "active_on_monitor": False})
        elif url.path.startswith("/recordings/"):
            self._send(404, {"error": "no recordings on scanner v2 yet"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        # drain any body so keep-alive stays sane
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if url.path == "/api/source/moswin":
            self._send(200, {"status": "already moswin", "source": "moswin"})
        elif url.path == "/api/monitor/tune":
            self._send(400, {"error": "aviation monitor not available on "
                                      "scanner v2 (returns with the Airspy R2)"})
        elif url.path == "/api/monitor/squelch":
            self._send(503, {"error": "no monitor pipeline on scanner v2"})
        else:
            self._send(404, {"error": "not found"})


def main():
    STATE.load_events()
    threading.Thread(target=poller, daemon=True, name="op25-poller").start()
    srv = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    log(f"scanner-api listening on :{API_PORT} (op25 terminal: {OP25_URL})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
