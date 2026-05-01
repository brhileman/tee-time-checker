"""SMS-style summary formatter for a SearchResult.

Designed to fit within Twilio's per-segment character budget (160 chars
GSM-7 / 70 chars UCS-2; using emoji forces UCS-2). Real-world target:
keep summaries under ~320 chars (2 segments) when 1–3 courses match,
gracefully degrade to "top courses + count" form for larger results.

Output shape:

    ✅ Sun 5/3 afternoon, 2p (18h)
    • Legacy Ridge — 9 slots, 4:20p–6:00p · 7 may finish after dark ⚠️
    • Walnut Creek — 8 slots, 2:30p–5:50p · 5 ⚠️
    Tap to book:
      cityofwestminster.cps.golf/onlineresweb/search-teetime

Or for the no-match case:

    ❌ No tee times: Sun 5/3 afternoon, 2p (18h)
    Searched: Westminster, Riverdale, Wellshire, ...
    Reply WATCH to keep checking, or CHANGE to adjust.

Daylight risk is computed per slot — see daylight.py — and aggregated
into per-course counts. We don't drop "after dark" slots; the user
might have lights on a sim or want a twilight rate.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from tee_time_checker.daylight import DaylightRisk, assess
from tee_time_checker.domain import SearchCriteria, TeeTime, TimeWindow

if TYPE_CHECKING:
    from tee_time_checker.search import SearchResult


def format_sms_summary(result: "SearchResult") -> str:
    """Render a SearchResult as an SMS-friendly string."""
    if not result.tee_times:
        return _format_no_match(result)
    return _format_match(result)


# ──────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────

_WINDOW_LABEL = {
    TimeWindow.MORNING: "morning",
    TimeWindow.MIDDAY: "midday",
    TimeWindow.AFTERNOON: "afternoon",
    TimeWindow.ANY: "any time",
}


def _format_match(result: "SearchResult") -> str:
    """Compose the match-found summary.

    Length-sensitive: 1 SMS segment is 67 chars (UCS-2 multi-part) and
    each segment costs the user another billable message. We cap the
    output to the top courses by slot count and only attach URLs to a
    couple of them, falling back to a count of unlisted courses.
    """
    # Group slots by course display name.
    groups: dict[str, list[TeeTime]] = {}
    for tt in result.tee_times:
        groups.setdefault(tt.course_name, []).append(tt)
    for slots in groups.values():
        slots.sort(key=lambda t: t.start_time)

    # Sort courses by slot count desc — best options first.
    by_count = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    header = _header_line(result.criteria)
    course_lines: list[str] = []
    listed_urls: list[str] = []

    # Build all course lines, then pick how many to include based on length.
    for name, slots in by_count:
        course_lines.append((name, _course_line(name, slots, result.criteria.holes), slots))

    # Greedy: keep adding courses while the running total stays inside
    # the budget (UCS-2 multi-part SMS = 67 chars/segment; aim for ≤3).
    # Always include at least the top 2 courses even if it pushes us
    # one segment longer.
    LENGTH_BUDGET = 230
    MIN_COURSES = 2
    out_lines = [header]
    included = 0
    for name, line, slots in course_lines:
        candidate = "\n".join(out_lines + [line])
        if included < MIN_COURSES or len(candidate) <= LENGTH_BUDGET:
            out_lines.append(line)
            included += 1
            url = next((s.booking_url for s in slots if s.booking_url), None)
            if url and url not in listed_urls:
                listed_urls.append(url)
        else:
            break

    remaining = len(course_lines) - included
    if remaining > 0:
        out_lines.append(f"…+{remaining} more course{'s' if remaining != 1 else ''} with slots")

    # Show only the first booking URL — adding more pushes us into a
    # third segment quickly and the user can re-search if they want
    # other courses' links.
    if listed_urls:
        out_lines.append(f"Book: {listed_urls[0]}")

    return "\n".join(out_lines)


def _format_no_match(result: "SearchResult") -> str:
    header = (
        f"❌ No tee times: {_criteria_phrase(result.criteria)}"
    )
    course_names = sorted(t.name for t in result.targets_searched)
    if not course_names:
        return f"{header}\n(no courses configured for that filter)"

    # Keep the searched-courses line tight — list initials when too long.
    listed = ", ".join(course_names)
    if len(listed) > 120:
        listed = ", ".join(course_names[:5]) + f" …+{len(course_names) - 5}"
    return (
        f"{header}\n"
        f"Searched: {listed}\n"
        f"Reply WATCH to keep checking, or CHANGE to adjust."
    )


def _header_line(criteria: SearchCriteria) -> str:
    return f"✅ {_criteria_phrase(criteria)}"


def _criteria_phrase(criteria: SearchCriteria) -> str:
    """Compact human description: 'Sun 5/3 afternoon, 2p (18h)'."""
    date_str = criteria.date.strftime("%a %-m/%-d")
    window = _WINDOW_LABEL[criteria.window]
    holes = "9h" if criteria.holes == 9 else "18h"
    return f"{date_str} {window}, {criteria.players}p ({holes})"


def _course_line(name: str, slots: list[TeeTime], holes: int) -> str:
    """One bullet line per course.

    `4:20p` rather than `4:20 PM` to save chars; SMS is the consumer.
    """
    earliest = _short_time(slots[0].start_time)
    latest = _short_time(slots[-1].start_time)
    range_str = earliest if earliest == latest else f"{earliest}–{latest}"

    # Daylight risk roll-up.
    risks = Counter(assess(s.start_time, holes).risk for s in slots)
    after_dark = risks.get(DaylightRisk.AFTER_DARK, 0)
    twilight = risks.get(DaylightRisk.TWILIGHT, 0)

    main = f"• {name} — {len(slots)} slot{'s' if len(slots) != 1 else ''}, {range_str}"

    risk_part: str | None = None
    if after_dark:
        risk_part = (
            "all after dark" if len(slots) == after_dark else f"{after_dark} after dark"
        )
    elif twilight:
        risk_part = (
            "all finish at dusk" if len(slots) == twilight else f"{twilight} at dusk"
        )

    if risk_part:
        return f"{main} · ⚠️ {risk_part}"
    return main


def _short_time(dt: datetime) -> str:
    """'4:20p', '11:30a' — compact and SMS-friendly."""
    s = dt.strftime("%-I:%M%p").lower()  # '4:20pm'
    return s.replace("am", "a").replace("pm", "p")
