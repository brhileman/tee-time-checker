"""Noteefy adapter (`booking-engine.noteefy.app`).

Noteefy is a meta-platform: it scrapes underlying booking systems
(ChronoGolf, etc.) and exposes their availability through a single API.
For courses like Broadlands that publish through Noteefy, we get a
clean unified shape.

One endpoint:
  POST https://booking-engine.noteefy.app/tee-times/availability
  body: {
    course_id, min_players, max_players, holes, dates,
    exclude_held_tee_times, player_type_ids, waitlist_course_id
  }

Cloudflare quirk: the `booking-engine.noteefy.app` host is fronted by
Cloudflare with TLS-fingerprint detection. Stock httpx/requests get a
403 "Just a moment..." JS challenge. We use `curl_cffi` everywhere
already, but need to send the full Chrome-style header set including
`sec-ch-ua` and a realistic `referer` for CF to wave us through.

Slot field meanings:

  - `time`         "HH:MM" string in course-local time
  - `date`         "M/D/YYYY"
  - `timestamp`    epoch seconds — preferred for unambiguous tz handling
  - `min_player`,
    `max_player`   party size constraint (already accounts for bookings)
  - `is_9_holes`,
    `is_18_holes`  true = this slot allows that round length. May be
                   true for both (player picks at booking).
  - `booking_url`  per-slot deep link to the underlying booking system
                   (e.g. ChronoGolf's date-specific page) — more useful
                   than the target's generic URL.

`player_type_ids` is a course-specific list of UUIDs Noteefy uses to
filter rate options. Get them by visiting Broadlands' booking page in a
browser, opening the network tab, and copying the values from any
`/tee-times/availability` POST. They're stable per-course.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests

from tee_time_checker.adapters.base import Target
from tee_time_checker.domain import SearchCriteria, TeeTime

_ENDPOINT = "https://booking-engine.noteefy.app/tee-times/availability"

# Cloudflare-friendly headers. The exact `sec-ch-ua` + a non-trivial
# `referer` matter — bare httpx-style headers get a 403 JS challenge.
_DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://booking.noteefy.app",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua-platform": '"macOS"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua": (
        '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"'
    ),
}


class NoteefyAdapter:
    """Adapter for Noteefy-fronted courses."""

    name = "noteefy"

    def __init__(self, *, timeout: float = 12.0) -> None:
        self._timeout = timeout

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        params = target.params
        course_id: str = params["course_id"]
        public_id: str | None = params.get("public_id")
        player_type_ids: list[str] = params["player_type_ids"]

        # Send a wide players range and filter ourselves — the response
        # always carries each slot's actual min/max so client-side filter
        # is straightforward, and a wide request avoids the chance of
        # over-narrow server filtering hiding usable slots.
        body = {
            "course_id": course_id,
            "min_players": 1,
            "max_players": 4,
            "holes": "18",
            "dates": [criteria.date.isoformat()],
            "exclude_held_tee_times": True,
            "player_type_ids": player_type_ids,
            "waitlist_course_id": course_id,
        }

        # CF likes a referer pointing at the public booking page; if the
        # target supplies its public_id we use the deeper URL, otherwise
        # the bare booking host is enough.
        referer = (
            f"https://booking.noteefy.app/e/{public_id}"
            if public_id
            else "https://booking.noteefy.app/"
        )
        headers = {**_DEFAULT_HEADERS, "referer": referer}

        r = requests.post(
            _ENDPOINT,
            json=body,
            headers=headers,
            impersonate="chrome",
            timeout=self._timeout,
        )
        r.raise_for_status()

        # Defensive: the Cloudflare challenge page returns 200/403 with
        # text/html; only proceed if we have a real JSON body.
        if "json" not in (r.headers.get("content-type") or "").lower():
            raise RuntimeError(
                "Noteefy returned non-JSON; likely Cloudflare JS challenge fired"
            )

        slots = r.json().get("tee_times", [])
        return [
            tt
            for slot in slots
            if (tt := _parse_slot(slot, target, criteria)) is not None
        ]


def _parse_slot(
    slot: dict[str, Any], target: Target, criteria: SearchCriteria,
) -> TeeTime | None:
    """Map one Noteefy slot to a TeeTime, or None to drop it."""
    # Holes filter via boolean flags. Default True so missing fields
    # don't accidentally drop everything.
    has_18 = slot.get("is_18_holes", True)
    has_9 = slot.get("is_9_holes", True)
    if criteria.holes == 18 and not has_18:
        return None
    if criteria.holes == 9 and not has_9:
        return None

    min_players = slot.get("min_player", 1)
    max_players = slot.get("max_player", 0)
    if max_players <= 0:
        return None
    if not (min_players <= criteria.players <= max_players):
        return None

    # `timestamp` is epoch seconds in UTC — convert to course-local tz.
    # If absent, fall back to date + time strings.
    ts = slot.get("timestamp")
    if ts:
        start_local = datetime.fromtimestamp(ts, tz=ZoneInfo(target.timezone))
    else:
        # Format: date "M/D/YYYY" + time "HH:MM"
        start_local = datetime.strptime(
            f"{slot['date']} {slot['time']}", "%m/%d/%Y %H:%M"
        ).replace(tzinfo=ZoneInfo(target.timezone))

    # Slot's booking_url is a deep link to the underlying provider
    # (e.g. ChronoGolf with the right date) — more useful than the
    # target's generic URL.
    booking_url = slot.get("booking_url") or target.booking_url

    return TeeTime(
        course_name=target.name,
        course_slug=target.slug,
        start_time=start_local,
        min_players=min_players,
        max_players=max_players,
        holes=criteria.holes,
        booking_url=booking_url,
        raw=slot,
    )
