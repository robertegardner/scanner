#!/usr/bin/env python3
"""scanner-api — V1-contract bridge for the scanner domain on scanner-compute.

Serves the Android app's (and any other V1 client's) scanner REST contract,
fed by op25's http terminal. Stdlib only — no Flask, no requests.

  GET  /api/status            {"current": {...}|null, "sdr_owner", "upcoming_passes": []}
  GET  /api/calls?limit=N     bare list of call EVENTS (no recordings yet — all
                              file fields null; the app renders rows, taps no-op)
  GET  /api/transcribe        latest live caption {text,source,context,updated}
                              from scanner-transcribe's tmpfs state file
  GET  /api/transcript?date=&limit=N   {date, days[], entries[]} from the EMS
                              transcript JSONL log (monitor-mode captions)
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
import glob
import json
import os
import re
import subprocess
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
# EMS transcription output (written by scanner-transcribe on this host). Defaults
# match its transcribe.env so the bridge surfaces captions with no extra config.
TRANSCRIPTS_DIR       = os.environ.get("TRANSCRIPTS_DIR", "/var/lib/scanner-compute/transcripts")
TRANSCRIBE_STATE_PATH = os.environ.get("TRANSCRIBE_STATE_PATH", "/run/scanner/transcribe.json")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

POLL_S          = 1.0    # op25 terminal poll cadence
STALE_S         = 30.0   # no trunk_update for this long -> report current: null
CALL_CLOSE_S    = 5.0    # tgid silent for this long -> close the call event
EVENTS_MAX      = 500    # ring buffer size
COMPACT_BYTES   = 1_000_000

_CLIENT_UUID = str(uuid.uuid4())

# ---- ATC airband (on-demand; preempts P25) -------------------------------
# A click starts atc-listen@<freq>.service on this host, which stops op25 + the
# rtl_tcp bridge to free the Airspy R2, AM-demods the airband channel to the rack
# Icecast /scanner-atc.mp3, and auto-returns to P25 after 10 min (RuntimeMaxSec)
# or on stop. systemctl start/stop is sudo-granted to this user (scanner-atc
# sudoers drop-in).
ATC_PRESETS = [
    {"label": "AWOS 120.55", "freq": 120550000},
    {"label": "Tower 119.0", "freq": 119000000},
    {"label": "Approach 133.65", "freq": 133650000},
    {"label": "Ground 121.6", "freq": 121600000},
]
ATC_MOUNT = "https://icecast.rg2.io/scanner-atc.mp3"
ATC_MIN_HZ, ATC_MAX_HZ = 118_000_000, 137_000_000   # airband voice


def atc_active_freq():
    """The freq (Hz) of the running atc-listen@ instance, or None."""
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain", "atc-listen@*"],
            capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"atc-listen@(\d+)\.service", out)
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def atc_set(freq):
    """Start ATC on freq (preempts P25); freq=None stops it. -> (ok, message)."""
    cur = atc_active_freq()
    if freq is None:
        if cur is not None:
            subprocess.run(["sudo", "systemctl", "stop", f"atc-listen@{cur}"], timeout=25)
        return True, "stopped"
    if not (ATC_MIN_HZ <= freq <= ATC_MAX_HZ):
        return False, "frequency out of airband (118-137 MHz)"
    if cur is not None and cur != freq:
        subprocess.run(["sudo", "systemctl", "stop", f"atc-listen@{cur}"], timeout=25)
    r = subprocess.run(["sudo", "systemctl", "start", f"atc-listen@{freq}"],
                       capture_output=True, text=True, timeout=25)
    if r.returncode != 0:
        return False, (r.stderr or "start failed").strip()
    return True, "started"

# Minimal human UI served at "/" (ems.rg2.io): live EMS caption + recent
# transcript log, polling the same-origin /api/transcribe + /api/transcript
# endpoints. Stdlib-only, no templates — a plain literal (braces are CSS/JS, so
# this must NOT be an f-string/.format target).
CAPTIONS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EMS Captions - MOSWIN P25</title>
<style>
:root{--bg:#0d0e10;--panel:#16181c;--line:#2a2e35;--text:#e6e8eb;--dim:#8b929c;--accent:#6db0f0;--green:#6df09b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif}
header{padding:.7rem 1rem;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
h1{font-size:1rem;margin:0;font-weight:600}
.sub{color:var(--dim);font-size:.8rem}
audio{height:34px}
main{max-width:780px;margin:0 auto;padding:1rem}
.live{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1rem 1.1rem;margin-bottom:1rem}
.live .ctx{color:var(--accent);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.live .txt{font-size:1.5rem;line-height:1.35;margin-top:.35rem;min-height:1.4em}
.live .age{color:var(--dim);font-size:.75rem;margin-top:.45rem}
.live.stale .txt{color:var(--dim)}
.log{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.log h2{font-size:.74rem;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin:0;padding:.55rem 1rem;border-bottom:1px solid var(--line)}
.row{padding:.5rem 1rem;border-bottom:1px solid var(--line);display:flex;gap:.75rem}
.row:last-child{border-bottom:0}
.row time{color:var(--dim);font-variant-numeric:tabular-nums;font-size:.8rem;white-space:nowrap;padding-top:.12rem}
.empty{color:var(--dim);padding:1rem;text-align:center}
.dot{width:9px;height:9px;border-radius:50%;background:var(--dim);display:inline-block;transition:background .3s}
.dot.on{background:var(--green)}
.atc{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:.9rem 1.1rem;margin-bottom:1rem}
.atch{display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.6rem;flex-wrap:wrap}
.atch b{font-weight:600}.atch .st{font-size:.8rem;color:var(--dim)}.atch .st.on{color:var(--green)}
.btns{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.55rem}
.btns button,.man button{background:#23262c;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:.42rem .7rem;font:inherit;font-size:.85rem;cursor:pointer}
.btns button.act{background:var(--accent);color:#06121f;border-color:var(--accent);font-weight:600}
.man{display:flex;gap:.4rem}
.man input{flex:1;min-width:0;background:#0d0e10;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:.42rem .6rem;font:inherit}
.man button.stop{background:#3a2326;border-color:#5a2e34}
</style></head><body>
<header>
<span class="dot" id="dot"></span>
<h1>EMS Captions</h1><span class="sub">MOSWIN P25 &middot; live transcription</span>
<span style="flex:1"></span>
<audio controls preload="none" src="https://icecast.rg2.io/ems.mp3"></audio>
</header>
<main>
<div class="atc" id="atc">
<div class="atch"><span><b>ATC</b> <span class="sub">airband &middot; preempts P25</span></span><span class="st" id="atcst">idle</span></div>
<div class="btns" id="atcbtns"></div>
<div class="man"><input id="atcf" placeholder="tune MHz, e.g. 124.2" inputmode="decimal"><button id="atcgo">Listen</button><button id="atcstop" class="stop">Stop</button></div>
<audio id="atcaudio" controls preload="none" style="display:none;width:100%;margin-top:.6rem"></audio>
</div>
<div class="live" id="live"><div class="ctx" id="ctx">MOSWIN P25</div>
<div class="txt" id="txt">&hellip;</div><div class="age" id="age"></div></div>
<div class="log"><h2>Recent</h2><div id="rows"><div class="empty">Loading&hellip;</div></div></div>
</main>
<script>
var $=function(i){return document.getElementById(i)};
function ago(s){if(!s)return'';var d=Math.max(0,Date.now()/1000-s);
 if(d<60)return Math.round(d)+'s ago';if(d<3600)return Math.round(d/60)+'m ago';return Math.round(d/3600)+'h ago';}
function esc(s){return String(s).replace(/[&<>]/g,function(m){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[m]});}
function poll(){fetch('/api/transcribe',{cache:'no-store'}).then(function(r){return r.json()}).then(function(c){
 $('txt').textContent=c.text||'(silence)';$('ctx').textContent=c.context||'MOSWIN P25';
 var fresh=c.updated&&(Date.now()/1000-c.updated)<90;
 $('live').classList.toggle('stale',!fresh);$('dot').classList.toggle('on',!!fresh);
 $('age').textContent=c.updated?('updated '+ago(c.updated)):'';
}).catch(function(){$('dot').classList.remove('on')});}
function loadLog(){fetch('/api/transcript?limit=60',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
 var rows=(d.entries||[]).filter(function(e){return e.text});
 $('rows').innerHTML=rows.length?rows.map(function(e){var t=(e.ts||'').slice(11,19);
  return '<div class="row"><time>'+t+'</time><span>'+esc(e.text)+'</span></div>';}).join(''):
  '<div class="empty">No captions yet today.</div>';
}).catch(function(){});}
var atcPresets=[];
function fmtMHz(hz){return (hz/1e6).toFixed(3).replace(/0+$/,'').replace(/[.]$/,'')}
function atcRender(active){
 $('atcbtns').innerHTML=atcPresets.map(function(p){
  return '<button data-f="'+p.freq+'"'+(active===p.freq?' class="act"':'')+'>'+esc(p.label)+'</button>';}).join('');
 Array.prototype.forEach.call($('atcbtns').children,function(b){
  b.onclick=function(){atcStart(parseInt(b.getAttribute('data-f'),10))};});
 var a=$('atcaudio');
 if(active){$('atcst').textContent='listening '+fmtMHz(active)+' MHz — P25 preempted';$('atcst').classList.add('on');
  if(a.style.display==='none'){a.src='https://icecast.rg2.io/scanner-atc.mp3?t='+Date.now();a.style.display='block';}}
 else{$('atcst').textContent='idle';$('atcst').classList.remove('on');a.style.display='none';a.removeAttribute('src');}
}
function atcStart(freq){fetch('/api/atc/start',{method:'POST',body:JSON.stringify({freq:freq})})
 .then(function(r){return r.json()}).then(function(d){if(d.msg&&d.active===null)$('atcst').textContent=d.msg;atcRender(d.active);});}
function atcStop(){fetch('/api/atc/stop',{method:'POST'}).then(function(r){return r.json()}).then(function(d){atcRender(d.active);});}
function atcPoll(){fetch('/api/atc',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
 atcPresets=d.presets||[];atcRender(d.active);}).catch(function(){});}
$('atcgo').onclick=function(){var v=parseFloat($('atcf').value);if(v){atcStart(Math.round(v<1000?v*1e6:v));}};
$('atcstop').onclick=atcStop;
poll();loadLog();atcPoll();setInterval(poll,3000);setInterval(loadLog,12000);setInterval(atcPoll,5000);
</script></body></html>
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def _int(val, default: int) -> int:
    """Parse a query-string int, falling back to default on junk (no 500s)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


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


