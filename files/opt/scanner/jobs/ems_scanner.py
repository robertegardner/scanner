"""EMS scanner job — runs SDRTrunk in headless mode for Cape County MOSWIN."""
import logging
import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from jobs import Job, JobResult

log = logging.getLogger(__name__)


class EMSJob(Job):
    name = "ems_scanner"
    priority = 1

    def __init__(self, config: "Config"):  # noqa: F821
        self._config = config
        self._active_talkgroup: str | None = None
        self._gap_start: datetime | None = None

    def should_requeue(self) -> bool:
        return True

    def status_detail(self) -> str:
        if self._active_talkgroup:
            return f"active: {self._active_talkgroup}"
        return "monitoring control channel"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        self._active_talkgroup = None
        self._gap_start = None

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

        output_lines: list[str] = []
        reader = threading.Thread(target=self._read_output, args=(proc, output_lines), daemon=True)
        reader.start()

        while True:
            if preempt_signal.wait(timeout=2.0):
                log.info("EMS preempted — stopping SDRTrunk")
                self._record_gap()
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                reader.join(timeout=3)
                return JobResult(success=True, log="Preempted by higher-priority job")

            rc = proc.poll()
            if rc is not None:
                reader.join(timeout=3)
                msg = "\n".join(output_lines[-20:])
                if rc == 0:
                    return JobResult(success=True, log="SDRTrunk exited cleanly")
                return JobResult(success=False, log=f"SDRTrunk exited rc={rc}\n{msg}")

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
            self._parse_sdrtrunk_line(line)
            if len(lines) > 500:
                lines[:] = lines[-500:]

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
