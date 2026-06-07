"""EMS scanner job — runs SDRTrunk in headless mode for Cape County MOSWIN."""
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from jobs import Job, JobResult

log = logging.getLogger(__name__)

# SDRTrunk keeps running headless even after its tuner drops off the USB bus
# (LIBUSB_TRANSFER_NO_DEVICE / "Tuner Unplugged") — a live-but-tunerless zombie
# that decodes nothing while the UI still reads "monitoring control channel".
# Its in-process hotplug recovery is unreliable (observed 2026-06-02: dongle came
# back on the bus but SDRTrunk never re-grabbed it, ~2.5h of silent dead air).
# So we watch the log stream for tuner loss and force a full restart via the
# scheduler's requeue path, which reliably re-acquires the device.
_TUNER_LOSS_GRACE_S = 25.0   # give in-process hotplug this long to recover before forcing a restart
_TUNER_STARTUP_S = 40.0      # SDRTrunk must acquire a tuner within this long after launch
_RESTART_BACKOFF_S = 10.0    # pause before failing out, so we don't hammer a flaky/over-current USB bus

# The aviation rtl_fm path and SDRTrunk share the one Nooelec dongle. After an
# rtl_fm→SDRTrunk hand-off the R820T's I2C register init intermittently fails
# ("USB error 1: error writing byte buffer" → "No Tuner Available"), leaving a
# silent tunerless zombie (observed 2026-06-07). A USB-level reset clears that
# wedged state; the settle delay lets the device re-enumerate before SDRTrunk
# opens it. Reset is best-effort — needs the usbreset sudoers entry; on failure
# we still take the settle delay, which alone often unsticks the hand-off.
_USBRESET = "/usr/bin/usbreset"
_DONGLE_USB_ID = "0bda:2838"  # Nooelec NESDR SMArt v5 (RTL2838)
_DONGLE_SETTLE_S = 3.0


