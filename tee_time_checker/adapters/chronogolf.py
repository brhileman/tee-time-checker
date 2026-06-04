"""ChronoGolf adapter (`www.chronogolf.com/marketplace/v2/teetimes`).

ChronoGolf is a booking platform used by courses like Broadlands. Their
marketplace API is public and unauthenticated — no Cloudflare challenge,
no API key required.

Endpoint:
  GET https://www.chronogolf.com/marketplace/v2/teetimes
  ?start_date=YYYY-MM-DD
  &course_ids=<uuid>
  &holes=9,18           (comma-separated list of allowed hole counts)
  &nb_players=<n>       (optional: filter server-side by player count)
  &page=1

The response returns all slots for the requested date in a single page;
pagination is not needed for single-day queries.

Slot field meanings:

  - `starts_at`        ISO 8601 UTC datetime — authoritative, tz-unambiguous
  - `date`             "YYYY-MM-DD" in course-local time (informational)
  - `start_time`       "HH:MM" in course-local time (informational)
  - `min_player_size`  minimum party size for this slot
  - `max_player_size`  maximum party size for this slot
  - `course.bookable_holes`  list of hole counts available for this slot

Target params (in courses.toml):
  course_id   ChronoGolf course UUID (from the marketplace API response
              or the booking URL's network traffic)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tee_time_checker.adapters.base import Target
from tee_time_checker.domain import SearchCriteria, TeeTime

_ENDPOINT = "https://www.chronogolf.com/marketplace/v2/teetimes"


class ChronogolfAdapter:
    """Adapter for ChronoGolf-hosted courses."""

    name = "chronogolf"

    def __init__(self, *, timeout: float = 12.0) -> None:
        self._timeout = timeout

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        course_id: str = target.params["course_id"]

        holes_param = "9,18" if criteria.holes == 18 else "9"
        params = {
            "start_date": criteria.date.isoformat(),
            "course_ids": course_id,
            "holes": holes_param,
            "nb_players": criteria.players,
            "page": 1,
        }

        with httpx.Client(timeout=self._timeout) as client:
            r = client.get(_ENDPOINT, params=params)
        r.raise_for_status()

        data = r.json()
        slots: list[dict] = data.get("teetimes", [])
        return [
            tt
            for slot in slots
            if (tt := _parse_slot(slot, target, criteria)) is not None
        ]


def _parse_slot(
    slot: dict[str, Any], target: Target, criteria: SearchCriteria,
) -> TeeTime | None:
    """Map one ChronoGolf slot to a TeeTime, or None to drop it."""
    bookable_holes: list[int] = slot.get("course", {}).get("bookable_holes", [])
    if bookable_holes and criteria.holes not in bookable_holes:
        return None

    min_players: int = slot.get("min_player_size", 1)
    max_players: int = slot.get("max_player_size", 0)
    if max_players <= 0:
        return None
    if not (min_players <= criteria.players <= max_players):
        return None

    # `starts_at` is UTC — convert to course-local tz.
    starts_at: str = slot["starts_at"]
    start_utc = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    start_local = start_utc.astimezone(ZoneInfo(target.timezone))

    return TeeTime(
        course_name=target.name,
        course_slug=target.slug,
        start_time=start_local,
        min_players=min_players,
        max_players=max_players,
        holes=criteria.holes,
        booking_url=target.booking_url,
        raw=slot,
    )
