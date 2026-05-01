"""TeeItUp / Kenna adapter (`phx-api-be-east-1b.kenna.io`).

TeeItUp is GolfNow's white-label booking platform; Kenna is the backend
that powers it. Each course has a tenant alias (e.g. `riverdale`,
`commonground-golf-course`) and an integer facility ID.

One endpoint:
  GET /v2/tee-times?date=YYYY-MM-DD&facilityIds=<id>&returnPromotedRates=true
  Header: x-be-alias: <tenant_alias>

Anonymous (no auth header). Public booking is universal.

Response shape:
  [{ dayInfo: {...}, teetimes: [<slot>, ...] }]

Each slot:
  {
    teetime: ISO-UTC string,        # "2026-05-03T20:42:00.000Z"
    courseId: "<mongo-id-string>",
    backNine: bool,                  # true = starts on the back nine
    bookedPlayers: int,              # currently booked (0..4)
    minPlayers, maxPlayers: int,     # already accounts for bookedPlayers;
                                     # maxPlayers is the *remaining* capacity
    rates: [{
      name: "18 Holes" | "9 Holes",
      holes: 9 | 18,
      allowedPlayers: [int],         # which party sizes this rate accepts
      greenFeeCart: int,             # in CENTS (6100 = $61)
      ...
    }, ...]
  }

Holes filtering: a slot may carry a single rate (9- or 18-only) or both
rates (player picks at booking). Drop the slot if no rate matches the
requested holes; otherwise the slot is fine and we use the matching
rate(s) for pricing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests

from tee_time_checker.adapters.base import Target
from tee_time_checker.domain import SearchCriteria, TeeTime


class TeeItUpAdapter:
    """Adapter for TeeItUp / Kenna-backed booking sites."""

    name = "teeitup"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        params = target.params
        alias: str = params["alias"]
        facility_id: int = params["facility_id"]

        url = (
            "https://phx-api-be-east-1b.kenna.io/v2/tee-times"
            f"?date={criteria.date.isoformat()}"
            f"&facilityIds={facility_id}"
            "&returnPromotedRates=true"
        )
        headers = {
            "accept": "application/json, text/plain, */*",
            "x-be-alias": alias,
            "referer": f"https://{alias}.book.teeitup.com/",
            "origin": f"https://{alias}.book.teeitup.com",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        }

        r = requests.get(url, headers=headers, impersonate="chrome", timeout=self._timeout)
        r.raise_for_status()
        payload = r.json()
        slots = payload[0].get("teetimes", []) if payload else []

        results: list[TeeTime] = []
        for slot in slots:
            tt = _parse_slot(slot, target, criteria)
            if tt is not None:
                results.append(tt)
        return results


def _parse_slot(slot: dict[str, Any], target: Target, criteria: SearchCriteria) -> TeeTime | None:
    """Map one Kenna slot to our TeeTime, or None to drop it.

    Drops the slot when:
    - the requested party size doesn't fit the slot's [min, max] window, or
    - no rate matches the requested round length.
    """
    min_players: int = slot.get("minPlayers", 1)
    max_players: int = slot.get("maxPlayers", 0)

    if max_players <= 0:
        return None
    if not (min_players <= criteria.players <= max_players):
        return None

    matching_rates = [r for r in slot.get("rates", []) if r.get("holes") == criteria.holes]
    if not matching_rates:
        return None

    # `teetime` is UTC ISO ("...Z"). fromisoformat handles the Z suffix
    # natively in Python 3.11+; convert to target timezone for windowing.
    start_utc = datetime.fromisoformat(slot["teetime"])
    start_local = start_utc.astimezone(ZoneInfo(target.timezone))

    # Distinguish back-9 starts in the display name so the user can tell
    # them apart on the booking page (matches our Wellshire / Fox Hollow
    # pattern from MemberSports).
    course_name = target.name
    if slot.get("backNine"):
        course_name = f"{target.name} (back 9)"

    return TeeTime(
        course_name=course_name,
        course_slug=target.slug,
        start_time=start_local,
        min_players=min_players,
        max_players=max_players,
        holes=criteria.holes,
        booking_url=target.booking_url,
        raw=slot,
    )
