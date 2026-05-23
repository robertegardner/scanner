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
NOAA_DATA_DIR = Path(os.environ.get("NOAA_DATA_DIR", "/var/lib/scanner/noaa"))


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
    return render_template("index.html", status=status, calls=calls)


@app.route("/gallery")
def gallery():
    images = []
    img_dir = NOAA_DATA_DIR / "images"
    if img_dir.exists():
        for day_dir in sorted(img_dir.iterdir(), reverse=True):
            if day_dir.is_dir():
                for img in sorted(day_dir.glob("*.png"), reverse=True):
                    images.append({
                        "date": day_dir.name,
                        "filename": img.name,
                        "url": f"/noaa/{day_dir.name}/{img.name}",
                        "size_kb": round(img.stat().st_size / 1024),
                    })
    return render_template("gallery.html", images=images)


@app.route("/calls")
def calls_page():
    calls = _sched("/calls?limit=100")
    return render_template("calls.html", calls=calls)


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


@app.route("/api/calls")
def api_calls():
    limit = request.args.get("limit", "50")
    return jsonify(_sched(f"/calls?limit={limit}"))


@app.route("/api/passes")
def api_passes():
    return jsonify(_sched("/passes"))


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
