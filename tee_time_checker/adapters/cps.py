"""Club Prophet Systems (CPS) adapter.

Used by `*.cps.golf` tenants. Validated against Westminster and Fossil
Trace during recon — see `investigation/cps_summary.txt`.

Two-step protocol:
  1. POST /onlineres/onlineapi/api/v1/onlinereservation/RegisterTransactionId
     with a fresh UUID — establishes the session for the search call.
  2. GET  /onlineres/onlineapi/api/v1/onlinereservation/TeeTimes
     with that same UUID + many query params. Returns tee times for one
     or more `courseIds` on the given date.

Search is fully public — no auth header, no user login. Only the booking
flow (which the user does themselves) requires authentication.

The `x-apikey` header is a per-tenant value embedded in the JS bundle. It
is NOT a secret; it's how the SPA identifies itself to its own backend.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests

from tee_time_checker.adapters.base import Adapter, Target
from tee_time_checker.domain import SearchCriteria, TeeTime

# Platforms vary in fixed values; CPS uses these consistently across tenants.
_DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "client-id": "onlineresweb",
    "x-ismobile": "false",
    "x-terminalid": "3",
    "x-productid": "1",
    "x-moduleid": "7",
    "x-componentid": "1",
}


class CPSAdapter:
    """Adapter for Club Prophet Systems booking sites (`*.cps.golf`)."""

    name = "cps"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        params = target.params
        tenant: str = params["tenant"]                  # e.g. "cityofwestminster"
        base = f"https://{tenant}.cps.golf"
        api_root = f"{base}/onlineres/onlineapi/api/v1/onlinereservation"

        headers = {
            **_DEFAULT_HEADERS,
            "x-apikey": params["api_key"],
            "x-websiteid": params["website_id"],
            "x-siteid": str(params["site_id"]),
            "x-timezoneid": target.timezone,
            "x-timezone-offset": str(_tz_offset_minutes(target.timezone, criteria.date)),
            "referer": f"{base}/onlineresweb/search-teetime",
            "origin": base,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        }

        # Step 1: register a transaction (single-use session token).
        txn = str(uuid.uuid4())
        r = requests.post(
            f"{api_root}/RegisterTransactionId",
            json={"transactionId": txn},
            headers=headers,
            impersonate="chrome",
            timeout=self._timeout,
        )
        r.raise_for_status()

        # Step 2: search. CPS expects the date as an English weekday string,
        # e.g. "Sun May 03 2026" — exactly what JS's `Date.toDateString()` emits.
        course_ids: list[int] = params["course_ids"]
        query = {
            "searchDate": _format_search_date(criteria.date),
            "holes": str(criteria.holes if criteria.holes in (9, 18) else 0),
            # `numberOfPlayer=0` returns all slots regardless of party size; we
            # filter ourselves so a slot with capacity >=N still counts.
            "numberOfPlayer": "0",
            "courseIds": ",".join(str(c) for c in course_ids),
            "searchTimeType": "0",
            "transactionId": txn,
            "teeOffTimeMin": "0",   # full-day fetch; window filter happens centrally
            "teeOffTimeMax": "23",
            "isChangeTeeOffTime": "true",
            "teeSheetSearchView": "5",
            "classCode": params.get("class_code", "R"),
            "defaultOnlineRate": "N",
            "isUseCapacityPricing": "false",
            "memberStoreId": str(params.get("member_store_id", 1)),
            "searchType": "1",
        }
        r = requests.get(
            f"{api_root}/TeeTimes",
            params=query,
            headers=headers,
            impersonate="chrome",
            timeout=self._timeout,
        )
        r.raise_for_status()
        payload = r.json()
        if not payload.get("isSuccess"):
            return []

        return [
            tt
            for slot in payload.get("content", [])
            if (tt := _parse_slot(slot, target, criteria)) is not None
        ]


def _parse_slot(slot: dict[str, Any], target: Target, criteria: SearchCriteria) -> TeeTime | None:
    """Convert a CPS slot dict into our normalized TeeTime, or None to drop it.

    Drops slots that:
    - are already booked (`bookingList` is non-empty),
    - don't have enough remaining capacity for the requested party size.
    """
    if slot.get("bookingList"):
        return None

    capacity: int = slot.get("participants", 0)
    if capacity < criteria.players:
        return None

    # CPS returns "2026-05-03T16:20:00" — naive ISO. Tag it with the
    # course's timezone so window filtering compares like-for-like.
    start_local = datetime.fromisoformat(slot["startTime"]).replace(
        tzinfo=ZoneInfo(target.timezone)
    )

    rate = slot.get("defaultBookingRate") or {}
    price_min = rate.get("greenFeeWalking") or rate.get("baseRate")
    price_max = rate.get("greenFeeRiding") or price_min

    return TeeTime(
        course_name=slot.get("courseName") or target.name,
        course_slug=target.slug,
        start_time=start_local,
        max_players=capacity,
        holes=slot.get("holes", criteria.holes),
        booking_url=target.booking_url,
        price_min=_as_float(price_min),
        price_max=_as_float(price_max),
        raw=slot,
    )


def _format_search_date(d) -> str:
    """CPS expects e.g. 'Sun May 03 2026' — equivalent to JS Date.toDateString().

    Note `%d` gives zero-padded day, which matches what the SPA sends.
    """
    return d.strftime("%a %b %d %Y")


def _tz_offset_minutes(tz_name: str, on_date) -> int:
    """Minutes that local time is *behind* UTC (positive for the Americas).

    Matches the `x-timezone-offset` value the SPA sends, which is itself
    derived from JS's `Date.getTimezoneOffset()` (positive = behind UTC).
    """
    sample = datetime(on_date.year, on_date.month, on_date.day, 12, 0, tzinfo=ZoneInfo(tz_name))
    return -int(sample.utcoffset().total_seconds() // 60)


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
