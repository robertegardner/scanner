"""Scanner Flask UI — port 8081.

Serves the dashboard. All live data proxies to the scheduler
running on localhost:SCHEDULER_PORT.
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file

app = Flask(__name__)

SCHEDULER_URL = f"http://127.0.0.1:{os.environ.get('SCHEDULER_PORT', '8082')}"

# V2-interim read-only mode (SCANNER_UI_READONLY=true): proxy every scheduler
# call to the scanner-api bridge on scanner-compute instead of the local
# scheduler. The bridge is safe by construction — it has no dongle access, so
# the UI physically cannot trigger the V1 jobs whose usbreset would yank the
# RTL2838 out from under the platform's sdr-source server. Listening +
# captions + live op25 status keep working; tune/record/source actions get
# clean bridge errors (aviation returns with the Airspy R2).
READONLY = os.environ.get("SCANNER_UI_READONLY", "").lower() in ("1", "true", "yes", "on")
if READONLY:
    SCHEDULER_URL = os.environ.get("SCANNER_API_URL", "http://192.168.6.83:8081") + "/api"
EMS_RECORDINGS_DIR = Path(os.environ.get("EMS_RECORDINGS_DIR", "/var/lib/scanner/ems/recordings"))
MANUAL_RECORDINGS_DIR = Path(os.environ.get("MANUAL_RECORDINGS_DIR", "/var/lib/scanner/manual"))
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/var/lib/scanner/recordings"))
ICECAST_STREAM_URL = os.environ.get("ICECAST_STREAM_URL", "")
MONITOR_STREAM_URL = os.environ.get("MONITOR_STREAM_URL", "")
MONITOR_DEFAULT_DURATION_S = int(os.environ.get("MONITOR_DEFAULT_DURATION_S", "600"))
TALKGROUPS_TSV = Path(os.environ.get("TALKGROUPS_TSV", "/opt/scanner/p25/moswin_talkgroups.tsv"))
TRANSCRIPTS_DIR = Path(os.environ.get("TRANSCRIPTS_DIR", "/var/lib/scanner/transcripts"))
TRANSCRIBE_STATE_PATH = Path(os.environ.get("TRANSCRIBE_STATE_PATH", "/run/scanner/transcribe.json"))


def _moswin_categories() -> list[dict]:
    """Category live-stream mounts, derived from the groups in the talkgroups
    TSV (same source gen_aliases.py uses). 'All' = the full /ems.mp3 feed; each
    group has its own /ems-<slug>.mp3 mount. URLs mirror ICECAST_STREAM_URL."""
    cats = [{"name": "All", "slug": "all", "url": ICECAST_STREAM_URL}]
    groups: list[str] = []
    try:
        for line in TALKGROUPS_TSV.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].strip().isdigit() and parts[2].strip():
                g = parts[2].strip()
                if g not in groups:
                    groups.append(g)
    except OSError:
        pass
    for g in groups:
        slug = re.sub(r"[^a-z0-9]+", "-", g.lower()).strip("-")
        url = re.sub(r"ems\.mp3$", f"ems-{slug}.mp3", ICECAST_STREAM_URL) if ICECAST_STREAM_URL else ""
        cats.append({"name": g, "slug": slug, "url": url})
    return cats


def _sched(path: str, method: str = "GET", json: dict | None = None) -> dict | list:
    try:
        resp = requests.request(method, SCHEDULER_URL + path, json=json, timeout=3)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _transcript_days() -> list[str]:
    """Dates (newest first) that have a transcript log."""
    try:
        days = [p.stem for p in TRANSCRIPTS_DIR.glob("*.jsonl") if _DATE_RE.match(p.stem)]
    except OSError:
        days = []
    return sorted(days, reverse=True)


def _read_transcript_day(date: str, limit: int = 1000) -> list[dict]:
    """Parse one day's JSONL transcript log, newest first."""
    if not _DATE_RE.match(date or ""):
        return []
    path = TRANSCRIPTS_DIR / f"{date}.jsonl"
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
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
    return entries[:limit]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    status = _sched("/status")
    calls = _sched("/calls?limit=20")
    return render_template(
        "index.html",
        status=status,
        calls=calls,
        stream_url=ICECAST_STREAM_URL,
        monitor_stream_url=MONITOR_STREAM_URL,
        monitor_default_duration_s=MONITOR_DEFAULT_DURATION_S,
    )


@app.route("/calls")
def calls_page():
    calls = _sched("/calls?limit=100")
    return render_template("calls.html", calls=calls)


@app.route("/monitor")
def monitor_page():
    status = _sched("/status")
    return render_template("monitor.html", status=status,
                           stream_url=MONITOR_STREAM_URL)


@app.route("/listen")
def listen_page():
    """Source switcher: listen to any Nooelec tunable (MOSWIN P25 default,
    aviation AM on demand) — all on the one shared SDR via the scheduler."""
    status = _sched("/status")
    # V2 interim: the category sub-mounts (/ems-*.mp3) are dark — only the
    # full op25 feed exists. Offer just "All" so the pills don't dead-end.
    cats = _moswin_categories()
    if READONLY:
        cats = cats[:1]
    return render_template(
        "listen.html",
        status=status,
        moswin_stream_url=ICECAST_STREAM_URL,
        monitor_stream_url=MONITOR_STREAM_URL,
        categories=cats,
        readonly=READONLY,
    )


