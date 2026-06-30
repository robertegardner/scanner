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

# ---- on-demand FM/AM monitor (the V1 tuner; preempts NOAA, the default) ----
# /api/monitor/tune writes /var/lib/scanner-compute/monitor.env and restarts
# monitor.service, which stops op25 + the rtl_tcp bridge to free the R2,
# NFM/AM-demods (monitor_stream.py) to /scanner-atc.mp3, and auto-returns to P25
# after RuntimeMaxSec or on stop. systemctl is sudo-granted (scanner-monitor).
MONITOR_ENV = "/var/lib/scanner-compute/monitor.env"
MON_PRESETS = [
    {"label": "NOAA WX",        "freq": 162550000, "mode": "nfm"},
    {"label": "Marine 16",      "freq": 156800000, "mode": "nfm"},
    {"label": "CCPA EMS",       "freq": 155205000, "mode": "nfm"},
    {"label": "KCGI Tower",     "freq": 125525000, "mode": "am"},
    {"label": "Memphis Center", "freq": 133650000, "mode": "am"},
]
MON_MOUNT = "https://icecast.rg2.io/scanner-atc.mp3"
MON_MIN_HZ, MON_MAX_HZ = 24_000_000, 1_700_000_000   # Airspy R2 tuning range


def parse_freq(val):
    """'162.550M' / '125525000' / 125.525 -> Hz float. Bare number = MHz."""
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().lower()
    mult = 1.0
    if s and s[-1] in "mkg":
        mult = {"m": 1e6, "k": 1e3, "g": 1e9}[s[-1]]
        s = s[:-1]
    v = float(s) * mult
    return v if v > 1e6 else v * 1e6


def monitor_active():
    try:
        return subprocess.run(["systemctl", "is-active", "monitor.service"],
                              capture_output=True, text=True, timeout=5).stdout.strip() == "active"
    except Exception:  # noqa: BLE001
        return False


def monitor_state():
    st = {"active": monitor_active(), "freq": None, "mode": None,
          "mount": MON_MOUNT, "presets": MON_PRESETS}
    if st["active"]:
        try:
            env = {}
            for line in open(MONITOR_ENV):
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
            st["freq"] = int(float(env.get("MON_FREQ", "0"))) or None
            st["mode"] = env.get("MON_MODE")
        except Exception:  # noqa: BLE001
            pass
    return st


def monitor_tune(freq, mode, squelch):
    """ATC/airband tune — routes through the R2-mode coordinator (so it stops all
    R2 users + bounces the source fresh, not just restarts monitor.service). The
    coordinator owns the single-tuner R2. -> (ok, message)."""
    if not (MON_MIN_HZ <= freq <= MON_MAX_HZ):
        return False, "frequency out of range"
    mode = {"fm": "nfm", "nfm": "nfm", "am": "am"}.get(mode, "")
    if not mode:
        return False, "mode must be nfm or am"
    return r2_set_mode("atc", freq, mode, squelch)


def monitor_stop():
    # Stopping ATC returns the R2 to its NOAA default (via the coordinator).
    return r2_set_mode("noaa")


# ---- R2-mode coordinator (Phase 4): the discone/R2 is single-tuner, so NOAA /
# P25 are mutually exclusive. r2-mode.sh is the single authority — it stops all R2
# users, bounces the Pi source fresh (it degrades on client switches), and starts
# the requested mode. NOAA is the 24/7 default; P25 and ATC preempt it on demand.
R2_UNITS = [("noaa", "wx-on-r2.service"), ("p25", "op25-ems.service"),
            ("atc", "monitor.service")]


def _unit_active(unit):
    return subprocess.run(["systemctl", "is-active", unit],
                          capture_output=True, text=True).stdout.strip() == "active"


def r2_state():
    for mode, unit in R2_UNITS:
        if _unit_active(unit):
            return {"mode": mode, "unit": unit}
    return {"mode": "idle", "unit": None}


