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

    @abstractmethod
    def run(self, preempt_signal: threading.Event) -> JobResult:
        ...

    def should_requeue(self) -> bool:
        return False

    def status_detail(self) -> str:
        """Short string shown in the UI alongside the job name."""
        return ""
