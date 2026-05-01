"""Command-line entry point — `tt search ...`.

Designed to mirror what the SMS layer will eventually do: parse a
request, fan out, and print the matches. The CLI is the harness we
build and verify the core against before any external services
(Twilio, scheduler, deployment) come in.

Usage examples:
    tt search --date 2026-05-03 --players 2 --window afternoon
    tt search --date sunday --players 4 --window any --course westminster
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from tee_time_checker.config import load_targets
from tee_time_checker.domain import SearchCriteria, TimeWindow
from tee_time_checker.search import SearchResult, build_default_registry, search
from tee_time_checker.summary import format_sms_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tt",
        description="Tee time availability checker",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search configured courses for tee times")
    p_search.add_argument(
        "--date",
        required=True,
        help="Date to search. Accepts YYYY-MM-DD, 'today', 'tomorrow', or a weekday name.",
    )
    p_search.add_argument("--players", type=int, default=2, help="Party size (default: 2)")
    p_search.add_argument(
        "--window",
        choices=[w.value for w in TimeWindow],
        default=TimeWindow.ANY.value,
        help="Time of day window (default: any)",
    )
    p_search.add_argument(
        "--holes",
        type=int,
        choices=[9, 18],
        default=18,
        help="Number of holes (default: 18)",
    )
    p_search.add_argument(
        "--course",
        action="append",
        default=None,
        help="Restrict to this target slug. Repeatable.",
    )
    p_search.add_argument(
        "--format",
        choices=["verbose", "sms"],
        default="verbose",
        help="Output format. 'sms' previews the SMS-ready summary.",
    )

    args = parser.parse_args(argv)

    if args.command == "search":
        return _cmd_search(args)
    return 2


def _cmd_search(args: argparse.Namespace) -> int:
    criteria = SearchCriteria(
        date=_parse_date(args.date),
        players=args.players,
        window=TimeWindow(args.window),
        holes=args.holes,
        course_filter=args.course,
    )

    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))
    if not targets:
        print("No targets configured (or all skipped). Check courses.toml.", file=sys.stderr)
        return 1

    print(
        f"Searching {len(targets)} target(s) for "
        f"{criteria.date} · {criteria.players} players · {criteria.window.value} · {criteria.holes} holes"
    )
    print()

    result = search(criteria, targets, registry)

    if args.format == "sms":
        body = format_sms_summary(result)
        print(body)
        print()
        print(f"[length: {len(body)} chars, {(len(body) // 70) + 1} SMS segment(s) UCS-2]")
    else:
        _print_result(result)

    if result.errors:
        return 1
    return 0


def _parse_date(s: str) -> date:
    """Accept ISO dates, 'today', 'tomorrow', or weekday names ('sunday')."""
    s = s.strip().lower()
    today = date.today()
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if s in weekdays:
        target_idx = weekdays.index(s)
        days_ahead = (target_idx - today.weekday()) % 7
        # If today is the named day, jump to next week — closer match to user intent.
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise SystemExit(
            f"Could not parse date {s!r}. Use YYYY-MM-DD, 'today', 'tomorrow', or a weekday."
        ) from None


def _print_result(result: SearchResult) -> None:
    if not result.tee_times:
        print("No matching tee times found.")
        for slug in {t.slug for t in result.targets_searched}:
            errs = [e for e in result.errors if e.target.slug == slug]
            if errs:
                for e in errs:
                    print(f"  {slug}: {e.error}")
        return

    # Group by course for the summary feel we'll mirror over SMS later.
    by_course: dict[str, list] = {}
    for tt in result.tee_times:
        by_course.setdefault(tt.course_name, []).append(tt)

    total = len(result.tee_times)
    print(f"Found {total} slot(s) across {len(by_course)} course(s):")
    print()

    for course_name in sorted(by_course):
        slots = by_course[course_name]
        first = slots[0].start_time.strftime("%I:%M %p").lstrip("0").lower()
        last = slots[-1].start_time.strftime("%I:%M %p").lstrip("0").lower()
        slug = slots[0].course_slug
        url = slots[0].booking_url or ""
        print(f"  {course_name}  ({slug})")
        print(f"    {len(slots)} slots, {first} → {last}")
        if url:
            print(f"    Book: {url}")
        # Show up to 6 individual slots so it's clear what's there.
        preview = slots[: min(6, len(slots))]
        for tt in preview:
            t = tt.start_time.strftime("%I:%M %p").lstrip("0").lower()
            players = _format_players(tt.min_players, tt.max_players)
            print(f"      {t}  {players}")
        if len(slots) > len(preview):
            print(f"      … +{len(slots) - len(preview)} more")
        print()

    if result.errors:
        print("Errors:")
        for e in result.errors:
            print(f"  {e.target.slug}: {e.error}")


def _format_players(min_p: int, max_p: int) -> str:
    """Format a slot's allowed party size, mirroring booking-site phrasing."""
    if min_p == max_p:
        unit = "player" if min_p == 1 else "players"
        return f"{min_p} {unit} only"
    return f"{min_p}-{max_p} players"


if __name__ == "__main__":
    raise SystemExit(main())
