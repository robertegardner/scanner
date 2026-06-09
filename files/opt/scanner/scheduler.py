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
from lib.sdr import SDRToken, dongle_present, reset_dongle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default filter chains — overridden by config.env. Keep these in sync with
# files/etc/scanner/config.env.example so a missing env var produces something
# sensible rather than silence.
#
# AM chain (designed for ATC voice on the 118-137 MHz aviation band):
#   - 200/3400 Hz band-limit to the speech formant range
#   - compand transfer curve: -22dB and below maps 1:1 (no upward expansion
#     of dead air), -13dB voice RMS lifts to -8dB, peaks at -2dB compress to
#     -4dB. Soft-knee 4dB smooths the transitions.
#   - dynaudnorm with g=15 (3.75s lookahead, safe vs Icecast's 10s timeout)
#     adds the final loudness pass
#   - alimiter at 0.95 is the brick-wall safety net for any remaining peaks
#
# {squelch} is a placeholder substituted at runtime — when audio_squelch is
# True the agate filter chunk goes there; when False it becomes empty so the
# chain runs gate-less. This keeps a single env var per mode while letting
# the UI toggle gating live.
#
# dynaudnorm's gausssize defaults to 31 (~7.5s lookahead) which exceeds
# Icecast's source-timeout and silently kills the stream during startup —
# any chain published to Icecast must use a smaller g.
_DEFAULT_FILTER_AM = (
    "highpass=f=200, lowpass=f=3400, {squelch}"
    "compand=attacks=0.05:decays=0.3:points=-60/-60|-22/-18|-13/-1|-2/-4:soft-knee=4:gain=0, "
    "dynaudnorm=p=0.95:m=15:s=10:g=15, "
    "alimiter=level_in=1:level_out=0.95:limit=0.95:attack=5:release=50"
)
_DEFAULT_FILTER_FM = (
    "highpass=f=80, lowpass=f=5000, {squelch}"
    "dynaudnorm=p=0.85:m=15:s=10:g=11"
)
_DEFAULT_SQUELCH = "agate=threshold=0.06:ratio=8:attack=20:release=150:detection=rms:link=average"

# When the dongle is physically off the USB bus (over-current / a wedge so hard
# it dropped off and won't re-enumerate), every job fails instantly and a USB
# reset can't help. Rather than churn the SDRTrunk JVM + usbreset every ~15s on a
# dead bus, the scheduler loop waits this long between checks, recovering within
# a cooldown of the dongle physically returning (replug / power-cycle).
_NO_DONGLE_COOLDOWN_S = 60.0


@dataclass
class Config:
    scheduler_port: int
    sdrtrunk_bin: str
    sdrtrunk_home: str
    ems_recordings_dir: str
    noaa_data_dir: str
    manual_recordings_dir: str
    recordings_dir: str
    sdr_device_index: int
    audio_filter_am: str
    audio_filter_fm: str
    audio_squelch_filter: str
    audio_bitrate: str
    monitor_default_duration_s: int
    recording_source_url: str
    autopilot: bool
    ems_default: bool
    squelch_default: bool
    talkgroups_tsv: str
    transcripts_dir: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            scheduler_port=int(os.environ.get("SCHEDULER_PORT", "8082")),
            sdrtrunk_bin=os.environ["SDRTRUNK_BIN"],
            sdrtrunk_home=os.environ.get("SDRTRUNK_HOME", "/var/lib/scanner/SDRTrunk"),
            ems_recordings_dir=os.environ.get("EMS_RECORDINGS_DIR", "/var/lib/scanner/ems/recordings"),
            noaa_data_dir=os.environ.get("NOAA_DATA_DIR", "/var/lib/scanner/noaa"),
            manual_recordings_dir=os.environ.get("MANUAL_RECORDINGS_DIR", "/var/lib/scanner/manual"),
            recordings_dir=os.environ.get("RECORDINGS_DIR", "/var/lib/scanner/recordings"),
            sdr_device_index=int(os.environ.get("SDR_DEVICE_INDEX", "0")),
            audio_filter_am=os.environ.get("MONITOR_AUDIO_FILTER_AM", _DEFAULT_FILTER_AM),
            audio_filter_fm=os.environ.get("MONITOR_AUDIO_FILTER_FM", _DEFAULT_FILTER_FM),
            audio_squelch_filter=os.environ.get("MONITOR_AUDIO_SQUELCH", _DEFAULT_SQUELCH),
            audio_bitrate=os.environ.get("MONITOR_AUDIO_BITRATE", "64k"),
            monitor_default_duration_s=int(os.environ.get("MONITOR_DEFAULT_DURATION_S", "600")),
            recording_source_url=os.environ.get("RECORDING_SOURCE_URL", "http://localhost:8000/monitor.mp3"),
            autopilot=os.environ.get("SCHEDULER_AUTOPILOT", "true").lower() in ("1", "true", "yes", "on"),
            ems_default=os.environ.get("SCHEDULER_EMS_DEFAULT", "false").lower() in ("1", "true", "yes", "on"),
            squelch_default=os.environ.get("MONITOR_SQUELCH_DEFAULT", "true").lower() in ("1", "true", "yes", "on"),
            talkgroups_tsv=os.environ.get("TALKGROUPS_TSV", "/opt/scanner/p25/moswin_talkgroups.tsv"),
            transcripts_dir=os.environ.get("TRANSCRIPTS_DIR", "/var/lib/scanner/transcripts"),
        )


