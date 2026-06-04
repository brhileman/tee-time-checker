"""Core domain types shared across adapters, search, and CLI.

Everything that crosses an adapter boundary is one of these types. Keep
them small, plain, and free of any platform-specific concepts — those
belong inside the adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any


class TimeWindow(StrEnum):
    """Coarse time-of-day buckets users speak in.

    Bounds are intentionally generous on the late side — courses close at
    different times, and we'd rather offer too much than miss a slot.
    """

    MORNING = "morning"      # open  -> 10:00
    MIDDAY = "midday"        # 10:00 -> 14:00
    AFTERNOON = "afternoon"  # 14:00 -> close
    ANY = "any"              # full day

    def bounds(self) -> tuple[int, int]:
        """Return (start_hour, end_hour) inclusive-exclusive for this window.

        Times are local-to-the-course; the search filter applies them after
        normalizing each adapter's response to the course's timezone.
        """
        match self:
            case TimeWindow.MORNING:
                return (0, 10)
            case TimeWindow.MIDDAY:
                return (10, 14)
            case TimeWindow.AFTERNOON:
                return (14, 23)
            case TimeWindow.ANY:
                return (0, 23)

    def contains(self, dt: datetime, time_min: time | None = None, time_max: time | None = None) -> bool:
        if time_min is not None or time_max is not None:
            slot_time = dt.time().replace(second=0, microsecond=0)
            if time_min is not None and slot_time < time_min:
                return False
            if time_max is not None and slot_time > time_max:
                return False
            return True
        start, end = self.bounds()
        return start <= dt.hour < end


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """What the user is asking for.

    Built either from CLI args or from the natural-language parser. Adapters
    receive this plus their own per-target config and return matching slots.
    """

    date: date
    players: int  # number of players the user wants to book for (>=1)
    window: TimeWindow = TimeWindow.ANY
    holes: int = 18  # 9 or 18; most adapters honor this where supported
    course_filter: list[str] | None = None  # restrict to these target slugs
    target_time: str | None = None  # "HH:MM" hint when user said "around 4:30"
    time_min: time | None = None  # explicit lower bound, e.g. 10:00 from "10am-3pm"
    time_max: time | None = None  # explicit upper bound, e.g. 15:00 from "10am-3pm"


@dataclass(frozen=True, slots=True)
class TeeTime:
    """A single available slot, normalized across platforms.

    `start_time` is timezone-aware (set to the course's local timezone). The
    search layer compares against `criteria.window` using local hours so
    "afternoon" means afternoon at *that course*, not the user's machine.
    """

    course_name: str
    course_slug: str       # which configured target produced this
    start_time: datetime   # tz-aware, in course-local time
    min_players: int       # minimum party size the slot will accept
    max_players: int       # maximum party size (min and max can equal — single-player only)
    holes: int
    booking_url: str | None = None      # deep-link the user can tap to book
    raw: dict[str, Any] = field(default_factory=dict, repr=False)  # debugging only
