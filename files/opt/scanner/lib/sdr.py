import threading


class SDRToken:
    """Tracks which job currently holds the RTL-SDR.

    The scheduler enforces exclusive access by running one job at a time;
    this token records ownership so the API can report it.
    """

    def __init__(self):
        self._owner: str | None = None
        self._lock = threading.Lock()

    def acquire(self, owner: str) -> None:
        with self._lock:
            self._owner = owner

    def release(self) -> None:
        with self._lock:
            self._owner = None

    @property
    def owner(self) -> str | None:
        with self._lock:
            return self._owner