@app.route("/transcript")
def transcript_page():
    days = _transcript_days()
    date = request.args.get("date") or (days[0] if days else "")
    entries = _read_transcript_day(date) if date else []
    return render_template("transcript.html", entries=entries, days=days, date=date)


@app.route("/recordings")
def recordings_page():
    recordings = _sched("/recording/list")
    if isinstance(recordings, dict) and "error" in recordings:
        recordings = []
    disk = _sched("/recording/disk")
    if isinstance(disk, dict) and "error" in disk:
        disk = {}
    return render_template("recordings.html", recordings=recordings, disk=disk)


# ---------------------------------------------------------------------------
# API proxies (called by dashboard JS)
# ---------------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    return jsonify(_sched("/status"))


@app.route("/api/override", methods=["POST"])
def api_override():
    data = request.get_json(force=True)
    result = _sched("/override", method="POST", json=data)
    code = 400 if "error" in result else 200
    return jsonify(result), code


@app.route("/api/release", methods=["POST"])
def api_release():
    return jsonify(_sched("/release", method="POST"))


@app.route("/api/monitor/tune", methods=["POST"])
def api_monitor_tune():
    data = request.get_json(force=True)
    result = _sched("/monitor/tune", method="POST", json=data)
    code = 400 if "error" in result else 200
    return jsonify(result), code


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    return jsonify(_sched("/monitor/stop", method="POST"))


@app.route("/api/source/moswin", methods=["POST"])
def api_source_moswin():
    result = _sched("/source/moswin", method="POST")
    code = 400 if isinstance(result, dict) and "error" in result else 200
    return jsonify(result), code


@app.route("/api/monitor/squelch", methods=["GET"])
def api_monitor_squelch_get():
    return jsonify(_sched("/monitor/squelch"))


@app.route("/api/monitor/squelch", methods=["POST"])
def api_monitor_squelch_set():
    data = request.get_json(force=True)
    return jsonify(_sched("/monitor/squelch", method="POST", json=data))


@app.route("/api/calls")
def api_calls():
    limit = request.args.get("limit", "50")
    return jsonify(_sched(f"/calls?limit={limit}"))


@app.route("/api/transcribe")
def api_transcribe():
    """Latest live caption (written by scanner-transcribe to tmpfs)."""
    try:
        return jsonify(json.loads(TRANSCRIBE_STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return jsonify({"text": "", "source": "", "context": "", "updated": 0})


@app.route("/api/transcript")
def api_transcript():
    date = request.args.get("date", "")
    limit = int(request.args.get("limit", "1000"))
    if not date:
        days = _transcript_days()
        date = days[0] if days else ""
    return jsonify({"date": date, "days": _transcript_days(),
                    "entries": _read_transcript_day(date, limit) if date else []})


@app.route("/api/recording/start", methods=["POST"])
def api_recording_start():
    data = request.get_json(silent=True) or {}
    result = _sched("/recording/start", method="POST", json=data)
    code = 400 if isinstance(result, dict) and "error" in result else 200
    return jsonify(result), code


@app.route("/api/recording/stop", methods=["POST"])
def api_recording_stop():
    result = _sched("/recording/stop", method="POST")
    code = 400 if isinstance(result, dict) and "error" in result else 200
    return jsonify(result), code


@app.route("/api/recording/status")
def api_recording_status():
    return jsonify(_sched("/recording/status"))


@app.route("/api/recording/list")
def api_recording_list():
    return jsonify(_sched("/recording/list"))


@app.route("/api/recording/disk")
def api_recording_disk():
    return jsonify(_sched("/recording/disk"))


@app.route("/api/recording/delete", methods=["POST"])
def api_recording_delete():
    data = request.get_json(force=True)
    result = _sched("/recording/delete", method="POST", json=data)
    code = 400 if isinstance(result, dict) and "error" in result else 200
    return jsonify(result), code


@app.route("/api/stream")
def api_stream():
    status = _sched("/status")
    current = status.get("current") if isinstance(status, dict) else None
    ems_active = current is not None and current.get("name") == "ems_scanner"
    return jsonify({
        "url": ICECAST_STREAM_URL,
        "active": ems_active,
        "talkgroup": current.get("detail") if ems_active and current else None,
    })


# ---------------------------------------------------------------------------
# File serving
# ---------------------------------------------------------------------------

@app.route("/recordings/ems/<path:filename>")
def serve_ems_recording(filename: str):
    path = (EMS_RECORDINGS_DIR / filename).resolve()
    if not str(path).startswith(str(EMS_RECORDINGS_DIR.resolve())):
        abort(403)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="audio/mpeg")


@app.route("/recordings/manual/<path:filename>")
def serve_manual_recording(filename: str):
    path = (MANUAL_RECORDINGS_DIR / filename).resolve()
    if not str(path).startswith(str(MANUAL_RECORDINGS_DIR.resolve())):
        abort(403)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="audio/wav")


@app.route("/recordings/file/<path:filename>")
def serve_recording(filename: str):
    base = RECORDINGS_DIR.resolve()
    path = (RECORDINGS_DIR / filename).resolve()
    if not str(path).startswith(str(base) + os.sep) and path != base:
        abort(403)
    if not path.exists() or path.suffix != ".mp3":
        abort(404)
    return send_file(path, mimetype="audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("UI_PORT", "8081"))
    app.run(host="0.0.0.0", port=port, threaded=True)
