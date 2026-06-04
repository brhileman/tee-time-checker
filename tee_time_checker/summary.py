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

Course names are hyperlinked to the booking page pre-set to the search
date (Discord renders these as clickable links).

Daylight risk is computed per slot via daylight.py and rolled up into
a single per-course flag. We don't drop "after dark" slots — the user
might want a twilight round on purpose.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse, urlunparse

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
    """Compose the match-found summary."""
    groups: dict[str, list[TeeTime]] = {}
    for tt in result.tee_times:
        groups.setdefault(tt.course_name, []).append(tt)
    for slots in groups.values():
        slots.sort(key=lambda t: t.start_time)

    by_count = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))

    out_lines = [_header_line(result.criteria), ""]

    target = _parse_target_time(result.criteria.target_time)
    if target is not None:
        note = _target_time_note(target, result.tee_times)
        if note:
            out_lines.append(note)
            out_lines.append("")

    for name, slots in by_count:
        out_lines.append(_course_line(name, slots, result.criteria.holes, result.criteria.date))

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
    date_str = criteria.date.strftime("%A, %B %-d")
    window = _WINDOW_LABEL[criteria.window]
    holes = "9 holes" if criteria.holes == 9 else "18 holes"
    players = "1 player" if criteria.players == 1 else f"{criteria.players} players"
    return f"{date_str} · {window} · {players} · {holes}"


def _booking_url_for_date(base_url: str, d: date) -> str:
    """Append a date query param to a booking URL if the platform supports it."""
    parsed = urlparse(base_url)
    host = parsed.netloc.lower()
    date_str = d.isoformat()

    # Platforms that accept ?date=YYYY-MM-DD on their booking pages
    if any(p in host for p in ("cps.golf", "book.teeitup.com", "chronogolf.com")):
        sep = "&" if parsed.query else "?"
        return base_url + sep + urlencode({"date": date_str})

    # MemberSports, Riverdale, Quick18, etc. — link to base booking page
    return base_url


def _course_line(name: str, slots: list[TeeTime], holes: int, search_date: date) -> str:
    earliest = _short_time(slots[0].start_time)
    latest = _short_time(slots[-1].start_time)
    range_str = earliest if earliest == latest else f"{earliest}–{latest}"
    count = f"{len(slots)} slot{'s' if len(slots) != 1 else ''}"

    booking_url = slots[0].booking_url
    if booking_url:
        url = _booking_url_for_date(booking_url, search_date)
        label = f"[{name}]({url})"
    else:
        label = f"**{name}**"

    return f"• {label} — {count}, {range_str}"


def _short_time(dt: datetime) -> str:
    """'4:20p', '11:30a' — compact and SMS-friendly."""
    s = dt.strftime("%-I:%M%p").lower()  # '4:20pm'
    return s.replace("am", "a").replace("pm", "p")


def _parse_target_time(target_time: str | None) -> time | None:
    if target_time is None:
        return None
    try:
        h, m = target_time.split(":")
        return time(int(h), int(m))
    except Exception:
        return None


def _target_time_note(target: time, slots: list[TeeTime]) -> str | None:
    """Return a note if no slots fall within 30 min of the target time."""
    target_minutes = target.hour * 60 + target.minute
    close = [s for s in slots if abs(s.start_time.hour * 60 + s.start_time.minute - target_minutes) <= 30]
    if close:
        return None  # something near that time exists, no note needed

    target_str = _short_time(datetime(2000, 1, 1, target.hour, target.minute))
    earliest = min(slots, key=lambda s: s.start_time)
    earliest_str = _short_time(earliest.start_time)
    if earliest.start_time.hour * 60 + earliest.start_time.minute > target_minutes:
        return f"Nothing at {target_str} — earliest available is {earliest_str}."
    latest = max(slots, key=lambda s: s.start_time)
    latest_str = _short_time(latest.start_time)
    return f"Nothing at {target_str} — latest available is {latest_str}."
