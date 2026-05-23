"""NOAA satellite pass predictor. Stub — implemented in Stage 4."""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Pass:
    satellite: str
    freq_mhz: float
    aos: datetime      # acquisition of signal
    los: datetime      # loss of signal
    max_el: float      # degrees


def upcoming_passes(hours_ahead: float = 24.0) -> list[Pass]:
    """Return predicted passes within the next `hours_ahead` hours."""
    return []
