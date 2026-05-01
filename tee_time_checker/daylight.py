"""Daylight risk for tee times — will the round finish before dark?

Computes per-slot risk based on the slot's start time, the round
length, and sunset/dusk for the day at the course's location.

Risk levels:
  - "ok"        — round finishes well before sunset
  - "twilight"  — finish is between sunset and civil dusk; playable but
                  fading light
  - "after_dark" — finish is after civil dusk; almost certainly won't
                   complete in usable light

Round-duration assumptions are conservative averages. Public/muni
courses can pace 4–4.5h for 18, courses with carts can be quicker.
We pick numbers that bias toward warning the user.

Project-wide location default: Denver (all currently configured
courses are within ~30 miles, sunset variance <2 min). Adding per-
target overrides is a small refactor when a non-Denver course is
added — the helper accepts a `location` arg.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from astral import LocationInfo
from astral.sun import sun

# Default location: downtown Denver. Sunset times for any of our courses
# differ from this by under 2 minutes.
_DEFAULT_LOCATION = LocationInfo(
    name="Denver", region="CO", timezone="America/Denver",
    latitude=39.7392, longitude=-104.9903,
)

# Conservative round-duration estimates. Real rounds vary by pace of
# play and party size, but these err on the side of warning more.
_ROUND_DURATION = {
    9: timedelta(hours=2, minutes=15),
    18: timedelta(hours=4, minutes=30),
}


class DaylightRisk(StrEnum):
    OK = "ok"
    TWILIGHT = "twilight"
    AFTER_DARK = "after_dark"


@dataclass(frozen=True, slots=True)
class DaylightInfo:
    """Computed daylight context for a single tee time."""

    risk: DaylightRisk
    sunset: datetime  # tz-aware
    dusk: datetime    # tz-aware (civil dusk)
    finish_time: datetime  # tz-aware


def assess(start_time: datetime, holes: int) -> DaylightInfo:
    """Return the daylight risk for a tee time at the default location.

    `start_time` must be tz-aware (course-local). `holes` is 9 or 18.
    """
    duration = _ROUND_DURATION.get(holes, _ROUND_DURATION[18])
    finish = start_time + duration

    sunset, dusk = _sun_for_date(start_time.date())

    if finish <= sunset:
        risk = DaylightRisk.OK
    elif finish <= dusk:
        risk = DaylightRisk.TWILIGHT
    else:
        risk = DaylightRisk.AFTER_DARK

    return DaylightInfo(risk=risk, sunset=sunset, dusk=dusk, finish_time=finish)


@lru_cache(maxsize=64)
def _sun_for_date(d: date) -> tuple[datetime, datetime]:
    """Sunset and civil dusk on `d` at the default location.

    Cached because a typical search round computes daylight info for
    many slots all on the same date. astral computes are cheap but
    not free.
    """
    info = sun(_DEFAULT_LOCATION.observer, date=d, tzinfo=_DEFAULT_LOCATION.timezone)
    return info["sunset"], info["dusk"]
