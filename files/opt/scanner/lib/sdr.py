import logging
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# The aviation rtl_fm path and SDRTrunk share the one Nooelec dongle. Switching
# between them (either direction) intermittently leaves the R820T in a wedged
# state: SDRTrunk's I2C register init then fails ("USB error 1: error writing
# byte buffer" -> "No Tuner Available"), and rtl_fm can come up on a stale
# device. A USB-level reset between jobs clears it; the settle delay lets the
# device re-enumerate before the next job opens it. Best-effort -- needs the
# usbreset sudoers entry; on failure we still take the settle delay, which alone
# often unsticks the hand-off.
_USBRESET = "/usr/bin/usbreset"
_DONGLE_USB_ID = "0bda:2838"  # Nooelec NESDR SMArt v5 (RTL2838)
_DONGLE_SETTLE_S = 3.0


def reset_dongle(settle_s: float = _DONGLE_SETTLE_S) -> None:
    """USB-reset the Nooelec, then let it settle, before a job opens it.

    Callers discover the dongle by USB bus/port (SDRTrunk) or device index
    (rtl_fm), both of which survive the post-reset re-enumeration.
    """
    try:
        r = subprocess.run(
            ["sudo", "-n", _USBRESET, _DONGLE_USB_ID],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=15,
        )
        if r.returncode == 0:
            log.info("Reset RTL dongle %s", _DONGLE_USB_ID)
        else:
            log.warning("usbreset %s failed rc=%s: %s",
                        _DONGLE_USB_ID, r.returncode, (r.stdout or "").strip())
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("usbreset unavailable (%s) -- using settle delay only", e)
    time.sleep(settle_s)


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
