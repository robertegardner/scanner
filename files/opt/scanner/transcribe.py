#!/usr/bin/env python3
"""Transcription orchestrator for the scanner.

Mirrors the radio project's caption half (a remote faster-whisper service on a
GPU host), minus all the lyrics/RDS/AcoustID machinery. Two independent jobs,
each naturally gated by what the scheduler is doing:

  1. EMS calls — SDRTrunk writes one MP3 per captured call into
     EMS_RECORDINGS_DIR. We transcribe each new file whole (only real call
     audio, no silence) and drop a sidecar `<call>.txt` next to a mirror of the
     recordings tree under TRANSCRIPTS_DIR/calls/. scheduler.recent_calls()
     reads that sidecar to annotate the /calls log.

  2. Monitor stream — when the scheduler reports the `monitor` job active
     (aviation etc.), decode the local Icecast mount in rolling windows and
     caption it live.

Both also append to a daily JSONL log (TRANSCRIPTS_DIR/YYYY-MM-DD.jsonl) for the
/transcript page, and the monitor caption is mirrored to a tmpfs state file for
the live overlay. Everything degrades gracefully when the Whisper host is
unreachable (the common case — it's a sometimes-on GPU box): we back off and
retry, nothing crashes.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

WHISPER_URL   = os.environ.get("WHISPER_URL", "").rstrip("/")
WHISPER_TOKEN = os.environ.get("WHISPER_TOKEN", "")
ENABLED       = os.environ.get("TRANSCRIBE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SCHEDULER_URL = f"http://127.0.0.1:{os.environ.get('SCHEDULER_PORT', '8082')}"
EMS_RECORDINGS_DIR = Path(os.environ.get("EMS_RECORDINGS_DIR", "/var/lib/scanner/ems/recordings"))
TRANSCRIPTS_DIR    = Path(os.environ.get("TRANSCRIPTS_DIR", "/var/lib/scanner/transcripts"))
MONITOR_LOCAL_URL  = os.environ.get("TRANSCRIBE_MONITOR_URL",
                                    os.environ.get("RECORDING_SOURCE_URL",
                                                   "http://localhost:8000/monitor.mp3"))
TALKGROUPS_TSV = Path(os.environ.get("TALKGROUPS_TSV", "/opt/scanner/p25/moswin_talkgroups.tsv"))
STATE_PATH     = Path(os.environ.get("TRANSCRIBE_STATE_PATH", "/run/scanner/transcribe.json"))

WINDOW_SEC   = int(os.environ.get("TRANSCRIBE_WINDOW_SEC", "8"))
# Don't backfill the whole call history when Whisper first comes online — only
# transcribe calls newer than this many hours (0 disables the cap = transcribe
# everything). Going-forward calls are always covered.
CALL_MAX_AGE_H = float(os.environ.get("TRANSCRIBE_CALL_MAX_AGE_H", "6"))
# A transcribed call only updates the live /listen caption overlay if it's this
# fresh — keeps backlog drains from flashing stale text as the "live" caption.
LIVE_CAPTION_MAX_AGE_SEC = 120
SAMPLE_RATE  = 16000
BYTES_PER_SAMPLE = 2
STATUS_POLL_SEC  = 3
CALL_SCAN_SEC    = 5
# When Whisper is unreachable, stop hammering it: hold off this long before any
# further attempts (calls or stream) after a connection-level failure.
WHISPER_COOLDOWN_SEC = 60

_CALL_NAME_RE = re.compile(r"_TO_(\d+)_FROM_(\d+)")

log_lock = threading.Lock()
_breaker = {"down_until": 0.0}
_breaker_lock = threading.Lock()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _whisper_down() -> bool:
    with _breaker_lock:
        return time.time() < _breaker["down_until"]


def _trip_breaker() -> None:
    with _breaker_lock:
        _breaker["down_until"] = time.time() + WHISPER_COOLDOWN_SEC


def _talkgroup_labels() -> dict:
    labels = {}
    try:
        for line in TALKGROUPS_TSV.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                labels[parts[0].strip()] = parts[1].strip()
    except OSError:
        pass
    return labels


def transcribe_pcm(pcm: bytes) -> str | None:
    """POST raw 16 kHz s16le mono PCM to the Whisper service. Returns the text,
    "" for confidently-empty audio, or None on transport failure (caller backs
    off). Trips the cooldown breaker on connection-level errors."""
    if not WHISPER_URL or not pcm:
        return None
    if _whisper_down():
        return None
    try:
        r = requests.post(
            f"{WHISPER_URL}/transcribe",
            files={"audio": ("chunk.pcm", pcm, "application/octet-stream")},
            headers={"Authorization": f"Bearer {WHISPER_TOKEN}"},
            timeout=45)
        if not r.ok:
            _log(f"[whisper] HTTP {r.status_code}")
            return None
        return (r.json().get("text") or "").strip()
    except requests.RequestException as e:
        _log(f"[whisper] {e}")
        _trip_breaker()
        return None


def decode_to_pcm(args_in: list[str]) -> bytes | None:
    """Run ffmpeg with the given input args, returning mono 16 kHz s16le PCM."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *args_in,
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=60)
        return p.stdout or None
    except (subprocess.SubprocessError, OSError) as e:
        _log(f"[ffmpeg] {e}")
        return None


