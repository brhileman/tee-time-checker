"""Zipcode → lat/lng lookup and drive-time estimation for Denver metro.

Drive time model: 40 mph average (accounts for highways + lights).
  20 miles = 30 minutes, 40 miles = 60 minutes, etc.

Only Colorado zips are bundled (co_zips.json, ~23KB). Out-of-state zips
return None from zip_coords().
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

_ZIPS_PATH = Path(__file__).parent / "co_zips.json"
_AVG_SPEED_MPH = 40.0  # Denver metro average including highways + lights


@lru_cache(maxsize=1)
def _load_zips() -> dict[str, tuple[float, float]]:
    data = json.loads(_ZIPS_PATH.read_text())
    return {code: (lat, lon) for code, (lat, lon) in data.items()}


def zip_coords(zipcode: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a Colorado zip, or None if unknown."""
    return _load_zips().get(zipcode.strip())


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lng points."""
    r = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def drive_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Estimated drive time in minutes using the Denver metro speed model."""
    miles = haversine_miles(lat1, lon1, lat2, lon2)
    return round(miles / _AVG_SPEED_MPH * 60)
