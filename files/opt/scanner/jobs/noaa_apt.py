"""NOAA APT satellite pass capture and decode."""
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from jobs import Job, JobResult

log = logging.getLogger(__name__)


class NOAAJob(Job):
    name = "noaa_apt"
    priority = 5

    def __init__(self, satellite: str, freq_mhz: float, duration_s: int, config):
        self.satellite = satellite
        self.freq_mhz = freq_mhz
        self.duration_s = duration_s
        self._config = config

    def should_requeue(self) -> bool:
        return False

    def status_detail(self) -> str:
        return f"{self.satellite} {self.freq_mhz} MHz"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        ts = datetime.now()
        date_str = ts.strftime("%Y-%m-%d")
        ts_str = ts.strftime("%Y%m%d_%H%M%S")
        sat_slug = self.satellite.replace(" ", "-").lower()

        raw_dir = Path(self._config.noaa_data_dir) / "raw"
        img_dir = Path(self._config.noaa_data_dir) / "images" / date_str
        raw_dir.mkdir(parents=True, exist_ok=True)
        img_dir.mkdir(parents=True, exist_ok=True)

        wav_path = raw_dir / f"{ts_str}_{sat_slug}.wav"
        img_path = img_dir / f"{ts_str}_{sat_slug}.png"

        freq_hz = int(self.freq_mhz * 1e6)
        rtl_cmd = [
            "rtl_fm",
            "-d", str(self._config.sdr_device_index),
            "-f", str(freq_hz),
            "-M", "fm",
            "-s", "60000",
            "-r", "11025",
            "-g", "49.6",   # near-max gain — satellite signal is weak
            "-",
        ]
        sox_cmd = [
            "sox",
            "-t", "raw", "-r", "11025", "-e", "signed", "-b", "16", "-",
            str(wav_path),
        ]

        log.info("NOAA capture: %s %.4f MHz → %s (%ds)",
                 self.satellite, self.freq_mhz, wav_path.name, self.duration_s)

        try:
            rtl = subprocess.Popen(rtl_cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
            sox = subprocess.Popen(sox_cmd, stdin=rtl.stdout,
                                   stderr=subprocess.DEVNULL)
            rtl.stdout.close()

            deadline = time.monotonic() + self.duration_s
            while time.monotonic() < deadline:
                if preempt_signal.wait(timeout=1.0):
                    log.warning("NOAA pass preempted — partial WAV may still decode")
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
            return JobResult(success=False, log=f"rtl_fm/sox not found: {e}")

        if not wav_path.exists() or wav_path.stat().st_size < 1024:
            return JobResult(success=False, log="No audio captured")

        # Decode WAV → PNG
        log.info("Decoding %s → %s", wav_path.name, img_path.name)
        try:
            result = subprocess.run(
                ["noaa-apt", str(wav_path), "-o", str(img_path)],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            return JobResult(
                success=False,
                log=f"noaa-apt not installed; WAV saved at {wav_path}",
            )
        except subprocess.TimeoutExpired:
            return JobResult(success=False, log="noaa-apt decode timed out")

        if result.returncode != 0:
            return JobResult(
                success=False,
                log=f"noaa-apt rc={result.returncode}: {result.stderr[:300]}",
            )

        wav_path.unlink(missing_ok=True)
        return JobResult(
            success=True,
            log=f"{self.satellite} → {img_path.name}",
            artifacts=[img_path],
        )