def _drain_named(stream, tag: str) -> None:
    """Forward a subprocess stream to the scheduler log with a prefix."""
    if stream is None:
        return
    try:
        for raw in stream:
            try:
                line = raw.decode(errors="replace").rstrip()
            except Exception:
                continue
            if line:
                log.info("%s: %s", tag, line)
    except Exception:
        pass


def audio_filter_for(mode: str, config: Config, audio_squelch: bool = True) -> str:
    """Pick the post-rtl_fm filter chain for the given demod mode.

    AM gets an aggressive compressor/limiter chain because rtl_fm's AM
    envelope detector has a ~14 dB dynamic range from quiet voice (-22 dBFS
    RMS) to loud aircraft transmissions (-2 dBFS peak). NBFM gets a milder
    filter (-3 dB squelch already enforced in hardware by `-l`).

    The {squelch} placeholder is substituted at runtime with the agate
    filter (when audio_squelch is True) or removed (when False). This lets
    a single env var per mode carry both states without the UI having to
    rebuild the filter from parts.

    ffmpeg's lavfi parser rejects whitespace around the comma separators
    between chained filters (silent failure — ffmpeg starts then never
    publishes), so we normalize "a, b, c" → "a,b,c" before handing it off.
    """
    template = config.audio_filter_am if mode == "am" else config.audio_filter_fm
    squelch_chunk = (config.audio_squelch_filter + ", ") if audio_squelch else ""
    chain = template.replace("{squelch}", squelch_chunk)
    return re.sub(r"\s*,\s*", ",", chain.strip())


# ---------------------------------------------------------------------------
# Manual override job
# ---------------------------------------------------------------------------

_VALID_FREQ = re.compile(r"^\d+(\.\d+)?[kMG]?$")
_VALID_MODE = {"fm", "am", "usb", "lsb", "wbfm", "raw"}

MONITOR_ICECAST_URL = os.environ.get("MONITOR_ICECAST_URL", "")

# SDRTrunk recording filenames look like:
#   20260601_224359T-Cape_County_MOSWIN__TO_4229_FROM_91986.mp3
_CALL_NAME_RE = re.compile(r"_TO_(\d+)_FROM_(\d+)")

# Cached TGID -> label map, reloaded when moswin_talkgroups.tsv changes on disk.
_TG_LABELS_CACHE: dict = {"path": None, "mtime": -1, "map": {}}


