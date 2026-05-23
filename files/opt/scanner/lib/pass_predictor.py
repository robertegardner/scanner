"""NOAA satellite pass predictor using pyorbital and Celestrak TLEs."""
import logging
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

_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
_TLE_PATH = Path("/var/lib/scanner/noaa/weather.tle")
_MIN_ELEVATION = 20.0   # degrees — passes below this are too weak to bother
_TLE_MAX_AGE_S = 86400  # refresh TLEs once per day


@dataclass
class Pass:
    satellite: str
    freq_mhz: float
    aos: datetime   # acquisition of signal, timezone-aware UTC
    los: datetime   # loss of signal, timezone-aware UTC
    max_el: float   # degrees


def update_tles() -> None:
    """Download fresh TLEs from Celestrak. Atomic write."""
    _TLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(_TLE_URL, timeout=30)
    resp.raise_for_status()
    tmp = _TLE_PATH.with_suffix(".tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(_TLE_PATH)
    log.info("TLEs updated: %s (%d bytes)", _TLE_PATH, _TLE_PATH.stat().st_size)


def _tle_stale() -> bool:
    if not _TLE_PATH.exists():
        return True
    mtime = datetime.fromtimestamp(_TLE_PATH.stat().st_mtime, tz=timezone.utc)
    age_s = (datetime.now(timezone.utc) - mtime).total_seconds()
    return age_s > _TLE_MAX_AGE_S


def upcoming_passes(hours_ahead: float = 24.0) -> list[Pass]:
    """Return passes above MIN_ELEVATION within the next hours_ahead hours."""
    if _tle_stale():
        try:
            update_tles()
        except Exception as e:
            log.warning("TLE update failed: %s", e)
            if not _TLE_PATH.exists():
                return []

    now = datetime.now(timezone.utc)
    passes: list[Pass] = []

    for sat_name, freq_mhz in _SATELLITES.items():
        try:
            orb = Orbital(sat_name, tle_file=str(_TLE_PATH))
            # pyorbital: get_next_passes(utc_time, length_h, lon, lat, alt_km)
            raw = orb.get_next_passes(now, hours_ahead, _LON, _LAT, _ALT)
        except Exception as e:
            log.warning("Pass prediction failed for %s: %s", sat_name, e)
            continue

        for aos, los, max_el in raw:
            if max_el < _MIN_ELEVATION:
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
                max_el=round(max_el, 1),
            ))

    passes.sort(key=lambda p: p.aos)
    return passes