# -- EMS transcripts (produced by scanner-transcribe on this host) -------------
# op25 has no per-call recordings, so transcription is monitor-mode (rolling
# captions of the live /ems.mp3). That output is stream-level, not call-keyed —
# hence it surfaces here, not in the per-call `transcript` field (which stays
# null). Shapes match the V1 scanner-ui contract exactly so existing clients work.

def live_caption() -> dict:
    """Latest live caption written to tmpfs by scanner-transcribe."""
    try:
        with open(TRANSCRIBE_STATE_PATH, encoding="utf-8") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return {"text": "", "source": "", "context": "", "updated": 0}


def transcript_days() -> list:
    """Dates (newest first) that have a transcript log."""
    try:
        days = [os.path.basename(p)[:-6]  # strip ".jsonl"
                for p in glob.glob(os.path.join(TRANSCRIPTS_DIR, "*.jsonl"))]
    except OSError:
        days = []
    return sorted((d for d in days if _DATE_RE.match(d)), reverse=True)


def read_transcript_day(date: str, limit: int = 1000) -> list:
    """Parse one day's JSONL transcript log, newest first."""
    if not _DATE_RE.match(date or ""):
        return []
    path = os.path.join(TRANSCRIPTS_DIR, f"{date}.jsonl")
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    entries.reverse()
    return entries[:max(1, limit)]


