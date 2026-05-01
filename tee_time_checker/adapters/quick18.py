"""Quick18 adapter (`*.quick18.com`).

Different shape from the JSON adapters: Quick18 server-renders an HTML
booking matrix in response to a form POST. Slot info is encoded into
each row's booking-link URL:

  /teetimes/course/<courseId>/teetime/<YYYYMMDDHHMM>?psid=<rateId>&p=<players>

Extracting via regex over the response HTML is reliable — the URL shape
is part of Quick18's stable booking API. We don't parse the rendered
table itself.

Filters are server-side rather than client-side:

- Holes: sub-courses get distinct CourseIds. For Thorncreek:
    693 = main 18-hole course
    694 = Thorncreek 9 Holes - Back 9
    705 = Thorncreek 9 Holes - Front 9
  Plus virtual aggregate IDs:
    -9  = all 9-hole sub-courses at this Quick18 site
    -18 = all 18-hole courses
  Adapter sends `course_id` from target params for 18-hole searches and
  `-9` for 9-hole searches; the response then naturally only contains
  slots of the requested length.

- Players: posting `Players=N` returns only slots that accept exactly
  that party size. Each booking link's `p=N` echoes that. Posting `0`
  (any) returns every slot but with `p=0` in every link, losing the
  per-slot party-size detail. For our use case (find slots that fit the
  user's request) the server-filtered approach is cheaper and accurate;
  we just don't know the slot's full min/max range, so we report
  `min = max = criteria.players` in the result.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from curl_cffi import requests

from tee_time_checker.adapters.base import Target
from tee_time_checker.domain import SearchCriteria, TeeTime

_SLOT_RE = re.compile(
    r"/teetimes/course/(\d+)/teetime/(\d{12})\?psid=\d+(?:&amp;|&)p=(\d+)",
)

_DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "accept-language": "en-US,en;q=0.9",
}


class Quick18Adapter:
    """Adapter for Quick18 booking sites."""

    name = "quick18"

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        params = target.params
        host: str = params["host"]                     # e.g. "thorncreek.quick18.com"
        course_id_18: int = params["course_id"]        # 18-hole CourseId
        nine_hole_suffixes: dict[int, str] = {
            int(k): v for k, v in params.get("nine_hole_suffixes", {}).items()
        }

        # 18-hole search: post the specific course; 9-hole: post the
        # aggregate "-9" virtual course id which catches every 9-hole
        # sub-course on this Quick18 tenant.
        course_id_param = course_id_18 if criteria.holes == 18 else -9

        endpoint = f"https://{host}/teetimes/searchmatrix"
        body = {
            "SearchForm.CourseId": str(course_id_param),
            # Quick18 expects M/D/YYYY (no zero-padding), the same format
            # the page's date input shows by default.
            "SearchForm.Date": f"{criteria.date.month}/{criteria.date.day}/{criteria.date.year}",
            "SearchForm.Players": str(criteria.players),
            # Filter window centrally so all platforms agree on what
            # "afternoon" means.
            "SearchForm.TimeOfDay": "Any",
        }
        r = requests.post(
            endpoint,
            data=body,
            headers={
                **_DEFAULT_HEADERS,
                "origin": f"https://{host}",
                "referer": f"{endpoint}",
            },
            impersonate="chrome",
            timeout=self._timeout,
        )
        r.raise_for_status()
        html = r.text

        # Each (course_id, datetime) may appear multiple times in the
        # response — once per `psid` (rate variant) — but they're all the
        # same physical slot. Dedupe to one TeeTime per (course, time).
        seen: set[tuple[int, str]] = set()
        results: list[TeeTime] = []
        for match in _SLOT_RE.finditer(html):
            cid = int(match.group(1))
            dt_str = match.group(2)
            key = (cid, dt_str)
            if key in seen:
                continue
            seen.add(key)

            results.append(_build_tee_time(
                cid=cid,
                dt_str=dt_str,
                target=target,
                criteria=criteria,
                nine_hole_suffixes=nine_hole_suffixes,
            ))

        return results


def _build_tee_time(
    *,
    cid: int,
    dt_str: str,
    target: Target,
    criteria: SearchCriteria,
    nine_hole_suffixes: dict[int, str],
) -> TeeTime:
    """Construct a TeeTime from a parsed booking link."""
    start_local = datetime.strptime(dt_str, "%Y%m%d%H%M").replace(
        tzinfo=ZoneInfo(target.timezone)
    )

    # Tag 9-hole sub-courses with a suffix so a single Quick18 facility
    # can surface multiple distinguishable entries (front 9 vs back 9).
    name = target.name + nine_hole_suffixes.get(cid, "")

    return TeeTime(
        course_name=name,
        course_slug=target.slug,
        start_time=start_local,
        # Server filtered by exact party size — we don't know the slot's
        # full min/max range, just that it accepts what we asked for.
        min_players=criteria.players,
        max_players=criteria.players,
        holes=criteria.holes,
        booking_url=target.booking_url,
        raw={"course_id": cid, "datetime_str": dt_str},
    )
