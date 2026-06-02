"""SMS-style summary formatter for a SearchResult.

Output shape:

    ✅ Sun 5/3 afternoon, 2p (18h)
    • Legacy Ridge — 9 slots, 4:20p–6:00p · ⚠️ 7 after dark
    • Walnut Creek — 8 slots, 2:30p–5:50p · ⚠️ 5 after dark

No-match case:

    ❌ No tee times: Sun 5/3 afternoon, 2p (18h)
    Searched: Westminster, Riverdale, Wellshire, ...
    Reply WATCH to keep checking, or CHANGE to adjust.

Lists every matching course (the user wants completeness; long messages
just span more SMS segments). Sorted by slot count desc — best options
at the top — with alphabetical tiebreaker.

Booking URLs are deliberately omitted; users already have these
bookmarked, and they balloon segment count.

Daylight risk is computed per slot via daylight.py and rolled up into
a single per-course flag. We don't drop "after dark" slots — the user
might want a twilight round on purpose.
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

    Always lists every course with matching slots — the user wants
    completeness over brevity. Booking URLs are intentionally omitted:
    users already have those bookmarked.

    Sort order: most-slots first, then alphabetical as a tiebreaker —
    surfaces the best options at the top of long messages.
    """
    groups: dict[str, list[TeeTime]] = {}
    for tt in result.tee_times:
        groups.setdefault(tt.course_name, []).append(tt)
    for slots in groups.values():
        slots.sort(key=lambda t: t.start_time)

    by_count = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    out_lines = [_header_line(result.criteria), ""]
    for name, slots in by_count:
        out_lines.append(_course_line(name, slots, result.criteria.holes))

    return "\n".join(out_lines)


def _format_no_match(result: "SearchResult") -> str:
    header = f"❌ **No tee times — {_criteria_phrase(result.criteria)}**"
    course_names = sorted(t.name for t in result.targets_searched)
    if not course_names:
        return f"{header}\n\n(no courses configured for that filter)"

    listed = ", ".join(course_names)
    if len(listed) > 120:
        listed = ", ".join(course_names[:5]) + f" +{len(course_names) - 5} more"
    return (
        f"{header}\n\n"
        f"Searched: {listed}\n\n"
        f"Nothin' there. Reply **WATCH** and I'll keep huntin' — I don't give up easy."
    )


def _header_line(criteria: SearchCriteria) -> str:
    return f"🏌️ **Grip it and rip it — {_criteria_phrase(criteria)}**"


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

    main = f"• **{name}** — {len(slots)} slot{'s' if len(slots) != 1 else ''}, {range_str}"

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