class EMSJob(Job):
    name = "ems_scanner"
    priority = 1

    def __init__(self, config: "Config"):  # noqa: F821
        self._config = config
        self._active_talkgroup: str | None = None
        self._gap_start: datetime | None = None
        self._tuner_lock = threading.Lock()
        self._tuner_ever_ok = False
        self._tuner_lost_since: float | None = None
        self._tuner_start_failed = False

    def should_requeue(self) -> bool:
        return True

    def status_detail(self) -> str:
        if self._active_talkgroup:
            return f"active: {self._active_talkgroup}"
        return "monitoring control channel"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        self._active_talkgroup = None
        self._gap_start = None
        with self._tuner_lock:
            self._tuner_ever_ok = False
            self._tuner_lost_since = None
            self._tuner_start_failed = False

        # Clear any wedged RTL state left by a prior rtl_fm hand-off before launch.
        self._reset_dongle()

        cmd = self._build_command()
        log.info("Starting SDRTrunk: %s", " ".join(cmd))

        env = os.environ.copy()
        env["SDR_TRUNK_OPTS"] = "-Xmx512m"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=os.setsid,
                env=env,
            )
        except FileNotFoundError as e:
            return JobResult(success=False, log=f"SDRTrunk not found: {e}")

        spawn_t = time.monotonic()
        output_lines: list[str] = []
        reader = threading.Thread(target=self._read_output, args=(proc, output_lines), daemon=True)
        reader.start()

        while True:
            if preempt_signal.wait(timeout=2.0):
                log.info("EMS preempted — stopping SDRTrunk")
                self._record_gap()
                self._terminate(proc, reader)
                return JobResult(success=True, log="Preempted by higher-priority job")

            rc = proc.poll()
            if rc is not None:
                reader.join(timeout=3)
                msg = "\n".join(output_lines[-20:])
                if rc == 0:
                    return JobResult(success=True, log="SDRTrunk exited cleanly")
                return JobResult(success=False, log=f"SDRTrunk exited rc={rc}\n{msg}")

            fault = self._tuner_fault(spawn_t)
            if fault:
                log.warning("EMS tuner unhealthy (%s) — restarting SDRTrunk", fault)
                self._terminate(proc, reader)
                # Brief, preempt-interruptible backoff so a genuinely dead/over-current
                # dongle doesn't trigger a restart storm via the scheduler's requeue.
                preempt_signal.wait(timeout=_RESTART_BACKOFF_S)
                return JobResult(success=False, log=f"SDRTrunk tuner lost: {fault}")

    def _reset_dongle(self) -> None:
        """USB-reset the Nooelec before launching SDRTrunk, then let it settle.

        Best-effort: if usbreset is missing or the sudoers entry isn't present,
        log and fall through to the settle delay (which on its own often unsticks
        the rtl_fm→SDRTrunk hand-off). SDRTrunk discovers by USB bus/port, not
        device number, so the post-reset re-enumeration is transparent to it.
        """
        try:
            r = subprocess.run(
                ["sudo", "-n", _USBRESET, _DONGLE_USB_ID],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=15,
            )
            if r.returncode == 0:
                log.info("Reset RTL dongle %s before SDRTrunk launch", _DONGLE_USB_ID)
            else:
                log.warning("usbreset %s failed rc=%s: %s",
                            _DONGLE_USB_ID, r.returncode, (r.stdout or "").strip())
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning("usbreset unavailable (%s) — using settle delay only", e)
        time.sleep(_DONGLE_SETTLE_S)

    def _terminate(self, proc: subprocess.Popen, reader: threading.Thread) -> None:
        """Stop the SDRTrunk process group, escalating to SIGKILL if needed."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        reader.join(timeout=3)

    def _tuner_fault(self, spawn_t: float) -> str | None:
        """Return a reason string if SDRTrunk has no usable tuner, else None."""
        now = time.monotonic()
        with self._tuner_lock:
            ever_ok = self._tuner_ever_ok
            lost_since = self._tuner_lost_since
            start_failed = self._tuner_start_failed
        # Explicit acquisition failure (e.g. "No Tuner Available" after the
        # RTL I2C init error) — fail out now so the requeue retries with a fresh
        # USB reset, rather than sitting silent for the whole startup window.
        if start_failed:
            return "tuner failed to start"
        if not ever_ok:
            if now - spawn_t > _TUNER_STARTUP_S:
                return "no tuner acquired within startup window"
            return None
        if lost_since is not None and now - lost_since > _TUNER_LOSS_GRACE_S:
            return "tuner unplugged and not recovered"
        return None

    _INTERESTING = ("BROADCAST", "ICECAST", "STREAM", "CALL ", "PLAYLIST", "CHANNEL", "ALIAS", "TUNER")

    def _read_output(self, proc: subprocess.Popen, lines: list[str]) -> None:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines.append(line)
            upper = line.upper()
            if "] ERROR " in line or "] WARN " in line:
                log.warning("SDRTrunk: %s", line)
            elif any(k in upper for k in self._INTERESTING):
                log.info("SDRTrunk: %s", line)
            self._track_tuner(upper)
            self._parse_sdrtrunk_line(line)
            if len(lines) > 500:
                lines[:] = lines[-500:]

    def _track_tuner(self, upper: str) -> None:
        """Watch SDRTrunk's log for tuner acquire-success / failure / loss markers.

        NOTE: "ADDED / STARTING" and "TUNER PLUG-IN DETECTED" are *attempt*
        markers — SDRTrunk logs them even when the tuner then fails to
        initialize (observed 2026-06-07: RTL I2C write I/O error right after an
        rtl_fm hand-off still logged both, so the old watchdog thought the tuner
        was healthy and never restarted → silent dead air). So success is keyed
        off a channel actually decoding samples ("sdrtrunk channel ["), and the
        explicit acquisition-failure lines force an immediate fault instead.
        """
        # Genuine success: a channel thread is decoding, which only happens once
        # the tuner is delivering samples.
        if "SDRTRUNK CHANNEL [" in upper:
            with self._tuner_lock:
                self._tuner_ever_ok = True
                self._tuner_lost_since = None
                self._tuner_start_failed = False
        # Hard failure to acquire/start the tuner at launch.
        elif ("NO TUNER AVAILABLE" in upper
              or "UNABLE TO START" in upper
              or "AUTO-START FAILED" in upper):
            with self._tuner_lock:
                self._tuner_start_failed = True
        # Tuner dropped off the bus after having worked.
        elif ("TUNER UNPLUGGED" in upper
              or "LIBUSB_TRANSFER_NO_DEVICE" in upper
              or "LIBUSB_ERROR_NO_DEVICE" in upper):
            with self._tuner_lock:
                if self._tuner_lost_since is None:
                    self._tuner_lost_since = time.monotonic()

    def _parse_sdrtrunk_line(self, line: str) -> None:
        """Extract talkgroup activity from SDRTrunk log output."""
        # SDRTrunk logs calls like: "CALL [Cape CO EMS] ..."
        if "CALL" in line and "[" in line:
            try:
                tg = line.split("[")[1].split("]")[0]
                self._active_talkgroup = tg
            except IndexError:
                pass
        elif "IDLE" in line or "END" in line:
            self._active_talkgroup = None

    def _record_gap(self) -> None:
        self._gap_start = datetime.now()
        gap_log = Path(self._config.ems_recordings_dir) / "gaps.log"
        gap_log.parent.mkdir(parents=True, exist_ok=True)
        ts = self._gap_start.isoformat(timespec="seconds")
        tg = self._active_talkgroup or "idle"
        with gap_log.open("a") as f:
            f.write(f"{ts} preempted (was: {tg})\n")

    def _build_command(self) -> list[str]:
        return [
            self._config.sdrtrunk_bin,
            "--headless",
        ]
