"""NOAA APT satellite capture job. Stub — implemented in Stage 4."""
import threading

from jobs import Job, JobResult


class NOAAJob(Job):
    name = "noaa_apt"
    priority = 5

    def __init__(self, satellite: str, freq_mhz: float, duration_s: int, config: "Config"):  # noqa: F821
        self.satellite = satellite
        self.freq_mhz = freq_mhz
        self.duration_s = duration_s
        self._config = config

    def status_detail(self) -> str:
        return f"{self.satellite} {self.freq_mhz} MHz"

    def run(self, preempt_signal: threading.Event) -> JobResult:
        return JobResult(success=False, log="NOAA APT not yet implemented")
