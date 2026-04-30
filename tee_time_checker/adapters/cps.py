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
    - have a per-slot party-size policy that excludes the requested size, or
    - don't support the requested round length (9 vs 18 holes).

    Important: `participants` (always 4) is hardware capacity and useless
    for filtering. The real constraint is the `minPlayer`/`maxPlayer` pair,
    which mirrors the "1 - 2 GOLFERS" style label on the booking page.

    For the hole count: `slot["holes"]` is the parent course total, not
    the slot's bookable length. The reliable signals are the booleans
    `isContain18HoleItems` / `isContain9HoleItems`. For Westminster every
    slot has both flags true (player chooses at booking), but other CPS
    tenants may have 9-only or 18-only slots and we shouldn't leak them.
    """
    if slot.get("bookingList"):
        return None

    min_players: int = slot.get("minPlayer", 1)
    max_players: int = slot.get("maxPlayer", slot.get("participants", 4))

    if not (min_players <= criteria.players <= max_players):
        return None

    # Default both to True if missing — keep the slot if we can't tell;
    # better to surface a slightly-wrong slot than drop a real match.
    has_18 = slot.get("isContain18HoleItems", True)
    has_9 = slot.get("isContain9HoleItems", True)
    if criteria.holes == 18 and not has_18:
        return None
    if criteria.holes == 9 and not has_9:
        return None

    # When a slot supports both, the user picks at booking time so we
    # report whatever they asked for; pinned slots use their actual length.
    if has_18 and has_9:
        slot_holes = criteria.holes
    elif has_18:
        slot_holes = 18
    elif has_9:
        slot_holes = 9
    else:
        slot_holes = criteria.holes

    # CPS returns "2026-05-03T16:20:00" — naive ISO. Tag it with the
    # course's timezone so window filtering compares like-for-like.
    start_local = datetime.fromisoformat(slot["startTime"]).replace(
        tzinfo=ZoneInfo(target.timezone)
    )

    # Best-effort price extraction: shItemPrices contains line items keyed
    # by shItemCode (GreenFee18, GreenFee9, HalfCart18, etc.). For our
    # summary we want the green fee for the user's holes choice; carts and
    # other add-ons aren't included.
    price_min, price_max = _extract_prices(slot.get("shItemPrices") or [], slot_holes)

    return TeeTime(
        course_name=slot.get("courseName") or target.name,
        course_slug=target.slug,
        start_time=start_local,
        min_players=min_players,
        max_players=max_players,
        holes=slot_holes,
        booking_url=target.booking_url,
        price_min=price_min,
        price_max=price_max,
        raw=slot,
    )


def _extract_prices(items: list[dict[str, Any]], holes: int) -> tuple[float | None, float | None]:
    """Pull the green-fee for the requested holes from CPS rate line items.

    Returns (min, max). For now they're equal — CPS exposes one green-fee
    rate per slot per duration. We keep the tuple shape for future
    multi-rate platforms (twilight vs prime, member vs guest, etc.).
    """
    code = "GreenFee9" if holes == 9 else "GreenFee18"
    for item in items:
        if item.get("shItemCode") == code:
            price = _as_float(item.get("displayPrice") or item.get("price"))
            return price, price
    return None, None


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