def append_log(source: str, context: str, text: str, ts: datetime | None = None) -> None:
    """Append one entry to the JSONL transcript log for ts's day (default now)."""
    if not text:
        return
    ts = ts or datetime.now()
    path = TRANSCRIPTS_DIR / f"{ts.strftime('%Y-%m-%d')}.jsonl"
    entry = {"ts": ts.isoformat(timespec="seconds"),
             "source": source, "context": context, "text": text}
    with log_lock:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_caption(source: str, context: str, text: str) -> None:
    """Mirror the latest live caption to the tmpfs state file for the overlay."""
    snap = {"text": text, "source": source, "context": context,
            "updated": time.time()}
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False))
        tmp.replace(STATE_PATH)
    except OSError as e:
        _log(f"[state] {e}")


# ---------------------------------------------------------------------------
# Job 1 — EMS call files
# ---------------------------------------------------------------------------

def sidecar_for(call_path: Path) -> Path:
    rel = call_path.relative_to(EMS_RECORDINGS_DIR)
    return TRANSCRIPTS_DIR / "calls" / rel.with_suffix(".txt")


def call_watch_loop() -> None:
    labels = _talkgroup_labels()
    labels_loaded = time.time()
    while True:
        time.sleep(CALL_SCAN_SEC)
        if _whisper_down() or not EMS_RECORDINGS_DIR.exists():
            continue
        # Refresh labels occasionally (cheap, picks up TSV edits).
        if time.time() - labels_loaded > 300:
            labels = _talkgroup_labels()
            labels_loaded = time.time()
        # Newest-first: a live call gets captioned immediately; the older
        # backlog backfills (for /calls + the log) only after the fresh ones.
        files = sorted(EMS_RECORDINGS_DIR.rglob("*.mp3"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        now = time.time()
        for f in files:
            side = sidecar_for(f)
            if side.exists():
                continue
            mtime = f.stat().st_mtime
            # Skip a file still being written (SDRTrunk just touched it).
            if now - mtime < 2:
                continue
            # Don't backfill ancient calls when Whisper first reconnects.
            if CALL_MAX_AGE_H and now - mtime > CALL_MAX_AGE_H * 3600:
                continue
            pcm = decode_to_pcm(["-i", str(f)])
            if pcm is None:
                continue
            text = transcribe_pcm(pcm)
            if text is None:        # whisper failed — leave for a later pass
                break
            side.parent.mkdir(parents=True, exist_ok=True)
            side.write_text(text, encoding="utf-8")   # may be empty (no speech)
            if text:
                m = _CALL_NAME_RE.search(f.name)
                tgid = m.group(1) if m else None
                ctx = labels.get(tgid) or (f"TG {tgid}" if tgid else "EMS")
                append_log("ems", ctx, text, ts=datetime.fromtimestamp(mtime))
                # Surface genuinely-live calls as the /listen caption overlay
                # (skip backlog so old calls don't hijack the "live" caption).
                if now - mtime < LIVE_CAPTION_MAX_AGE_SEC:
                    write_caption("ems", ctx, text)
                _log(f"[call] {ctx}: {text}")


# ---------------------------------------------------------------------------
# Job 2 — live monitor stream
# ---------------------------------------------------------------------------

def sched_status() -> dict:
    try:
        r = requests.get(f"{SCHEDULER_URL}/status", timeout=3)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return {}


def monitor_caption_loop() -> None:
    proc = None
    buf = bytearray()
    window_bytes = WINDOW_SEC * SAMPLE_RATE * BYTES_PER_SAMPLE
    context = "monitor"

    def stop():
        nonlocal proc, buf
        if proc:
            try: proc.kill()
            except Exception: pass
            proc = None
        buf = bytearray()

    while True:
        status = sched_status()
        current = status.get("current") if isinstance(status, dict) else None
        active = bool(current) and current.get("name") == "monitor"
        if not active:
            stop()
            time.sleep(STATUS_POLL_SEC)
            continue
        context = current.get("detail") or "monitor"
        if proc is None:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_delay_max", "5", "-i", MONITOR_LOCAL_URL,
                 "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        chunk = proc.stdout.read(SAMPLE_RATE * BYTES_PER_SAMPLE)  # ~1s
        if not chunk:                      # stream ended/stalled — restart it
            stop()
            time.sleep(1)
            continue
        buf.extend(chunk)
        if len(buf) < window_bytes:
            continue
        pcm, buf = bytes(buf), bytearray()
        if _whisper_down():
            continue
        text = transcribe_pcm(pcm)
        if text:
            write_caption("monitor", context, text)
            append_log("monitor", context, text)
            _log(f"[monitor] {context}: {text}")


def main() -> None:
    if not ENABLED:
        _log("[transcribe] TRANSCRIBE_ENABLED is false — idling")
        while True:
            time.sleep(3600)
    if not WHISPER_URL:
        _log("[transcribe] WHISPER_URL not set — nothing to do")
        while True:
            time.sleep(3600)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"[transcribe] up — whisper={WHISPER_URL} ems={EMS_RECORDINGS_DIR} "
         f"monitor={MONITOR_LOCAL_URL}")
    threads = [
        threading.Thread(target=call_watch_loop, daemon=True),
        threading.Thread(target=monitor_caption_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