def transcript_payload(date: str, limit: int) -> dict:
    days = transcript_days()
    if not date:
        date = days[0] if days else ""
    return {"date": date, "days": days,
            "entries": read_transcript_day(date, limit) if date else []}


class Handler(BaseHTTPRequestHandler):
    server_version = "scanner-api/1.0"

    def _send(self, code: int, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code: int, html: str):
        data = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # journal noise control
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send_html(200, CAPTIONS_HTML)
        elif url.path == "/api":
            self._send(200, {
                "service": "scanner-api (V2 bridge)",
                "endpoints": ["/api/status", "/api/calls?limit=N",
                              "/api/transcribe", "/api/transcript?date=&limit=N",
                              "/api/source/moswin", "/api/monitor/squelch"],
                "audio": "https://icecast.rg2.io/ems.mp3",
                "console": "https://scanner.rg2.io/",
                "ui": "/",
            })
        elif url.path == "/api/status":
            self._send(200, status_payload())
        elif url.path == "/api/calls":
            q = parse_qs(url.query)
            limit = _int(q.get("limit", ["15"])[0], 15)
            self._send(200, calls_payload(limit))
        elif url.path == "/api/transcribe":
            self._send(200, live_caption())
        elif url.path == "/api/transcript":
            q = parse_qs(url.query)
            date = q.get("date", [""])[0]
            limit = _int(q.get("limit", ["1000"])[0], 1000)
            self._send(200, transcript_payload(date, limit))
        elif url.path == "/api/monitor/squelch":
            self._send(200, {"enabled": False, "active_on_monitor": False})
        elif url.path == "/api/atc":
            self._send(200, {"active": atc_active_freq(), "mount": ATC_MOUNT,
                             "presets": ATC_PRESETS,
                             "note": "starting ATC preempts P25; auto-returns after 10 min"})
        elif url.path.startswith("/recordings/"):
            self._send(404, {"error": "no recordings on scanner v2 yet"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        # read the body (needed for ATC start; drained otherwise for keep-alive)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if url.path == "/api/source/moswin":
            self._send(200, {"status": "already moswin", "source": "moswin"})
        elif url.path == "/api/atc/start":
            try:
                freq = int(json.loads(body or "{}").get("freq"))
            except (ValueError, TypeError):
                self._send(400, {"error": "freq (Hz int) required"})
                return
            ok, msg = atc_set(freq)
            self._send(200 if ok else 400, {"active": atc_active_freq(), "msg": msg})
        elif url.path == "/api/atc/stop":
            _ok, msg = atc_set(None)
            self._send(200, {"active": atc_active_freq(), "msg": msg})
        elif url.path == "/api/monitor/tune":
            self._send(400, {"error": "aviation monitor is /api/atc on scanner v2"})
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
