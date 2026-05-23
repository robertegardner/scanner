"""AIS marine polling job. Stub — implemented in Stage 6."""
import threading

from jobs import Job, JobResult


class AISJob(Job):
    name = "ais_poll"
    priority = 3

    def __init__(self, duration_s: int, config: "Config"):  # noqa: F821
        self.duration_s = duration_s
        self._config = config

    def run(self, preempt_signal: threading.Event) -> JobResult:
        return JobResult(success=False, log="AIS poll not yet implemented")
