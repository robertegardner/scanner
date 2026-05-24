"""Scanner scheduler — owns the SDR, dispatches jobs by priority.

Exposes a tiny HTTP API on localhost so the Flask UI can query status
and inject manual overrides without touching job state directly.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request

from jobs import Job, JobResult
from jobs.ems_scanner import EMSJob
from jobs.noaa_apt import NOAAJob
from lib.pass_predictor import upcoming_passes, update_tles
from lib.queue import JobQueue
from lib.sdr import SDRToken

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    scheduler_port: int
    sdrtrunk_bin: str
    sdrtrunk_home: str
    ems_recordings_dir: str
    noaa_data_dir: str
    manual_recordings_dir: str
    sdr_device_index: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            scheduler_port=int(os.environ.get("SCHEDULER_PORT", "8082")),
            sdrtrunk_bin=os.environ["SDRTRUNK_BIN"],
            sdrtrunk_home=os.environ.get("SDRTRUNK_HOME", "/var/lib/scanner/SDRTrunk"),
            ems_recordings_dir=os.environ.get("EMS_RECORDINGS_DIR", "/var/lib/scanner/ems/recordings"),
            noaa_data_dir=os.environ.get("NOAA_DATA_DIR", "/var/lib/scanner/noaa"),
            manual_recordings_dir=os.environ.get("MANUAL_RECORDINGS_DIR", "/var/lib/scanner/manual"),
            sdr_device_index=int(os.environ.get("SDR_DEVICE_INDEX", "0")),
        )


# ---------------------------------------------------------------------------
# Manual override job
# ---------------------------------------------------------------------------

_VALID_FREQ = re.compile(r"^\d+(\.\d+)?[kMG]?$")
_VALID_MODE = {"fm", "am", "usb", "lsb", "wbfm", "raw"}


class ManualJob(Job):
    name = "manual_override"
    priority = 10

    def __init__(self, freq: str, mode: str, duration_s: int, config: Config):
        self.freq = freq
        self.mode = mode
        self.duration_s = min(max(duration_s, 5), 3600)
        self._config = config
        self.output_file: Optional[Path] = None

    def status_detail(self) -> str:
        return f"{self.freq} {self.mode.upper()} {self.duration_s}s"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        outdir = Path(self._config.manual_recordings_dir)
        outdir.mkdir(parents=True, exist_ok=True)
        wav_path = outdir / f"{ts}_{self.freq}_{self.mode}.wav"
        self.output_file = wav_path

        rtl_fm_cmd = [
            "rtl_fm",
            "-d", str(self._config.sdr_device_index),
            "-f", self.freq,
            "-M", self.mode,
            "-s", "200000",
            "-r", "48000",
            "-",
        ]
        sox_cmd = [
            "sox",
            "-t", "raw", "-r", "48000", "-e", "signed", "-b", "16", "-",
            str(wav_path),
        ]

        log.info("Manual override: %s %s → %s", self.freq, self.mode, wav_path)

        try:
            rtl = subprocess.Popen(rtl_fm_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            sox = subprocess.Popen(sox_cmd, stdin=rtl.stdout, stderr=subprocess.DEVNULL)
            rtl.stdout.close()

            deadline = time.monotonic() + self.duration_s
            while time.monotonic() < deadline:
                if preempt_signal.wait(timeout=1.0):
                    break

            rtl.terminate()
            sox.terminate()
            try:
                rtl.wait(timeout=5)
                sox.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rtl.kill()
                sox.kill()

        except FileNotFoundError as e:
            return JobResult(success=False, log=f"rtl_fm or sox not found: {e}")

        if wav_path.exists() and wav_path.stat().st_size > 0:
            return JobResult(success=True, log=f"Recorded {wav_path.name}", artifacts=[wav_path])
        return JobResult(success=False, log="No audio captured")

    def should_requeue(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self, config: Config):
        self._config = config
        self._queue = JobQueue()
        self._sdr = SDRToken()
        self._current_job: Optional[Job] = None
        self._current_thread: Optional[threading.Thread] = None
        self._preempt_signal = threading.Event()
        self._lock = threading.Lock()
        self._activity: deque[dict] = deque(maxlen=100)
        self._manual_job: Optional[ManualJob] = None
        self._upcoming_passes: list[dict] = []

    def start(self) -> None:
        self._queue.push(EMSJob(self._config))
        threading.Thread(target=self._loop, daemon=True, name="scheduler-loop").start()
        threading.Thread(target=self._pass_watcher, daemon=True, name="pass-watcher").start()
        log.info("Scheduler started")

    def _loop(self) -> None:
        while True:
            job = self._next_job()
            self._preempt_signal.clear()

            with self._lock:
                self._current_job = job

            self._sdr.acquire(job.name)
            log.info("Starting job: %s", job.name)

            t = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            with self._lock:
                self._current_thread = t
            t.start()
            t.join()

            self._sdr.release()

            with self._lock:
                self._current_job = None
                self._current_thread = None
                if isinstance(job, ManualJob):
                    self._manual_job = None

            if job.should_requeue():
                self._queue.push(job)

    def _pass_watcher(self) -> None:
        """Background thread: predict NOAA passes and queue them ~5 min before AOS."""
        queued: set[str] = set()

        while True:
            now = datetime.now(timezone.utc)

            try:
                passes = upcoming_passes(hours_ahead=24.0)
            except Exception as e:
                log.warning("Pass prediction error: %s", e)
                time.sleep(60)
                continue

            self._upcoming_passes = [
                {
                    "satellite": p.satellite,
                    "freq_mhz": p.freq_mhz,
                    "aos": p.aos.isoformat(),
                    "los": p.los.isoformat(),
                    "max_el": p.max_el,
                }
                for p in passes[:10]
            ]

            for p in passes:
                key = f"{p.satellite}|{p.aos.isoformat()}"
                if key in queued:
                    continue
                queue_at = p.aos - timedelta(minutes=5)
                if not (queue_at <= now <= p.los):
                    continue
                # Pass is imminent or in progress — calculate remaining duration
                elapsed = max(0.0, (now - p.aos).total_seconds())
                duration_s = int((p.los - p.aos).total_seconds()) - int(elapsed) + 30
                if duration_s < 60:
                    continue
                job = NOAAJob(p.satellite, p.freq_mhz, duration_s, self._config)
                self.push_job(job)
                queued.add(key)
                log.info("Queued NOAA pass: %s AOS=%s max_el=%.1f°",
                         p.satellite, p.aos.strftime("%H:%M UTC"), p.max_el)

            # Expire keys older than 3 hours
            cutoff = (now - timedelta(hours=3)).isoformat()
            queued = {k for k in queued if k.split("|")[1] > cutoff}

            time.sleep(60)

    def _next_job(self) -> Job:
        while True:
            job = self._queue.pop()
            if job is not None:
                return job
            time.sleep(0.25)

    def _run_job(self, job: Job) -> None:
        started = datetime.now()
        result = job.run(self._preempt_signal)
        elapsed = (datetime.now() - started).total_seconds()
        entry = {
            "ts": started.isoformat(timespec="seconds"),
            "job": job.name,
            "detail": job.status_detail(),
            "success": result.success,
            "log": result.log,
            "elapsed_s": round(elapsed),
        }
        self._activity.appendleft(entry)
        if result.success:
            log.info("Job %s done in %ds", job.name, elapsed)
        else:
            log.warning("Job %s failed: %s", job.name, result.log)

    def push_job(self, job: Job) -> None:
        with self._lock:
            current = self._current_job
        if current is not None and job.priority > current.priority:
            log.info("Preempting %s for %s", current.name, job.name)
            self._preempt_signal.set()
        self._queue.push(job)

    def override(self, freq: str, mode: str, duration_s: int) -> dict:
        if not _VALID_FREQ.match(freq):
            return {"error": "invalid frequency"}
        if mode not in _VALID_MODE:
            return {"error": f"mode must be one of {sorted(_VALID_MODE)}"}
        job = ManualJob(freq, mode, duration_s, self._config)
        with self._lock:
            self._manual_job = job
        self.push_job(job)
        return {"status": "queued", "freq": freq, "mode": mode, "duration_s": duration_s}

    def release(self) -> dict:
        with self._lock:
            current = self._current_job
        if isinstance(current, ManualJob):
            self._preempt_signal.set()
            return {"status": "released"}
        self._queue.remove_by_name("manual_override")
        return {"status": "nothing to release"}

    def status(self) -> dict:
        with self._lock:
            current = self._current_job
        queue_jobs = self._queue.all_jobs()
        return {
            "current": {
                "name": current.name,
                "detail": current.status_detail(),
            } if current else None,
            "sdr_owner": self._sdr.owner,
            "queue": [{"name": j.name, "priority": j.priority, "detail": j.status_detail()}
                      for j in queue_jobs],
            "recent": list(self._activity)[:20],
            "upcoming_passes": self._upcoming_passes[:5],
        }

    def recent_calls(self, limit: int = 50) -> list[dict]:
        """Scan EMS recordings directory for recent call files."""
        recordings = Path(self._config.ems_recordings_dir)
        if not recordings.exists():
            return []
        files = sorted(recordings.rglob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        calls = []
        for f in files[:limit]:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            calls.append({
                "ts": mtime.isoformat(timespec="seconds"),
                "filename": f.name,
                "path": str(f.relative_to(recordings)),
                "size_kb": round(f.stat().st_size / 1024),
            })
        return calls

    def recent_manual(self, limit: int = 20) -> list[dict]:
        """List manual override recordings."""
        recordings = Path(self._config.manual_recordings_dir)
        if not recordings.exists():
            return []
        files = sorted(recordings.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for f in files[:limit]:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            result.append({
                "ts": mtime.isoformat(timespec="seconds"),
                "filename": f.name,
                "size_kb": round(f.stat().st_size / 1024),
            })
        return result


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def create_api(scheduler: Scheduler) -> Flask:
    api = Flask(__name__)

    @api.route("/status")
    def status():
        return jsonify(scheduler.status())

    @api.route("/override", methods=["POST"])
    def override():
        data = request.get_json(force=True)
        freq = data.get("freq", "")
        mode = data.get("mode", "fm").lower()
        duration_s = int(data.get("duration_s", 120))
        result = scheduler.override(freq, mode, duration_s)
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/release", methods=["POST"])
    def release():
        return jsonify(scheduler.release())

    @api.route("/calls")
    def calls():
        limit = min(int(request.args.get("limit", 50)), 200)
        return jsonify(scheduler.recent_calls(limit))

    @api.route("/manual_recordings")
    def manual_recordings():
        return jsonify(scheduler.recent_manual())

    @api.route("/passes")
    def passes():
        return jsonify(scheduler._upcoming_passes)

    return api


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        config = Config.from_env()
    except KeyError as e:
        sys.exit(f"Missing required environment variable: {e}")

    scheduler = Scheduler(config)
    scheduler.start()

    def _shutdown(sig, frame):
        log.info("Shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    api = create_api(scheduler)
    api.run(host="127.0.0.1", port=config.scheduler_port, threaded=True)
