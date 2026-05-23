import heapq
import threading
from typing import Optional

from jobs import Job


class JobQueue:
    def __init__(self):
        self._heap: list[tuple[int, int, Job]] = []
        self._lock = threading.Lock()
        self._counter = 0

    def push(self, job: Job) -> None:
        with self._lock:
            heapq.heappush(self._heap, (-job.priority, self._counter, job))
            self._counter += 1

    def pop(self) -> Optional[Job]:
        with self._lock:
            if self._heap:
                _, _, job = heapq.heappop(self._heap)
                return job
            return None

    def peek_priority(self) -> Optional[int]:
        with self._lock:
            return -self._heap[0][0] if self._heap else None

    def all_jobs(self) -> list[Job]:
        with self._lock:
            return [job for _, _, job in sorted(self._heap)]

    def remove_by_name(self, name: str) -> int:
        """Remove all queued jobs with the given name. Returns count removed."""
        with self._lock:
            before = len(self._heap)
            self._heap = [(p, c, j) for p, c, j in self._heap if j.name != name]
            heapq.heapify(self._heap)
            return before - len(self._heap)

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)
