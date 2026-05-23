"""ACARS aircraft message polling job. Stub — implemented in Stage 7."""
import threading

from jobs import Job, JobResult


class ACARSJob(Job):
    name = "acars_poll"
    priority = 2

    def __init__(self, duration_s: int, config: "Config"):  # noqa: F821
        self.duration_s = duration_s
        self._config = config

    def run(self, preempt_signal: threading.Event) -> JobResult:
        return JobResult(success=False, log="ACARS poll not yet implemented")
