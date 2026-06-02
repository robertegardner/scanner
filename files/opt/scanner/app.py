"""Scanner Flask UI — port 8081.

Serves the dashboard and gallery. All live data proxies to the scheduler
running on localhost:SCHEDULER_PORT.
"""
import os
import sys
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template, request, send_file

app = Flask(__name__)

SCHEDULER_URL = f"http://127.0.0.1:{os.environ.get('SCHEDULER_PORT', '8082')}"
EMS_RECORDINGS_DIR = Path(os.environ.get("EMS_RECORDINGS_DIR", "/var/lib/scanner/ems/recordings"))
MANUAL_RECORDINGS_DIR = Path(os.environ.get("MANUAL_RECORDINGS_DIR", "/var/lib/scanner/manual"))
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "/var/lib/scanner/recordings"))
NOAA_DATA_DIR = Path(os.environ.get("NOAA_DATA_DIR", "/var/lib/scanner/noaa"))
ICECAST_STREAM_URL = os.environ.get("ICECAST_STREAM_URL", "")
MONITOR_STREAM_URL = os.environ.get("MONITOR_STREAM_URL", "")
MONITOR_DEFAULT_DURATION_S = int(os.environ.get("MONITOR_DEFAULT_DURATION_S", "600"))


def _sched(path: str, method: str = "GET", json: dict | None = None) -> dict | list:
    try:
        resp = requests.request(method, SCHEDULER_URL + path, json=json, timeout=3)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


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


@app.route("/gallery")
def gallery():
    images = []
    img_dir = NOAA_DATA_DIR / "images"
    if img_dir.exists():
        for day_dir in sorted(img_dir.iterdir(), reverse=True):
            if day_dir.is_dir():
                for img in sorted(day_dir.glob("*.png"), reverse=True):
                    size = img.stat().st_size
                    if size < 10240:  # skip 0-byte or corrupt decode artifacts
                        continue
                    images.append({
                        "date": day_dir.name,
                        "filename": img.name,
                        "url": f"/noaa/{day_dir.name}/{img.name}",
                        "size_kb": round(size / 1024),
                    })
    return render_template("gallery.html", images=images)


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
    return render_template(
        "listen.html",
        status=status,
        moswin_stream_url=ICECAST_STREAM_URL,
        monitor_stream_url=MONITOR_STREAM_URL,
    )


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


@app.route("/api/passes")
def api_passes():
    return jsonify(_sched("/passes"))


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


@app.route("/noaa/<date>/<filename>")
def serve_noaa_image(date: str, filename: str):
    path = (NOAA_DATA_DIR / "images" / date / filename).resolve()
    if not str(path).startswith(str(NOAA_DATA_DIR.resolve())):
        abort(403)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="image/png")


if __name__ == "__main__":
    port = int(os.environ.get("UI_PORT", "8081"))
    app.run(host="0.0.0.0", port=port, threaded=True)
