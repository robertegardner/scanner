"""NOAA satellite pass predictor using pyorbital and Celestrak TLEs."""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from pyorbital.orbital import Orbital

log = logging.getLogger(__name__)

_LAT = 37.3059   # Cape Girardeau, MO
_LON = -89.5181
_ALT = 0.1       # km above sea level

_SATELLITES = {
    "NOAA 15": 137.620,
    "NOAA 18": 137.9125,
    "NOAA 19": 137.100,
}

# Fetch the whole "weather" group in ONE request rather than three per-object
# CATNR queries. Celestrak rate-limits / temporarily IP-bans clients that hammer
# gp.php with many requests; the combined group file is the documented, polite
# way to get all three NOAA APT birds at once. We filter it to the sats we want.
_TLE_GROUP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
_TLE_CATNRS = {"NOAA 15": "25338", "NOAA 18": "28654", "NOAA 19": "33591"}
_TLE_PATH = Path("/var/lib/scanner/noaa/weather.tle")
_MIN_ELEVATION = 20.0   # degrees — passes below this are too weak to bother
_TLE_MAX_AGE_S = 86400  # refresh TLEs once per day
_TLE_RETRY_INTERVAL_S = 3600  # when an update fails, wait this long before retrying
_HTTP_TIMEOUT_S = 20

# Monotonic timestamp of the last update attempt (success or failure). Used to
# throttle retries so a failing/blocked source isn't pounded on every
# pass-watcher tick (~60 s) — that tight loop is what gets the IP banned.
_last_update_attempt = 0.0


@dataclass
class Pass:
    satellite: str
    freq_mhz: float
    aos: datetime   # acquisition of signal, timezone-aware UTC
    los: datetime   # loss of signal, timezone-aware UTC
    max_el: float   # degrees


def _parse_tle_records(text: str) -> dict[str, list[str]]:
    """Parse a multi-satellite TLE file into {name: [name, line1, line2]}.

    Records are three lines: a name line, then the '1 ' and '2 ' element lines.
    Tolerates blank lines and trailing whitespace (celestrak pads name lines).
    """
    rows = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    records: dict[str, list[str]] = {}
    i = 0
    while i + 2 < len(rows):
        name, l1, l2 = rows[i], rows[i + 1], rows[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            records[name.strip()] = [name.strip(), l1, l2]
            i += 3
        else:
            i += 1  # resync if we're misaligned
    return records


def update_tles() -> None:
    """Download fresh TLEs from Celestrak's weather group in one request.

    Writes atomically, and only for the NOAA APT birds we care about. Raises on
    network/HTTP error or if none of the wanted satellites are present.
    """
    _TLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(_TLE_GROUP_URL, timeout=_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    records = _parse_tle_records(resp.text)

    lines: list[str] = []
    missing: list[str] = []
    for sat in _TLE_CATNRS:
        rec = records.get(sat)
        if rec is None:
            missing.append(sat)
            continue
        lines.extend(rec)
    if not lines:
        raise RuntimeError(
            f"weather group fetched but contained none of {list(_TLE_CATNRS)} "
            f"(got {len(records)} sats)"
        )
    if missing:
        log.warning("TLE update: missing %s from weather group; keeping the rest",
                    ", ".join(missing))

    tmp = _TLE_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(_TLE_PATH)
    log.info("TLEs updated: %s (%d sats, %d bytes)",
             _TLE_PATH, len(lines) // 3, _TLE_PATH.stat().st_size)


def _tle_stale() -> bool:
    if not _TLE_PATH.exists():
        return True
    mtime = datetime.fromtimestamp(_TLE_PATH.stat().st_mtime, tz=timezone.utc)
    age_s = (datetime.now(timezone.utc) - mtime).total_seconds()
    return age_s > _TLE_MAX_AGE_S


def _maybe_update_tles() -> None:
    """Refresh TLEs if stale, but at most once per _TLE_RETRY_INTERVAL_S.

    The pass watcher calls upcoming_passes() roughly once a minute. Without this
    throttle, a stale-and-unreachable source would be retried every tick — the
    behaviour that gets the IP rate-limited/banned in the first place.
    """
    global _last_update_attempt
    if not _tle_stale():
        return
    have_file = _TLE_PATH.exists()
    since = time.monotonic() - _last_update_attempt
    # Always try when we have no file at all; otherwise respect the backoff.
    if have_file and _last_update_attempt and since < _TLE_RETRY_INTERVAL_S:
        return
    _last_update_attempt = time.monotonic()
    try:
        update_tles()
    except Exception as e:
        log.warning("TLE update failed (next retry in ~%dm): %s",
                    _TLE_RETRY_INTERVAL_S // 60, e)


def upcoming_passes(hours_ahead: float = 24.0) -> list[Pass]:
    """Return passes above MIN_ELEVATION within the next hours_ahead hours."""
    _maybe_update_tles()
    if not _TLE_PATH.exists():
        return []

    now = datetime.now(timezone.utc)
    passes: list[Pass] = []

    for sat_name, freq_mhz in _SATELLITES.items():
        try:
            orb = Orbital(sat_name, tle_file=str(_TLE_PATH))
            # pyorbital: get_next_passes(utc_time, length_h, lon, lat, alt_km)
            raw = orb.get_next_passes(now, int(hours_ahead), _LON, _LAT, _ALT)
        except Exception as e:
            log.warning("Pass prediction failed for %s: %s", sat_name, e)
            continue

        # pyorbital returns (rise_time, fall_time, max_elevation_time)
        for aos, los, max_el_time in raw:
            _, el_deg = orb.get_observer_look(max_el_time, _LON, _LAT, _ALT)
            if el_deg < _MIN_ELEVATION:
                continue
            # pyorbital returns naive UTC datetimes
            if aos.tzinfo is None:
                aos = aos.replace(tzinfo=timezone.utc)
            if los.tzinfo is None:
                los = los.replace(tzinfo=timezone.utc)
            passes.append(Pass(
                satellite=sat_name,
                freq_mhz=freq_mhz,
                aos=aos,
                los=los,
                max_el=round(float(el_deg), 1),
            ))

    passes.sort(key=lambda p: p.aos)
    return passes