def _talkgroup_labels(path: str) -> dict:
    """Load TGID->label from the tab-separated talkgroups file (cached by mtime)."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    cache = _TG_LABELS_CACHE
    if cache["path"] == path and cache["mtime"] == mtime:
        return cache["map"]
    mapping: dict = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    mapping[parts[0].strip()] = parts[1].strip()
    except OSError:
        return cache["map"]
    cache.update(path=path, mtime=mtime, map=mapping)
    return mapping


class ManualJob(Job):
    name = "manual_override"
    priority = 10

    def __init__(self, freq: str, mode: str, duration_s: int, config: Config,
                 gain: Optional[int] = None):
        self.freq = freq
        self.mode = mode
        self.duration_s = min(max(duration_s, 5), 3600)
        self.gain = gain  # None = rtl_fm auto gain
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
        ]
        if self.gain is not None:
            rtl_fm_cmd += ["-g", str(self.gain)]
        rtl_fm_cmd.append("-")

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


class MonitorJob(Job):
    """Live-stream a frequency to an Icecast mount for browser listening."""
    name = "monitor"
    priority = 3  # preempts EMS (1) but yields to NOAA (5) and manual (10)

    def __init__(self, freq: str, mode: str, gain: int, duration_s: int,
                 label: str, config: Config, squelch: int = 0,
                 audio_squelch: bool = True):
        self.freq = freq
        self.mode = mode
        self.gain = max(0, min(gain, 60))
        # Up to 8h so the user can park on a freq and walk away while tweaking.
        self.duration_s = min(max(duration_s, 5), 28800)
        self.label = label
        self.squelch = max(0, squelch)            # rtl_fm hardware squelch (-l)
        self.audio_squelch = audio_squelch        # ffmpeg agate post-demod
        self._config = config

    def status_detail(self) -> str:
        suffix = f" — {self.label}" if self.label else ""
        sq = f" sq={self.squelch}" if self.squelch > 0 else ""
        gate = "" if self.audio_squelch else " gate-off"
        return f"{self.freq} {self.mode.upper()}{sq}{gate}{suffix}"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        if not MONITOR_ICECAST_URL:
            return JobResult(success=False, log="MONITOR_ICECAST_URL not configured")

        rtl_cmd = [
            "rtl_fm",
            "-d", str(self._config.sdr_device_index),
            "-f", self.freq,
            "-M", self.mode,
            "-s", "200000",
            "-r", "48000",
            "-g", str(self.gain),
        ]
        if self.squelch > 0:
            rtl_cmd += ["-l", str(self.squelch)]
        rtl_cmd.append("-")
        af = audio_filter_for(self.mode, self._config, self.audio_squelch)
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
            "-af", af,
            "-codec:a", "libmp3lame", "-b:a", self._config.audio_bitrate,
            "-f", "mp3", MONITOR_ICECAST_URL,
        ]

        log.info("Monitor: %s %s gain=%d squelch=%s filter=%s → Icecast (%s)",
                 self.freq, self.mode, self.gain,
                 "on" if self.audio_squelch else "off",
                 af.split(",")[0].strip(), self.label)

        try:
            rtl = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=rtl.stdout,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            rtl.stdout.close()

            # Tag stderr so failures (bad filter, icecast auth, missing tuner) are visible in journald.
            threading.Thread(target=_drain_named, args=(rtl.stderr, "monitor-rtl_fm"), daemon=True).start()
            threading.Thread(target=_drain_named, args=(ffmpeg.stderr, "monitor-ffmpeg"), daemon=True).start()

            deadline = time.monotonic() + self.duration_s
            while time.monotonic() < deadline:
                if preempt_signal.wait(timeout=1.0):
                    break

            rtl.terminate()
            ffmpeg.terminate()
            try:
                rtl.wait(timeout=5)
                ffmpeg.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rtl.kill()
                ffmpeg.kill()

        except FileNotFoundError as e:
            return JobResult(success=False, log=f"rtl_fm or ffmpeg not found: {e}")

        return JobResult(success=True, log=f"Monitor {self.freq} {self.mode} done")

    def should_requeue(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Recording manager — captures the live Icecast monitor stream to disk.
# Independent of the SDR; survives tune changes because it consumes the
# already-encoded MP3 stream rather than reaching into the rtl_fm pipeline.
# ---------------------------------------------------------------------------

_REC_FILENAME = re.compile(
    r"^rec-(\d{4}-\d{2}-\d{2})_(\d{6})-([\d.]+(?:[kMG]Hz)?)-([A-Z]+)\.mp3$"
)


def _sanitize_freq(freq: str) -> str:
    """Normalize a freq string for the filename: '131.36M' → '131.36MHz', '500k' → '500kHz'."""
    if not freq:
        return "unknown"
    if freq[-1] in "kMG":
        return f"{freq}Hz"
    return f"{freq}Hz" if freq.replace(".", "").isdigit() else freq


class RecordingManager:
    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._started: Optional[datetime] = None
        self._freq: Optional[str] = None
        self._mode: Optional[str] = None

    # ----- lifecycle -----
    def start(self, freq: Optional[str], mode: Optional[str]) -> dict:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return {"error": "already recording", "filename": self._path.name if self._path else None}

            recdir = Path(self._config.recordings_dir)
            recdir.mkdir(parents=True, exist_ok=True)

            now = datetime.now()
            freq_tag = _sanitize_freq(freq) if freq else "unknown"
            mode_tag = (mode or "unk").upper()
            filename = f"rec-{now.strftime('%Y-%m-%d_%H%M%S')}-{freq_tag}-{mode_tag}.mp3"
            path = recdir / filename

            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "30",
                "-i", self._config.recording_source_url,
                "-c:a", "copy",
                "-f", "mp3",
                str(path),
            ]

            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
            except FileNotFoundError as e:
                return {"error": f"ffmpeg not found: {e}"}

            self._proc = proc
            self._path = path
            self._started = now
            self._freq = freq
            self._mode = mode

            # Drain stderr in background so the pipe doesn't fill
            threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True).start()

            log.info("Recording started: %s ← %s", filename, self._config.recording_source_url)
            return {
                "status": "started",
                "filename": filename,
                "freq": freq,
                "mode": mode,
            }

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
            path = self._path
            if not proc or proc.poll() is not None:
                self._proc = None
                return {"error": "not recording"}

            # ffmpeg writes MP3 trailer on 'q' to stdin; SIGINT also flushes cleanly.
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

            size = path.stat().st_size if path and path.exists() else 0
            log.info("Recording stopped: %s (%d bytes)", path.name if path else "?", size)

            result = {
                "status": "stopped",
                "filename": path.name if path else None,
                "size_bytes": size,
            }
            self._proc = None
            return result

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            try:
                msg = line.decode(errors="replace").rstrip()
            except Exception:
                continue
            if msg:
                log.info("recording-ffmpeg: %s", msg)

    # ----- introspection -----
    def status(self) -> dict:
        with self._lock:
            active = self._proc is not None and self._proc.poll() is None
            if not active:
                return {"active": False}
            elapsed = (datetime.now() - self._started).total_seconds() if self._started else 0
            size = self._path.stat().st_size if self._path and self._path.exists() else 0
            return {
                "active": True,
                "filename": self._path.name,
                "freq": self._freq,
                "mode": self._mode,
                "elapsed_s": int(elapsed),
                "size_bytes": size,
            }

    def list_files(self) -> list[dict]:
        recdir = Path(self._config.recordings_dir)
        if not recdir.exists():
            return []
        out = []
        for f in sorted(recdir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True):
            stat = f.stat()
            m = _REC_FILENAME.match(f.name)
            entry = {
                "filename": f.name,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024),
                "mtime": stat.st_mtime,
                "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
            if m:
                entry["captured_freq"] = m.group(3)
                entry["captured_mode"] = m.group(4)
            out.append(entry)
        return out

    def delete_file(self, filename: str) -> dict:
        recdir = Path(self._config.recordings_dir).resolve()
        target = (recdir / filename).resolve()
        # Path traversal guard
        if not str(target).startswith(str(recdir) + os.sep) and target != recdir:
            return {"error": "invalid filename"}
        if target.suffix != ".mp3" or not target.exists():
            return {"error": "not found"}
        with self._lock:
            if self._path and self._path.resolve() == target:
                return {"error": "cannot delete file currently being recorded"}
        target.unlink()
        log.info("Recording deleted: %s", filename)
        return {"status": "deleted", "filename": filename}

    def disk_usage(self) -> dict:
        recdir = Path(self._config.recordings_dir)
        # Use the recordings dir's filesystem for the warning threshold.
        target = recdir if recdir.exists() else recdir.parent
        try:
            st = os.statvfs(target)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - free
            pct_used = (used / total * 100) if total else 0
        except OSError:
            total = free = used = pct_used = 0
        own_bytes = sum(f.stat().st_size for f in recdir.glob("*.mp3")) if recdir.exists() else 0
        return {
            "fs_total_bytes": total,
            "fs_used_bytes": used,
            "fs_free_bytes": free,
            "fs_pct_used": round(pct_used, 1),
            "recordings_bytes": own_bytes,
            "warn": pct_used > 80,
        }


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
        # Pre-acquire reset_dongle() gating state — see _loop. Together these
        # let us USB-reset only when it actually clears something, sparing this
        # aging dongle resets it doesn't need (a needless usbreset on a healthy
        # unit is what knocks it off the bus with error -71):
        #   _dongle_held_by — name of the last job that held the dongle, or None
        #     when it has freshly (re)enumerated and nothing has held it since
        #     (boot / replug / post-absence). None => skip the reset.
        #   _last_tool      — that holder's sdr_tool; a change across the hand-off
        #     (rtl_fm<->SDRTrunk) is the documented R820T wedge trigger => reset.
        #   _last_result    — that holder's JobResult; a failed/None run may have
        #     left a wedge (e.g. EMS tuner fault) => reset before retrying.
        self._dongle_held_by: Optional[str] = None
        self._last_tool: Optional[str] = None
        self._last_result: Optional[JobResult] = None
        self._preempt_signal = threading.Event()
        self._lock = threading.Lock()
        self._activity: deque[dict] = deque(maxlen=100)
        self._manual_job: Optional[ManualJob] = None
        self._monitor_job: Optional[MonitorJob] = None
        self._upcoming_passes: list[dict] = []
        self._recorder = RecordingManager(config)
        # Squelch (ffmpeg agate) state — toggled live from the dashboard.
        # New monitor tunes inherit this value unless they override it.
        self._audio_squelch = config.squelch_default

    @property
    def recorder(self) -> "RecordingManager":
        return self._recorder

    def current_freq_mode(self) -> tuple[Optional[str], Optional[str]]:
        """Return (freq, mode) of the current Monitor job, if any."""
        with self._lock:
            job = self._current_job
        if isinstance(job, MonitorJob):
            return job.freq, job.mode
        return None, None

    def start(self) -> None:
        if self._config.autopilot:
            self._queue.push(EMSJob(self._config))
            log.info("Scheduler started (autopilot ON: EMS default + NOAA passes)")
        elif self._config.ems_default:
            self._queue.push(EMSJob(self._config))
            log.info("Scheduler started (EMS default ON, NOAA off: MOSWIN runs in "
                     "background, yields to manual tunes and resumes after)")
        else:
            log.info("Scheduler started (idle until manually tuned)")
        # Always run the pass watcher: it keeps TLEs fresh and populates the
        # dashboard's upcoming-pass list in both modes. It only *queues* NOAA
        # capture jobs when autopilot is on (see _pass_watcher).
        threading.Thread(target=self._pass_watcher, daemon=True, name="pass-watcher").start()
        threading.Thread(target=self._loop, daemon=True, name="scheduler-loop").start()

    def _requeue_if_due(self, job: Job) -> None:
        """Push a finished/deferred job back onto the queue if it should recur.

        EMS self-requeues only when it's meant to be the background default
        (autopilot or ems_default) — so after a manual aviation tune ends, MOSWIN
        resumes; in plain manual mode EMS does not requeue, leaving the scheduler
        idle between tunes. Non-EMS jobs follow their own should_requeue().
        """
        if job.should_requeue() and (
            self._config.autopilot or self._config.ems_default
            or not isinstance(job, EMSJob)
        ):
            self._queue.push(job)

    def _loop(self) -> None:
        while True:
            job = self._next_job()
            self._preempt_signal.clear()

            # Every job needs the one Nooelec. If it's physically gone from the
            # USB bus (over-current / hard wedge — see lib.sdr.dongle_present),
            # don't run: SDRTrunk would just loop "No Tuner Available" and
            # usbreset would storm a dead device every ~15s. Defer with a long,
            # preempt-interruptible cooldown and requeue, so we recover within a
            # minute of the dongle returning (replug / power-cycle) without
            # hammering the bus or churning the JVM meanwhile.
            if not dongle_present():
                log.warning("Dongle not on USB bus — deferring %s for %ds "
                            "(needs replug / power-cycle)",
                            job.name, int(_NO_DONGLE_COOLDOWN_S))
                # When it returns it'll be freshly enumerated — don't usbreset it
                # on the first acquire (see reset_dongle gating below).
                self._dongle_held_by = None
                self._last_tool = None
                self._requeue_if_due(job)
                self._preempt_signal.wait(timeout=_NO_DONGLE_COOLDOWN_S)
                continue

            with self._lock:
                self._current_job = job

            # Decide whether to USB-reset before this job opens the dongle. A
            # reset only clears something real on the rtl_fm<->SDRTrunk swap (the
            # hand-off that intermittently wedges the R820T) or after the prior
            # holder faulted (which may have left a wedge). Same-tool, clean
            # hand-offs (an rtl_fm retune, a squelch-toggle restart, a clean EMS
            # restart) close/reopen the device cleanly — resetting there is pure
            # risk on this degrading dongle (a needless usbreset is what drops it
            # off the bus, error -71). ~3s when it does run.
            incoming_tool = getattr(job, "sdr_tool", "rtl_fm")
            if self._dongle_held_by is None:
                need_reset, why = False, "dongle freshly enumerated, no prior owner to clear"
            elif incoming_tool != self._last_tool:
                need_reset, why = True, f"tool change {self._last_tool}->{incoming_tool}"
            elif not (self._last_result and self._last_result.success):
                need_reset, why = True, f"previous job ({self._dongle_held_by}) faulted"
            else:
                need_reset, why = False, f"same tool ({incoming_tool}), prior run clean"

            self._sdr.acquire(job.name)
            log.info("Starting job: %s", job.name)
            if need_reset:
                log.info("Pre-acquire USB reset for %s — %s", job.name, why)
                reset_dongle()
            else:
                log.info("Skipping pre-acquire reset for %s — %s", job.name, why)

            # _run_job clears _last_result before the job runs, so a job that
            # raises leaves it None — treated as a fault above (reset on retry).
            t = threading.Thread(target=self._run_job, args=(job,), daemon=True)
            with self._lock:
                self._current_thread = t
            t.start()
            t.join()

            self._sdr.release()
            # Record what held the dongle so the next iteration can decide
            # whether the upcoming hand-off needs a reset (see gate above).
            self._dongle_held_by = job.name
            self._last_tool = incoming_tool

            with self._lock:
                self._current_job = None
                self._current_thread = None
                if isinstance(job, ManualJob):
                    self._manual_job = None
                if isinstance(job, MonitorJob):
                    self._monitor_job = None

            self._requeue_if_due(job)

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

            # In manual mode we still predict + refresh TLEs above, but we do
            # not auto-queue captures — the scheduler stays idle until a manual
            # tune. Queueing resumes immediately if autopilot is flipped on.
            for p in (passes if self._config.autopilot else []):
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
        # Cleared before the run so a job that raises out of run() leaves this
        # None — the pre-acquire gate in _loop reads None as a fault and resets
        # the dongle before the next acquire. join() guarantees visibility.
        self._last_result = None
        result = job.run(self._preempt_signal)
        self._last_result = result
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

    def override(self, freq: str, mode: str, duration_s: int,
                 gain: Optional[int] = None) -> dict:
        if not _VALID_FREQ.match(freq):
            return {"error": "invalid frequency"}
        if mode not in _VALID_MODE:
            return {"error": f"mode must be one of {sorted(_VALID_MODE)}"}
        job = ManualJob(freq, mode, duration_s, self._config, gain=gain)
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

    def monitor_tune(self, freq: str, mode: str, gain: int, label: str,
                     duration_s: int = 3600, squelch: int = 0,
                     audio_squelch: Optional[bool] = None) -> dict:
        if not _VALID_FREQ.match(freq):
            return {"error": "invalid frequency"}
        if mode not in _VALID_MODE:
            return {"error": f"mode must be one of {sorted(_VALID_MODE)}"}
        if not MONITOR_ICECAST_URL:
            return {"error": "MONITOR_ICECAST_URL not configured on server"}
        # If the caller didn't specify, inherit the dashboard's current toggle.
        # Persist their explicit choice so future toggles know the latest state.
        effective_squelch = self._audio_squelch if audio_squelch is None else bool(audio_squelch)
        if audio_squelch is not None:
            self._audio_squelch = effective_squelch
        job = MonitorJob(freq, mode, gain, duration_s, label, self._config,
                         squelch=squelch, audio_squelch=effective_squelch)
        with self._lock:
            self._monitor_job = job
            current = self._current_job
        self._queue.remove_by_name("monitor")
        # Explicitly preempt a running monitor job — same-priority jobs
        # don't trigger push_job's check, so we handle that case here.
        # push_job still handles the EMS→Monitor case (priority 3 > 1).
        if isinstance(current, MonitorJob):
            self._preempt_signal.set()
        self.push_job(job)
        return {"status": "queued", "freq": freq, "mode": mode, "gain": gain,
                "label": label, "audio_squelch": effective_squelch}

    def get_squelch(self) -> dict:
        with self._lock:
            current = self._current_job
        return {
            "enabled": self._audio_squelch,
            "active_on_monitor": isinstance(current, MonitorJob) and current.audio_squelch,
        }

    def set_squelch(self, enabled: bool) -> dict:
        """Toggle the audio gate. Restarts a running monitor job in place.

        Implementation note: ffmpeg's agate filter doesn't expose threshold as
        a runtime command, so we restart the rtl_fm→ffmpeg pipeline (~1-2s
        stream gap) rather than use the zmq/sendcmd approach. The UI should
        show a transient "Squelch on/off" message during the restart.
        """
        enabled = bool(enabled)
        with self._lock:
            self._audio_squelch = enabled
            current = self._current_job
        restarted = False
        if isinstance(current, MonitorJob):
            # Re-queue with the same freq/mode/gain/label but new squelch state.
            # We use the in-flight job's stored params, NOT _monitor_job, which
            # might be a stale reference if multiple tunes raced.
            self.monitor_tune(
                freq=current.freq,
                mode=current.mode,
                gain=current.gain,
                label=current.label,
                duration_s=current.duration_s,
                squelch=current.squelch,
                audio_squelch=enabled,
            )
            restarted = True
        return {"enabled": enabled, "restarted": restarted}

    def monitor_stop(self) -> dict:
        with self._lock:
            current = self._current_job
        if isinstance(current, MonitorJob):
            self._preempt_signal.set()
            return {"status": "stopped"}
        self._queue.remove_by_name("monitor")
        with self._lock:
            self._monitor_job = None
        return {"status": "nothing to stop"}

    def start_moswin(self) -> dict:
        """Switch the SDR to the MOSWIN P25 source (EMS / SDRTrunk job).

        EMS is the lowest-priority job, so it can't preempt a running monitor on
        its own — we stop the current monitor/manual job and queue EMS for the
        loop to pick up next. A NOAA capture in progress (priority 5) is left
        alone. Used by the /listen source switcher; works with autopilot off.
        """
        with self._lock:
            current = self._current_job
        if isinstance(current, EMSJob):
            return {"status": "already moswin", "source": "moswin"}
        if isinstance(current, NOAAJob):
            return {"error": "NOAA pass in progress; try again after it ends"}
        self._queue.remove_by_name("monitor")
        self._queue.remove_by_name("ems_scanner")
        with self._lock:
            self._monitor_job = None
        self._queue.push(EMSJob(self._config))
        # EMS (priority 1) can't preempt a monitor/manual via push_job, so do it.
        if isinstance(current, (MonitorJob, ManualJob)):
            self._preempt_signal.set()
        return {"status": "queued", "source": "moswin"}

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
        """Scan EMS recordings directory for recent call files.

        SDRTrunk names recordings with the raw talkgroup/radio numbers
        (...__TO_<tgid>_FROM_<radio>.mp3), so we map the TGID to a friendly
        label here from moswin_talkgroups.tsv (the same file gen_aliases.py
        uses). Unknown TGIDs fall back to "TG <n>".
        """
        recordings = Path(self._config.ems_recordings_dir)
        if not recordings.exists():
            return []
        labels = _talkgroup_labels(self._config.talkgroups_tsv)
        transcripts = Path(self._config.transcripts_dir) / "calls"
        files = sorted(recordings.rglob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        calls = []
        for f in files[:limit]:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            m = _CALL_NAME_RE.search(f.name)
            tgid = m.group(1) if m else None
            radio = m.group(2) if m else None
            rel = f.relative_to(recordings)
            side = transcripts / rel.with_suffix(".txt")
            try:
                transcript = side.read_text(encoding="utf-8").strip() or None
            except OSError:
                transcript = None
            calls.append({
                "ts": mtime.isoformat(timespec="seconds"),
                "filename": f.name,
                "path": str(rel),
                "size_kb": round(f.stat().st_size / 1024),
                "tgid": tgid,
                "radio": radio,
                "talkgroup": labels.get(tgid) or (f"TG {tgid}" if tgid else "—"),
                "transcript": transcript,
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
        gain_raw = data.get("gain")
        gain = int(gain_raw) if gain_raw is not None else None
        result = scheduler.override(freq, mode, duration_s, gain=gain)
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/release", methods=["POST"])
    def release():
        return jsonify(scheduler.release())

    @api.route("/monitor/tune", methods=["POST"])
    def monitor_tune():
        data = request.get_json(force=True)
        freq = data.get("freq", "")
        mode = data.get("mode", "fm").lower()
        gain = int(data.get("gain", 20))
        label = str(data.get("label", ""))
        duration_s = int(data.get("duration_s", 3600))
        squelch = int(data.get("squelch", 0))
        audio_squelch = data.get("audio_squelch")
        if audio_squelch is not None:
            audio_squelch = bool(audio_squelch)
        result = scheduler.monitor_tune(freq, mode, gain, label, duration_s,
                                        squelch=squelch, audio_squelch=audio_squelch)
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/monitor/stop", methods=["POST"])
    def monitor_stop():
        return jsonify(scheduler.monitor_stop())

    @api.route("/source/moswin", methods=["POST"])
    def source_moswin():
        result = scheduler.start_moswin()
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/monitor/squelch", methods=["GET"])
    def monitor_squelch_get():
        return jsonify(scheduler.get_squelch())

    @api.route("/monitor/squelch", methods=["POST"])
    def monitor_squelch_set():
        data = request.get_json(force=True)
        enabled = bool(data.get("enabled", True))
        return jsonify(scheduler.set_squelch(enabled))

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

    # ----- Recording -----
    @api.route("/recording/start", methods=["POST"])
    def recording_start():
        data = request.get_json(silent=True) or {}
        # Caller may pass freq/mode for the filename; otherwise we look up the
        # current monitor job so the file is named after whatever's on air.
        freq = data.get("freq")
        mode = data.get("mode")
        if not freq or not mode:
            cf, cm = scheduler.current_freq_mode()
            freq = freq or cf
            mode = mode or cm
        result = scheduler.recorder.start(freq, mode)
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/recording/stop", methods=["POST"])
    def recording_stop():
        result = scheduler.recorder.stop()
        code = 400 if "error" in result else 200
        return jsonify(result), code

    @api.route("/recording/status")
    def recording_status():
        return jsonify(scheduler.recorder.status())

    @api.route("/recording/list")
    def recording_list():
        return jsonify(scheduler.recorder.list_files())

    @api.route("/recording/disk")
    def recording_disk():
        return jsonify(scheduler.recorder.disk_usage())

    @api.route("/recording/delete", methods=["POST"])
    def recording_delete():
        data = request.get_json(force=True)
        filename = data.get("filename", "")
        result = scheduler.recorder.delete_file(filename)
        code = 400 if "error" in result else 200
        return jsonify(result), code

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
