"""MemberSports adapter (`api.membersports.com`).

One endpoint:
  POST /api/v1/golfclubs/onlineBookingTeeTimes
  body: {configurationTypeId, date, golfClubGroupId, golfClubId,
         golfCourseId, groupSheetTypeId}

The response is **group-wide** — even though the body specifies a
`golfCourseId`, the response includes items for every course in the
golfClubGroup. Each item carries its own `golfClubId`/`golfCourseId`,
so we filter to the wanted course after the call.

That makes it efficient to model each course as its own target: the
adapter caches the per-(group, date) response for the duration of a
single search round, so three Denver-group courses only cost one API
call across all three.

Field meanings (learned the hard way):

  - `availableCount` — always 0; useless. Don't trust it.
  - `playerCount`    — number of golfers already booked at this slot.
                       The actual remaining capacity is
                       `slot_capacity - playerCount` where slot_capacity
                       defaults to 4 (or 5 if the course allows fivesomes,
                       configurable via `slot_capacity` in target params).
  - `bookingNotAllowed` + `bookingNotAllowedReason` — gates the slot. We
    drop slots where this is True. Reason often = "7 day standard booking
    window exceeded" for dates beyond the booking horizon.
  - `hide` — UI hides this item; we drop it.
  - `minimumNumberOfPlayers` — lower bound for the party size.
  - `allowSinglesToBookOnline` — auxiliary flag; we honor minimum directly.

Auth: anonymous. The literal string "Bearer null" is what the SPA sends,
and the server accepts it.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests

from tee_time_checker.adapters.base import Target
from tee_time_checker.domain import SearchCriteria, TeeTime

_API_KEY = "A9814038-9E19-4683-B171-5A06B39147FC"  # public, embedded in the JS bundle
_ENDPOINT = "https://api.membersports.com/api/v1/golfclubs/onlineBookingTeeTimes"

_DEFAULT_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json; charset=UTF-8",
    "authorization": "Bearer null",
    "x-api-key": _API_KEY,
    "origin": "https://app.membersports.com",
    "referer": "https://app.membersports.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
}


class MemberSportsAdapter:
    """Adapter for the MemberSports booking platform."""

    name = "membersports"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout
        # Cache scope: this adapter instance. The orchestrator builds a
        # fresh registry per search round, so the cache is naturally per-round.
        self._response_cache: dict[tuple[int, str], list[dict[str, Any]]] = {}

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        params = target.params
        group_id: int = params["group_id"]
        wanted_course_id: int = params["course_id"]
        slot_capacity: int = params.get("slot_capacity", 4)

        slots = self._fetch_group_day(
            group_id=group_id,
            club_id=params["club_id"],
            course_id=wanted_course_id,
            on_date=criteria.date,
        )

        results: list[TeeTime] = []
        for slot in slots:
            tee_minutes: int = slot.get("teeTime", 0)
            for item in slot.get("items", []):
                if item.get("hide") or item.get("bookingNotAllowed"):
                    continue
                if item.get("golfCourseId") != wanted_course_id:
                    continue

                tt = _build_tee_time(
                    item=item,
                    tee_minutes=tee_minutes,
                    target=target,
                    criteria=criteria,
                    slot_capacity=slot_capacity,
                )
                if tt is not None:
                    results.append(tt)
        return results

    def _fetch_group_day(
        self,
        *,
        group_id: int,
        club_id: int,
        course_id: int,
        on_date: date_cls,
    ) -> list[dict[str, Any]]:
        """Fetch the group-wide tee sheet for one day, cached per (group, date).

        The API response covers the whole group regardless of which
        course_id we send, so callers for sibling courses share results.
        """
        date_str = on_date.isoformat()
        key = (group_id, date_str)
        cached = self._response_cache.get(key)
        if cached is not None:
            return cached

        body = {
            "configurationTypeId": 0,
            "date": date_str,
            "golfClubGroupId": group_id,
            "golfClubId": club_id,
            "golfCourseId": course_id,
            "groupSheetTypeId": 0,
        }
        r = requests.post(
            _ENDPOINT,
            json=body,
            headers=_DEFAULT_HEADERS,
            impersonate="chrome",
            timeout=self._timeout,
        )
        r.raise_for_status()
        slots = r.json()
        self._response_cache[key] = slots
        return slots


def _build_tee_time(
    *,
    item: dict[str, Any],
    tee_minutes: int,
    target: Target,
    criteria: SearchCriteria,
    slot_capacity: int,
) -> TeeTime | None:
    """Map one MemberSports item into our normalized TeeTime.

    Drops the slot if the requested party size doesn't fit the
    [minimumNumberOfPlayers, slot_capacity-playerCount] range.
    """
    booked: int = item.get("playerCount", 0)
    remaining = max(0, slot_capacity - booked)
    if remaining <= 0:
        return None

    min_players = max(1, item.get("minimumNumberOfPlayers", 1))
    max_players = remaining

    if not (min_players <= criteria.players <= max_players):
        return None

    # Filter by hole count *per item*, not per course. Each item carries a
    # `holesRequirementTypeId`: 0=either, 1=9-only, 2=18-only, null=unknown.
    # The course-level `golfCourseNumberOfHoles` describes the total holes
    # (27 for Fox Hollow), which is misleading — a 27-hole course offers
    # both 9- and 18-hole rounds as separate items, distinguished by this
    # field, not by the parent total.
    holes_req = item.get("holesRequirementTypeId")
    if holes_req == 1 and criteria.holes != 9:
        return None
    if holes_req == 2 and criteria.holes != 18:
        return None

    if holes_req == 1:
        slot_holes = 9
    elif holes_req == 2:
        slot_holes = 18
    else:
        # 0 (either) or null (regular course): pass through whatever the
        # user asked for, since they get to pick at booking time.
        slot_holes = criteria.holes

    start_local = _minutes_to_local_dt(tee_minutes, criteria.date, target.timezone)

    return TeeTime(
        course_name=item.get("name") or target.name,
        course_slug=target.slug,
        start_time=start_local,
        min_players=min_players,
        max_players=max_players,
        holes=slot_holes,
        booking_url=target.booking_url,
        raw=item,
    )


def _minutes_to_local_dt(minutes: int, on_date: date_cls, tz_name: str) -> datetime:
    """Convert MemberSports' minutes-since-midnight to a tz-aware datetime.

    Uses `timedelta` rather than `time(h, m)` so values that overflow into
    the next day (rare but possible at 24:00 boundaries) don't crash.
    """
    midnight = datetime.combine(on_date, time(0, 0), tzinfo=ZoneInfo(tz_name))
    return midnight + timedelta(minutes=minutes)