def r2_set_mode(mode, freq=None, audio_mode="am", squelch=0.0):
    # r2-mode.sh takes ~15s (stop-all + Pi source bounce + start) and op25's CC
    # lock takes longer still — fire-and-forget; the GUI polls /api/r2/state.
    if mode in ("noaa", "p25"):
        subprocess.Popen(["sudo", "/opt/scanner-compute/r2-mode.sh", mode])
        return True, f"switching R2 -> {mode}"
    if mode == "atc":
        if not freq:
            return False, "atc requires freq"
        gains = "LNA:13,MIX:12,VGA:13" if audio_mode == "nfm" else "LNA:14,MIX:13,VGA:14"
        sq = round(max(0.0, squelch) / 150.0 * 0.03, 4) if squelch else 0.0
        try:
            with open(MONITOR_ENV, "w") as f:
                f.write(f"MON_FREQ={int(freq)}\nMON_MODE={audio_mode}\n"
                        f"MON_GAINS={gains}\nMON_SQUELCH={sq}\n")
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        subprocess.Popen(["sudo", "/opt/scanner-compute/r2-mode.sh", "atc"])
        return True, f"switching R2 -> atc {int(freq)}"
    return False, f"invalid mode {mode!r} (noaa|p25|atc)"


# Minimal human UI served at "/" (ems.rg2.io): live EMS caption + recent
# transcript log, polling the same-origin /api/transcribe + /api/transcript
# endpoints. Stdlib-only, no templates — a plain literal (braces are CSS/JS, so
# this must NOT be an f-string/.format target).
CAPTIONS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Scanner Monitor</title>
<style>
:root{--bg-deep:#0a0a0c;--bg-panel:#15161a;--bg-raise:#1f2126;--bg-button:#2a2d33;--bg-button-hover:#353941;--line:#2c2e34;--amber:#ffae3a;--amber-dim:#6b4818;--green:#6df09b;--red:#f06d6d;--text:#e6e6e6;--text-dim:#888;--text-faint:#555;--accent:#2563eb}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:radial-gradient(circle at 50% -20%,#1a1c22 0%,var(--bg-deep) 70%);color:var(--text);font-family:system-ui,-apple-system,sans-serif;min-height:100vh}
header{background:#1a1d27;border-bottom:1px solid #2d3148;padding:12px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
header h1{font-size:1.1rem;font-weight:600;letter-spacing:.05em;color:#a5b4fc}
nav a{font-size:.82rem;color:#94a3b8;text-decoration:none}
nav a:hover{color:#e2e8f0}
.wrap{display:flex;justify-content:center;align-items:flex-start;padding:1.6rem 1rem 3rem}
.tuner{width:100%;max-width:540px;background:linear-gradient(180deg,#1a1c22 0%,var(--bg-panel) 100%);border:1px solid var(--line);border-radius:18px;box-shadow:0 20px 60px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.04);padding:1.25rem;display:flex;flex-direction:column;gap:1rem}
.lcd{background:linear-gradient(180deg,#0d0e10 0%,#16181c 100%);border:1px solid #000;border-radius:10px;padding:1rem 1.25rem;box-shadow:inset 0 2px 8px rgba(0,0,0,.7);min-height:120px}
.lcd-row{display:flex;justify-content:space-between;align-items:baseline}
.lcd-band{color:var(--amber);font-family:'Courier New',monospace;font-size:.85rem;font-weight:700;letter-spacing:.12em;text-shadow:0 0 8px var(--amber)}
.lcd-status{display:flex;gap:.65rem;align-items:center;font-size:.7rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.1em}
.led{width:7px;height:7px;border-radius:50%;background:#333;transition:background .15s,box-shadow .15s;display:inline-block}
.led.on{background:var(--green);box-shadow:0 0 8px var(--green)}
.led.amber{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.led.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.lcd-freq{color:var(--amber);font-family:'Courier New',monospace;font-weight:700;font-size:3.2rem;letter-spacing:.04em;line-height:1;margin:.45rem 0 .2rem;text-shadow:0 0 12px rgba(255,174,58,.6);font-variant-numeric:tabular-nums}
.lcd-freq .unit{font-size:1rem;color:var(--amber-dim);margin-left:.35rem;letter-spacing:.15em;vertical-align:middle}
.lcd-call{color:var(--text);font-size:1rem;font-weight:500;min-height:1.4em}
.lcd-call .sub{color:var(--text-dim);font-weight:400;font-size:.82rem}
.presets-label{font-size:.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.15em;margin-bottom:.4rem;padding-left:.25rem}
.presets{display:grid;grid-template-columns:repeat(2,1fr);gap:.5rem}
.preset{background:var(--bg-button);border:1px solid transparent;border-radius:8px;padding:.7rem .5rem;cursor:pointer;text-align:center;transition:all .12s}
.preset:hover{background:var(--bg-button-hover)}
.preset.active{border-color:var(--amber);background:#2c2520}
.preset .plabel{display:block;font-size:.9rem;font-weight:600;color:var(--text)}
.preset .pdesc{display:block;margin-top:.2rem;font-size:.7rem;color:var(--text-dim);font-family:'Courier New',monospace}
.controls{display:flex;align-items:center;gap:.5rem;justify-content:space-between}
.ctrl-left{display:flex;gap:.4rem}
.ctrl-right{display:flex;align-items:center;gap:.5rem}
.btn{background:var(--bg-button);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:.6rem .85rem;font-size:.85rem;font-weight:500;cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;justify-content:center;gap:.35rem;min-width:44px;min-height:40px}
.btn:hover:not(:disabled){background:var(--bg-button-hover)}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn.primary{background:var(--accent);border-color:var(--accent)}
.btn.primary:hover:not(:disabled){filter:brightness(1.1)}
.btn.danger{background:#3a2424;border-color:#5a3434;color:#f06d6d}
.btn.danger:hover:not(:disabled){background:#4a2c2c}
.vol-wrap{display:flex;align-items:center;gap:.4rem}
.vol-icon{font-size:1rem;color:var(--text-dim)}
input[type=range]{-webkit-appearance:none;appearance:none;width:110px;height:6px;background:var(--bg-raise);border-radius:3px;outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:18px;height:18px;background:var(--amber);border-radius:50%;cursor:pointer;box-shadow:0 0 4px rgba(255,174,58,.5)}
input[type=range]::-moz-range-thumb{width:18px;height:18px;background:var(--amber);border-radius:50%;cursor:pointer;border:0}
.sq-row{display:flex;align-items:center;gap:.6rem;padding:0 .25rem}
.sq-label{font-size:.65rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.12em;min-width:1.6rem}
.sq-val{font-size:.78rem;color:var(--amber);font-family:'Courier New',monospace;min-width:2.8rem}
.sq-hint{font-size:.65rem;color:var(--text-faint);margin-left:auto}
.status-bar{font-size:.75rem;color:var(--text-dim);min-height:1.2em;padding:0 .25rem}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;padding:1rem;z-index:100}
.modal-bg.show{display:flex}
.modal{background:var(--bg-panel);border:1px solid var(--line);border-radius:14px;padding:1.1rem;width:100%;max-width:340px;display:flex;flex-direction:column;gap:.75rem}
.modal h3{font-size:1rem;color:var(--text)}
.modal label{font-size:.8rem;color:var(--text-dim);display:block;margin-bottom:.25rem}
.modal select,.modal input[type=text]{width:100%;padding:.5rem .65rem;background:var(--bg-raise);border:1px solid var(--line);border-radius:6px;color:var(--text);font-size:.9rem;font-family:'Courier New',monospace;outline:none}
.modal input:focus,.modal select:focus{border-color:var(--amber)}
.modal-row{display:flex;gap:.5rem}
.modal-row .btn{flex:1}
.toast{position:fixed;bottom:1.5rem;left:50%;transform:translateX(-50%);background:var(--bg-button);color:var(--text);padding:.6rem 1rem;border-radius:6px;border:1px solid var(--line);font-size:.85rem;opacity:0;transition:opacity .25s;pointer-events:none;z-index:200}
.toast.show{opacity:1}
.footer{text-align:center;font-size:.7rem;color:var(--text-faint)}
.footer a{color:var(--text-dim);text-decoration:none}
.note{font-size:.66rem;color:var(--text-faint);text-align:center;letter-spacing:.04em}
@media (max-width:400px){.lcd-freq{font-size:2.4rem}input[type=range]{width:80px}}
.hsub{font-size:.72rem;color:var(--text-faint);letter-spacing:.04em}
.modebar{display:flex;gap:.4rem;background:#0d0e10;border:1px solid #000;border-radius:10px;padding:.35rem;box-shadow:inset 0 2px 8px rgba(0,0,0,.7)}
.modebtn{flex:1;background:transparent;border:1px solid transparent;border-radius:7px;color:var(--text-dim);padding:.55rem .4rem;font-size:1rem;font-weight:700;letter-spacing:.04em;cursor:pointer;transition:all .12s;display:flex;flex-direction:column;align-items:center;gap:.15rem;min-height:50px;justify-content:center}
.modebtn:hover{background:var(--bg-button);color:var(--text)}
.modebtn.sel{background:#2c2520;border-color:var(--amber);color:var(--amber);text-shadow:0 0 8px rgba(255,174,58,.5)}
.modebtn .sub{font-size:.58rem;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--text-faint);display:flex;align-items:center;gap:.3rem}
.modebtn.sel .sub{color:var(--amber-dim)}
.livedot{width:6px;height:6px;border-radius:50%;background:#333;display:inline-block}
.modebtn.live .livedot{background:var(--green);box-shadow:0 0 6px var(--green)}
.switching{font-size:.78rem;color:var(--amber);text-align:center;min-height:1.1em;letter-spacing:.03em;text-shadow:0 0 6px rgba(255,174,58,.3)}
.panel{display:none;flex-direction:column;gap:1rem}
.panel.show{display:flex}
.simple-lcd{background:linear-gradient(180deg,#0d0e10,#16181c);border:1px solid #000;border-radius:10px;padding:1.1rem 1.25rem;box-shadow:inset 0 2px 8px rgba(0,0,0,.7);text-align:center}
.simple-lcd .big{color:var(--amber);font-family:'Courier New',monospace;font-weight:700;font-size:1.7rem;text-shadow:0 0 10px rgba(255,174,58,.5);font-variant-numeric:tabular-nums}
.simple-lcd .sub{color:var(--text-dim);font-size:.85rem;margin-top:.35rem}
audio{width:100%;height:40px}
.console-wrap{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#0d0e10}
.console-bar{display:flex;justify-content:space-between;align-items:center;padding:.45rem .75rem;font-size:.72rem;color:var(--text-dim);border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.08em}
.console-bar a{color:var(--accent);text-decoration:none}
.console-wrap iframe{width:100%;height:440px;border:0;display:block;background:#0d0e10}
</style></head><body>
<header>
<h1>Scanner</h1><span class="hsub">discone &middot; single tuner</span>
<nav><a href="https://p25.rg2.io/">archive</a> &nbsp; <a href="https://wx.rg2.io/">weather</a></nav>
</header>
<div class="wrap"><div class="tuner">
<div class="modebar">
<button class="modebtn" id="m-noaa">NOAA<span class="sub">default <span class="livedot"></span></span></button>
<button class="modebtn" id="m-p25">P25<span class="sub">trunk <span class="livedot"></span></span></button>
<button class="modebtn" id="m-atc">ATC<span class="sub">airband <span class="livedot"></span></span></button>
</div>
<div class="switching" id="switching"></div>
<div class="panel" id="p-noaa">
<div class="simple-lcd"><div class="big">NOAA Weather Radio</div><div class="sub">162.550 MHz &middot; 24/7 default</div></div>
<audio id="noaaaudio" controls preload="none" src="https://icecast.rg2.io/wx.mp3"></audio>
</div>
<div class="panel" id="p-p25">
<div class="simple-lcd"><div class="big" id="p25tg">MOSWIN P25</div><div class="sub" id="p25sub">trunk scanner</div></div>
<audio id="p25audio" controls preload="none"></audio>
<div id="capbox" style="display:none;padding:10px 14px;background:#11151f;border:1px solid #2d3148;border-radius:8px;color:#cbd5e1;font-style:italic;line-height:1.4">
<span id="caplabel" style="font-style:normal;font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:#64748b;margin-right:8px">caption</span><span id="captext"></span></div>
<div class="console-wrap" id="console" style="display:none">
<div class="console-bar"><span>op25 console</span><a id="consolefs" href="https://scanner.rg2.io/" target="_blank" rel="noopener">open fullscreen &#8599;</a></div>
<iframe id="consoleframe" title="op25 console" referrerpolicy="no-referrer"></iframe></div>
</div>
<div class="panel" id="p-atc">
<div class="lcd">
<div class="lcd-row"><div class="lcd-band" id="band">ATC</div>
<div class="lcd-status"><span class="led" id="led"></span> <span id="ledtxt">MONITOR</span></div></div>
<div class="lcd-freq" id="freq">---.---<span class="unit">MHz</span></div>
<div class="lcd-call" id="call">Airband / FM monitor &middot; pick a preset or direct-tune</div>
</div>
<div><div class="presets-label">Presets &mdash; click to monitor (preempts NOAA)</div>
<div class="presets" id="presets"></div></div>
<div class="controls">
<div class="ctrl-left"><button class="btn danger" id="stop" disabled>&#9632; Stop</button>
<button class="btn" id="tune" title="Direct tune">&#9000; Tune</button></div>
<div class="ctrl-right"><button class="btn primary" id="play">&#9654;</button>
<div class="vol-wrap"><span class="vol-icon" id="volicon">&#128266;</span>
<input type="range" id="vol" min="0" max="100" value="80"></div></div>
</div>
<div class="sq-row"><span class="sq-label">SQ</span>
<input type="range" id="sq" min="0" max="150" value="0" style="width:120px">
<span class="sq-val" id="sqval">OFF</span><span class="sq-hint">re-tune to apply</span></div>
<div class="note">Tuning ATC/airband preempts NOAA (the 24/7 default) and auto-returns after 30 min.</div>
</div>
<div class="status-bar" id="status"></div>
<div class="footer"><a href="/transcript">transcript log</a></div>
</div></div>
<audio id="audio" preload="none"></audio>
<div class="modal-bg" id="tunemodal"><div class="modal"><h3>Direct Tune</h3>
<div><label>Frequency (e.g. 162.550M or 125.525M)</label><input type="text" id="dtfreq" placeholder="162.550M" autocomplete="off"></div>
<div><label>Mode</label><select id="dtmode"><option value="nfm">NFM (narrowband FM)</option><option value="am">AM (airband)</option></select></div>
<div class="modal-row"><button class="btn primary" id="dtgo">Tune</button><button class="btn" id="dtcancel">Cancel</button></div>
</div></div>
<div class="toast" id="toast"></div>
<script>
'use strict';
var $=function(i){return document.getElementById(i)};
var ICE='https://icecast.rg2.io';
var ATC_MOUNT=ICE+'/scanner-atc.mp3';
var audio=$('audio');
var presets=[], active=null, isPlaying=false, tuning=false;
var activeMode='idle', view=null, pending=null, pendingSince=0;
var allAudio=['noaaaudio','p25audio','audio'].map($).filter(Boolean);
allAudio.forEach(function(a){a.addEventListener('play',function(){allAudio.forEach(function(b){if(b!==a)b.pause()})})});
function esc(s){return String(s).replace(/[&<>]/g,function(m){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[m]})}
function fmtMHz(hz){return (hz/1e6).toFixed(3)}
function modeLabel(m){return m==='am'?'AM':'NFM'}
function showToast(m){var t=$('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._tm);t._tm=setTimeout(function(){t.classList.remove('show')},2800)}
function setLed(s){var l=$('led');l.classList.remove('on','amber','err');if(s)l.classList.add(s)}
function setStatus(m){$('status').textContent=m||''}
function setSwitching(m){$('switching').textContent=m||''}
// ---- mode switcher + panels ----
function setView(m){view=m;
 ['noaa','p25','atc'].forEach(function(k){$('p-'+k).classList.toggle('show',k===m);$('m-'+k).classList.toggle('sel',k===m)});
 if(m==='p25')ensureConsole()}
function clickMode(m){
 setView(m);
 if(m==='atc')return;                         // ATC switches the R2 on tune
 if(activeMode!==m){pending=m;pendingSince=Date.now();
  setSwitching('Switching to '+m.toUpperCase()+'… ~15s (takes the shared tuner)');
  fetch('/api/r2/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})}).catch(function(){})}}
function ensureConsole(){
 var f=$('consoleframe'), show=(view==='p25'&&activeMode==='p25');
 $('console').style.display=show?'block':'none';
 if(show){if(f.dataset.loaded!=='1'){f.src='https://scanner.rg2.io/';f.dataset.loaded='1'}}
 else{f.removeAttribute('src');f.dataset.loaded=''}}
function applyR2(d){
 activeMode=d.mode||'idle';
 ['noaa','p25','atc'].forEach(function(k){$('m-'+k).classList.toggle('live',k===activeMode)});
 if(pending){if(activeMode===pending){pending=null;setSwitching('')}
  else if(Date.now()-pendingSince>30000){pending=null;setSwitching('')}}
 if(view===null)setView(activeMode==='idle'?'noaa':activeMode);
 if(view==='p25')ensureConsole();
 if(activeMode==='p25'){var pa=$('p25audio');if(!pa.getAttribute('src'))pa.src=ICE+'/ems.mp3'}}
// ---- P25 talkgroup + captions ----
function pollStatus(){fetch('/api/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(s){
 var c=s.current;
 if(c&&c.detail){if(c.detail.indexOf('active:')===0){$('p25tg').textContent=c.detail.replace('active:','TG').trim();$('p25sub').textContent='call in progress'}
  else{$('p25tg').textContent='MOSWIN P25';$('p25sub').textContent='monitoring control channel'}}
 else{$('p25tg').textContent='MOSWIN P25';$('p25sub').textContent=(activeMode==='p25'?'control channel locking…':'not active')}
}).catch(function(){})}
function pollCap(){fetch('/api/transcribe',{cache:'no-store'}).then(function(r){return r.json()}).then(function(c){
 var fresh=c.text&&(Date.now()/1000-(c.updated||0)<30);
 if(fresh&&view==='p25'){$('caplabel').textContent=c.context||'caption';$('captext').textContent=c.text;$('capbox').style.display='block'}else{$('capbox').style.display='none'}
}).catch(function(){})}
// ---- ATC tuner (preempts NOAA) ----
function lcd(freq,mode,label){
 if(!freq){$('band').textContent='ATC';$('freq').innerHTML='---.---<span class="unit">MHz</span>';$('call').innerHTML='Airband / FM monitor &middot; pick a preset or direct-tune';return}
 $('band').textContent=modeLabel(mode);
 $('freq').innerHTML=fmtMHz(freq)+'<span class="unit">MHz</span>';
 $('call').innerHTML=label?esc(label)+' <span class="sub">&middot; '+fmtMHz(freq)+' '+modeLabel(mode)+'</span>':'<span class="sub">'+fmtMHz(freq)+'</span>'}
function renderPresets(){
 var g=$('presets');g.innerHTML='';
 presets.forEach(function(p){
  var b=document.createElement('button');
  b.className='preset'+(active&&active===p.freq?' active':'');
  b.innerHTML='<span class="plabel">'+esc(p.label)+'</span><span class="pdesc">'+fmtMHz(p.freq)+' '+modeLabel(p.mode)+'</span>';
  b.addEventListener('click',function(){tune(p.freq,p.mode,p.label)});
  g.appendChild(b)})}
function playATC(){var u=ATC_MOUNT,sep=u.indexOf('?')>=0?'&':'?';audio.src=u+sep+'t='+Date.now();audio.volume=$('vol').value/100;
 var pr=audio.play();if(pr&&pr.catch)pr.catch(function(e){isPlaying=false;$('play').innerHTML='&#9654;';setLed('err');showToast('Play failed: '+e.message)});
 isPlaying=true;$('play').innerHTML='&#10073;&#10073;'}
function pauseATC(){audio.pause();audio.src='';isPlaying=false;$('play').innerHTML='&#9654;'}
function tune(freq,mode,label){
 if(tuning)return;tuning=true;setView('atc');
 setSwitching('Switching to ATC… ~15s (takes the shared tuner)');
 setStatus('Tuning '+fmtMHz(freq)+' '+modeLabel(mode)+'…');setLed('amber');if(isPlaying)pauseATC();
 var sq=parseInt($('sq').value)||0;
 fetch('/api/monitor/tune',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({freq:freq,mode:mode,squelch:sq})})
  .then(function(r){return r.json().then(function(j){return{ok:r.ok,j:j}})}).then(function(res){
   if(!res.ok)throw new Error(res.j.error||res.j.msg||'tune failed');
   active=freq;lcd(freq,mode,label);renderPresets();$('stop').disabled=false;
   setStatus('Waiting for stream…');return waitActive(freq)})
  .then(function(){setStatus('');setSwitching('');setLed('on');playATC()})
  .catch(function(e){setLed('err');setStatus('Error: '+e.message);setSwitching('');showToast('Tune failed: '+e.message)})
  .then(function(){tuning=false})}
function waitActive(freq){
 var deadline=Date.now()+22000;
 return new Promise(function(resolve){
  (function poll(){
   if(Date.now()>deadline){resolve();return}
   fetch('/api/monitor',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    if(d.active&&d.freq===freq){setTimeout(resolve,3000)}else{setTimeout(poll,600)}
   }).catch(function(){setTimeout(poll,600)})
  })()})}
function stopATC(){pauseATC();setLed('');
 fetch('/api/monitor/stop',{method:'POST'}).catch(function(){});
 active=null;$('stop').disabled=true;lcd(null,null,null);renderPresets();setStatus('Stopped — returning to NOAA');setTimeout(function(){setStatus('')},2500)}
function commitDirect(){var f=$('dtfreq').value.trim();if(!f){showToast('Enter a frequency');return}
 var m=$('dtmode').value;$('tunemodal').classList.remove('show');
 var hz=parseFloat(f.replace(/[mM]$/,''));hz=hz<1000?hz*1e6:hz;tune(Math.round(hz),m,f)}
function pollMonitor(){
 fetch('/api/monitor',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
  if(d.presets&&d.presets.length&&!presets.length){presets=d.presets;renderPresets()}
  if(d.mount)ATC_MOUNT=d.mount;
  if(!d.active&&active!==null&&!tuning){active=null;pauseATC();setLed('');$('stop').disabled=true;lcd(null,null,null);renderPresets()}
  if(d.active&&active===null){active=d.freq;var p=presets.filter(function(x){return x.freq===d.freq})[0];lcd(d.freq,d.mode,p?p.label:'');$('stop').disabled=false;setLed('on');renderPresets()}
 }).catch(function(){})}
// ---- wiring ----
['noaa','p25','atc'].forEach(function(k){$('m-'+k).addEventListener('click',function(){clickMode(k)})});
$('play').addEventListener('click',function(){if(isPlaying)pauseATC();else if(active)playATC();else showToast('Pick a preset first')});
$('stop').addEventListener('click',stopATC);
$('tune').addEventListener('click',function(){$('tunemodal').classList.add('show');setTimeout(function(){$('dtfreq').focus()},60)});
$('dtgo').addEventListener('click',commitDirect);
$('dtcancel').addEventListener('click',function(){$('tunemodal').classList.remove('show')});
$('dtfreq').addEventListener('keydown',function(e){if(e.key==='Enter')commitDirect();if(e.key==='Escape')$('tunemodal').classList.remove('show')});
$('tunemodal').addEventListener('click',function(e){if(e.target.id==='tunemodal')$('tunemodal').classList.remove('show')});
$('vol').addEventListener('input',function(e){audio.volume=e.target.value/100;localStorage.setItem('mon.vol',e.target.value);
 $('volicon').innerHTML=e.target.value==0?'&#128263;':e.target.value<50?'&#128264;':'&#128266;'});
$('sq').addEventListener('input',function(e){var v=parseInt(e.target.value);$('sqval').textContent=v===0?'OFF':v;localStorage.setItem('mon.sq',v)});
var sv=localStorage.getItem('mon.vol');if(sv!=null){$('vol').value=sv;$('vol').dispatchEvent(new Event('input'))}
var ss=localStorage.getItem('mon.sq');if(ss!=null){$('sq').value=ss;$('sq').dispatchEvent(new Event('input'))}
function pollR2(){fetch('/api/r2/state',{cache:'no-store'}).then(function(r){return r.json()}).then(applyR2).catch(function(){})}
pollR2();pollMonitor();pollStatus();pollCap();
setInterval(pollR2,4000);setInterval(pollMonitor,5000);setInterval(pollStatus,4000);setInterval(pollCap,3000);
</script></body></html>"""


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
                              "/api/source/moswin", "/api/monitor/squelch",
                              "/api/r2/state", "/api/r2/mode (POST {mode:noaa|p25})"],
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
        elif url.path == "/api/monitor":
            self._send(200, monitor_state())
        elif url.path == "/api/r2/state":
            self._send(200, r2_state())
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
        elif url.path == "/api/monitor/tune":
            try:
                d = json.loads(body or "{}")
                freq = parse_freq(d["freq"])
            except (ValueError, TypeError, KeyError):
                self._send(400, {"error": "freq required (e.g. 162.550M or Hz)"})
                return
            ok, msg = monitor_tune(freq, str(d.get("mode", "nfm")),
                                   float(d.get("squelch", 0) or 0))
            st = monitor_state()
            st["msg"] = msg
            self._send(200 if ok else 400, st)
        elif url.path == "/api/monitor/stop":
            _ok, msg = monitor_stop()
            st = monitor_state()
            st["msg"] = msg
            self._send(200, st)
        elif url.path == "/api/r2/mode":
            try:
                d = json.loads(body or "{}")
                mode = str(d.get("mode", ""))
                freq = parse_freq(d["freq"]) if d.get("freq") else None
            except (ValueError, TypeError, KeyError):
                self._send(400, {"error": "mode required (noaa|p25|atc); atc needs freq"})
                return
            ok, msg = r2_set_mode(mode, freq, str(d.get("audio_mode", "am")),
                                  float(d.get("squelch", 0) or 0))
            self._send(200 if ok else 400, {"ok": ok, "msg": msg, **r2_state()})
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
