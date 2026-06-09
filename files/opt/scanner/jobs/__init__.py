from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import threading


@dataclass
class JobResult:
    success: bool
    log: str
    artifacts: list[Path] = field(default_factory=list)


class Job(ABC):
    name: str
    priority: int  # 1 (lowest) to 10 (highest)
    # Underlying SDR-access tool ("rtl_fm" or "sdrtrunk"). The scheduler
    # USB-resets the dongle before a job only when this differs from the
    # previous holder's tool — the rtl_fm<->SDRTrunk swap is what intermittently
    # wedges the R820T. Same-tool hand-offs (an rtl_fm retune, a squelch-toggle
    # restart) close/reopen the device cleanly and skip the reset, sparing this
    # aging dongle a usbreset it doesn't need. EMS overrides this to "sdrtrunk".
    sdr_tool: str = "rtl_fm"

    @abstractmethod
    def run(self, preempt_signal: threading.Event) -> JobResult:
        ...

    def should_requeue(self) -> bool:
        return False

    def status_detail(self) -> str:
        """Short string shown in the UI alongside the job name."""
        return ""
