"""Command-line entry point — `tt search ...`.

Designed to mirror what the SMS layer will eventually do: parse a
request, fan out, and print the matches. The CLI is the harness we
build and verify the core against before any external services
(Twilio, scheduler, deployment) come in.

Usage examples:
    tt search --date 2026-05-03 --players 2 --window afternoon
    tt search --date sunday --players 4 --window any --course westminster
    tt parse "tee time tomorrow afternoon for 2"
    tt ask "tee time tomorrow afternoon for 2 at westminster"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

from tee_time_checker.config import load_targets
from tee_time_checker.domain import SearchCriteria, TimeWindow
from tee_time_checker.search import SearchResult, build_default_registry, search
from tee_time_checker.summary import format_sms_summary

# Auto-load a project-root .env if present. Lets devs keep ANTHROPIC_API_KEY
# (and later Twilio creds) in a gitignored file instead of exporting in
# every shell. Production deploys (Fly.io secrets) override this naturally
# since `load_dotenv` doesn't overwrite existing env vars by default.
load_dotenv()


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

    p_parse = sub.add_parser(
        "parse",
        help="Parse a natural-language request via Claude API (debug only — no search)",
    )
    p_parse.add_argument("text", help="The SMS-style message to parse")

    p_ask = sub.add_parser(
        "ask",
        help="Parse a natural-language request and run the search (mirrors SMS flow)",
    )
    p_ask.add_argument("text", help="The SMS-style message")
    p_ask.add_argument(
        "--format",
        choices=["verbose", "sms"],
        default="sms",
        help="Output format (default: sms — what the user would receive)",
    )

    sub.add_parser(
        "chat",
        help="Multi-turn REPL — type messages, get responses. Simulates SMS dialog.",
    )

    args = parser.parse_args(argv)

    if args.command == "search":
        return _cmd_search(args)
    if args.command == "parse":
        return _cmd_parse(args)
    if args.command == "ask":
        return _cmd_ask(args)
    if args.command == "chat":
        return _cmd_chat(args)
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


def _require_anthropic_key() -> int | None:
    """Pre-flight check for the Claude API key.

    Returns an exit code to short-circuit the command, or None if the
    env var is set and we should proceed. Friendlier than letting the
    SDK raise its generic auth error.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None
    print(
        "ANTHROPIC_API_KEY not set.\n"
        "  Quick fix:  ANTHROPIC_API_KEY=sk-ant-... uv run tt ask '...'\n"
        "  Or:         echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env\n"
        "  (.env is gitignored; auto-loaded on every CLI run.)",
        file=sys.stderr,
    )
    return 2


def _cmd_parse(args: argparse.Namespace) -> int:
    """Parse-only path — useful for inspecting what the model would extract.

    Doesn't touch the search/booking adapters. The output is the raw
    Pydantic model from the parser, dumped as JSON so it's easy to
    eyeball or pipe.
    """
    # Lazy import so search-only invocations don't pay the anthropic
    # SDK import cost.
    from tee_time_checker.parser import parse

    if (rc := _require_anthropic_key()) is not None:
        return rc

    targets = load_targets(known_adapters=set(build_default_registry().keys()))
    course_display_names = {t.slug: t.name for t in targets}

    parsed = parse(
        args.text,
        today=date.today(),
        course_display_names=course_display_names,
    )

    # Use mode='json' so date objects serialize as ISO strings.
    print(json.dumps(parsed.model_dump(mode="json"), indent=2))
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    """End-to-end SMS-style flow: parse the message, then search (or ask back).

    Mirrors what the Twilio webhook will eventually do — exact same code
    path except the input is argv text instead of an SMS body and the
    output is stdout instead of an outbound message.
    """
    from tee_time_checker.parser import parse

    if (rc := _require_anthropic_key()) is not None:
        return rc

    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))
    course_display_names = {t.slug: t.name for t in targets}

    parsed = parse(
        args.text,
        today=date.today(),
        course_display_names=course_display_names,
    )

    if parsed.needs_clarification:
        # Send the clarification message verbatim — the parser already
        # phrased it for SMS consumption.
        msg = parsed.clarification_message or "Sorry, can you rephrase?"
        print(msg)
        print()
        print(f"[length: {len(msg)} chars, {(len(msg) // 70) + 1} SMS segment(s) UCS-2]")
        return 0

    # Translate the parsed result into our internal SearchCriteria. The
    # parser leaves Nones where the user didn't specify; we fill in the
    # same defaults the explicit-args path uses.
    if parsed.date is None or parsed.players is None:
        # Defensive — should have been caught by needs_clarification.
        print("Parser returned no date/players but didn't ask back; aborting.", file=sys.stderr)
        return 1

    criteria = SearchCriteria(
        date=parsed.date,
        players=parsed.players,
        window=TimeWindow(parsed.window or "any"),
        holes=parsed.holes or 18,
        course_filter=parsed.courses,
    )

    print(
        f"Parsed: {criteria.date} · {criteria.players} players · "
        f"{criteria.window.value} · {criteria.holes} holes"
        + (f" · courses={criteria.course_filter}" if criteria.course_filter else "")
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

    return 0 if not result.errors else 1


def _cmd_chat(args: argparse.Namespace) -> int:
    """Multi-turn REPL — simulates the SMS dialog flow against in-memory state.

    Each turn parses with the previous partial as context, so a follow-up
    "2" after "walnut saturday morning" produces a complete search rather
    than another clarification. State lives only in this process — quit
    and you start fresh; persistent per-phone state arrives with the
    SQLite layer in the WATCH phase.

    Commands inside the REPL:
      reset    clear pending state (start a fresh request)
      exit     leave the REPL (Ctrl-D also works)
    """
    from tee_time_checker.parser import ParsedSearch, parse

    if (rc := _require_anthropic_key()) is not None:
        return rc

    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))
    course_display_names = {t.slug: t.name for t in targets}

    print("Multi-turn chat. Type 'exit' or Ctrl-D to quit, 'reset' to clear state.")
    print()

    pending: ParsedSearch | None = None

    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if text.lower() == "reset":
            pending = None
            print("(state cleared)")
            print()
            continue

        parsed = parse(
            text,
            today=date.today(),
            course_display_names=course_display_names,
            previous=pending,
        )

        if parsed.needs_clarification:
            # Hold the partial — next turn merges into it.
            pending = parsed
            print(parsed.clarification_message or "Sorry, can you rephrase?")
            print()
            continue

        # Search runs — state is consumed.
        pending = None

        if parsed.date is None or parsed.players is None:
            # Defensive — shouldn't happen if needs_clarification was honored.
            print("Parser returned an incomplete parse without asking back; aborting.", file=sys.stderr)
            continue

        criteria = SearchCriteria(
            date=parsed.date,
            players=parsed.players,
            window=TimeWindow(parsed.window or "any"),
            holes=parsed.holes or 18,
            course_filter=parsed.courses,
        )
        result = search(criteria, targets, registry)
        print(format_sms_summary(result))
        print()

    return 0


def _format_players(min_p: int, max_p: int) -> str:
    """Format a slot's allowed party size, mirroring booking-site phrasing."""
    if min_p == max_p:
        unit = "player" if min_p == 1 else "players"
        return f"{min_p} {unit} only"
    return f"{min_p}-{max_p} players"


if __name__ == "__main__":
    raise SystemExit(main())
