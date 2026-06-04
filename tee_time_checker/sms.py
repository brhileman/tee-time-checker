"""SMS handler — turns one inbound message into one or more outbound replies.

This is the orchestration layer that ties together everything we've built:
parser, search, watcher state, summary formatter. The HTTP webhook (web.py)
calls `handle_sms()` for every inbound; the production deploy gets the
real Twilio path, dev gets the print/recording path — same handler, just
a different `Notifier`.

Persistent per-phone state (the SQLite layer) is what makes the multi-turn
flow work. The CLI's `tt chat` REPL kept that state in memory because the
process ran the whole conversation; here, every webhook is its own process
invocation, so we look state up by phone number on every call.

State semantics:

- `pending_conversations` holds the most-recent ParsedSearch for a phone
  whether it's partial (waiting on more input) or fully complete-but-empty
  (waiting on a `WATCH` reply). On any merge or new search, we replace it.
- `watches` holds at most one active 24h watch per phone — the scheduler
  in watcher.py drives those independently of inbound SMS.

Commands beat NL parsing. `STOP`/`HELP`/`WATCH` are exact-match keywords;
anything else routes to the parser. We keep the command vocabulary tight
(`STOP` / `WATCH` / `HELP`) per spec — adding LIST/COURSES later is easy.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import TYPE_CHECKING

from tee_time_checker import profile as profile_mod
from tee_time_checker import state
from tee_time_checker.config import load_targets
from tee_time_checker.domain import SearchCriteria, TimeWindow
from tee_time_checker.geo import drive_minutes, zip_coords
from tee_time_checker.parser import ParsedSearch, parse
from tee_time_checker.search import build_default_registry, search
from tee_time_checker.summary import format_sms_summary

if TYPE_CHECKING:
    from tee_time_checker.watcher import Notifier

log = logging.getLogger(__name__)


_HELP_TEXT = (
    "Grip it and rip it, baby. Tell me what you need:\n"
    "  tee time tomorrow afternoon for 2\n"
    "  saturday morning for 4 at westminster\n\n"
    "Commands:\n"
    "  WATCH — I'll keep hunting for 24h. Like a lion.\n"
    "  STOP  — call off the hunt\n"
    "  HELP  — you're lookin' at it"
)


def handle_sms(
    phone: str,
    body: str,
    *,
    notifier: "Notifier",
    today: date_cls | None = None,
    watch_key: str | None = None,
) -> None:
    """Process one inbound SMS. Sends replies via the notifier.

    `phone` is the stable user identifier (Discord user ID) used for
    profile, pending, and last_search state. `watch_key` encodes the
    reply channel as "{user_id}:{channel_id}" so the watcher knows
    where to send notifications when a watch fires.
    """
    body = body.strip()
    if not body:
        return
    today = today or date_cls.today()
    wkey = watch_key or phone  # key used for watch storage (encodes reply channel)

    # New or mid-onboarding user — handle setup before anything else.
    if profile_mod.needs_onboarding(phone):
        registry = build_default_registry()
        targets = load_targets(known_adapters=set(registry.keys()))
        step = profile_mod.onboarding_step(phone)
        if step == "zip" and state.get_profile(phone) is None:
            profile_mod.start_onboarding(phone, notifier=notifier)
            return
        if profile_mod.handle_onboarding(phone, body, notifier=notifier, targets=targets):
            return

    # Commands first — exact-match short keywords.
    cmd = _detect_command(body)
    if cmd is not None:
        _handle_command(cmd, phone, notifier=notifier, watch_key=wkey)
        return

    _handle_natural_language(phone, body, today=today, notifier=notifier, watch_key=wkey)


# ──────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────

# Single-word, case-insensitive. We deliberately don't match
# punctuation-stripped variants — keep it tight, the help message
# documents the exact tokens.
_COMMAND_KEYWORDS = {
    "STOP": {"stop", "cancel", "quit"},
    "WATCH": {"watch"},
    "HELP": {"help", "?"},
}


def _detect_command(body: str) -> str | None:
    """Return a command name when `body` is a single keyword, else None."""
    token = body.strip().lower().rstrip(".!?")
    if not token or " " in token or len(token) > 10:
        return None
    for cmd, keywords in _COMMAND_KEYWORDS.items():
        if token in keywords:
            return cmd
    return None


def _handle_command(cmd: str, phone: str, *, notifier: "Notifier", watch_key: str) -> None:
    if cmd == "HELP":
        notifier.notify(phone, _HELP_TEXT)
        return

    if cmd == "STOP":
        cancelled = state.cancel_watch(watch_key)
        state.clear_pending(phone)
        notifier.notify(
            phone,
            "Watch cancelled. Go light a cigarette." if cancelled else "No active watch — you're already in the fairway.",
        )
        return

    if cmd == "WATCH":
        pending = state.get_pending(phone)
        if (
            pending is None
            or pending.needs_clarification
            or pending.date is None
            or pending.players is None
        ):
            notifier.notify(
                phone,
                "Easy there — I need a tee time request first. "
                "Try somethin' like 'tee time tomorrow afternoon for 2'. "
                "If nothing's there, reply WATCH and I'll hunt for 24h.",
            )
            return

        criteria = _build_criteria(pending)
        state.start_watch(watch_key, criteria, initial_check_delay_minutes=10)
        state.clear_pending(phone)

        notifier.notify(
            phone,
            "I'm on it. Watching for the next 24h like a lion stalking the jungle. "
            "I'll holler when something opens up. Reply STOP to call it off.",
        )
        return


# ──────────────────────────────────────────────────────────────────────
# Natural language path
# ──────────────────────────────────────────────────────────────────────


def _handle_natural_language(
    phone: str,
    body: str,
    *,
    today: date_cls,
    notifier: "Notifier",
    watch_key: str,
) -> None:
    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))
    course_display_names = {t.slug: t.name for t in targets}
    course_areas = {t.slug: t.area for t in targets if t.area}

    user_profile = state.get_profile(phone)
    prior = state.get_pending(phone)
    last_search = state.get_last_search(phone)

    location_defaults_label: str | None = None
    if user_profile is not None:
        if user_profile.favorite_slugs:
            names = [course_display_names.get(s, s) for s in user_profile.favorite_slugs]
            location_defaults_label = ", ".join(names)
        elif user_profile.zipcode is not None:
            location_defaults_label = "whatever's close"

    parsed = parse(
        body,
        today=today,
        course_display_names=course_display_names,
        course_areas=course_areas,
        previous=prior,
        last_search=last_search,
        location_defaults_label=location_defaults_label,
    )

    # Still missing required fields → save partial, ask back.
    if parsed.needs_clarification:
        state.save_pending(phone, parsed)
        msg = parsed.clarification_message or (
            "I need a few more details. " + _HELP_TEXT
        )
        notifier.notify(phone, msg)
        return

    # Defensive — should be impossible if the parser honored its contract.
    if parsed.date is None or parsed.players is None:
        log.warning("parse returned incomplete + needs_clarification=False: %s", parsed)
        notifier.notify(phone, "Sorry, can you rephrase?")
        return

    criteria = _build_criteria(parsed)

    # Apply profile exclusions regardless of whether user specified courses.
    if user_profile is not None and user_profile.excluded_slugs:
        excluded = set(user_profile.excluded_slugs)
        effective_filter = [
            t.slug for t in targets
            if t.slug not in excluded
            and (criteria.course_filter is None or t.slug in criteria.course_filter)
        ]
        criteria = SearchCriteria(
            date=criteria.date,
            players=criteria.players,
            window=criteria.window,
            holes=criteria.holes,
            course_filter=effective_filter,
            target_time=criteria.target_time,
            time_min=criteria.time_min,
            time_max=criteria.time_max,
        )

    # Apply profile defaults when the user didn't specify courses/area.
    if criteria.course_filter is None and user_profile is not None:
        if user_profile.favorite_slugs:
            criteria = SearchCriteria(
                date=criteria.date,
                players=criteria.players,
                window=criteria.window,
                holes=criteria.holes,
                course_filter=user_profile.favorite_slugs,
                target_time=criteria.target_time,
                time_min=criteria.time_min,
                time_max=criteria.time_max,
            )
        elif user_profile.zipcode:
            coords = zip_coords(user_profile.zipcode)
            if coords:
                user_lat, user_lng = coords
                nearby = [
                    t.slug for t in targets
                    if t.lat is not None and t.lng is not None
                    and drive_minutes(user_lat, user_lng, t.lat, t.lng) <= (parsed.max_drive_minutes or 60)
                ]
                if nearby:
                    criteria = SearchCriteria(
                        date=criteria.date,
                        players=criteria.players,
                        window=criteria.window,
                        holes=criteria.holes,
                        course_filter=nearby,
                        target_time=criteria.target_time,
                        time_min=criteria.time_min,
                        time_max=criteria.time_max,
                    )

    # If this is a refinement and there's an active watch, update the watch.
    if parsed.is_refinement and state.get_active_watch(watch_key) is not None:
        state.cancel_watch(watch_key)
        state.start_watch(watch_key, criteria, initial_check_delay_minutes=10)
        state.clear_pending(phone)
        state.save_last_search(phone, parsed)
        notifier.notify(
            phone,
            "Got it — watch updated. I'm on the new criteria. Reply STOP to cancel.",
        )
        return

    # Complete parse — run the search.
    result = search(criteria, targets, registry)
    state.save_last_search(phone, parsed)

    if result.tee_times:
        state.clear_pending(phone)
        notifier.notify(phone, format_sms_summary(result))
        return

    # Miss. Save the COMPLETE parse so a follow-up `WATCH` reply can use it.
    state.save_pending(phone, parsed)
    notifier.notify(phone, format_sms_summary(result))


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────


def _build_criteria(parsed: ParsedSearch) -> SearchCriteria:
    """Translate a ParsedSearch into a SearchCriteria with sensible defaults.

    Only legal to call when `parsed.date` and `parsed.players` are set —
    callers are responsible for the precondition check.
    """
    assert parsed.date is not None and parsed.players is not None
    from datetime import time as time_cls
    def _parse_hhmm(s: str | None):
        if s is None:
            return None
        h, m = s.split(":")
        return time_cls(int(h), int(m))

    return SearchCriteria(
        date=parsed.date,
        players=parsed.players,
        window=TimeWindow(parsed.window or "any"),
        holes=parsed.holes or 18,
        course_filter=parsed.courses,
        target_time=parsed.target_time,
        time_min=_parse_hhmm(parsed.time_min),
        time_max=_parse_hhmm(parsed.time_max),
    )


# ──────────────────────────────────────────────────────────────────────
# Test notifier — useful for `tt sms reply` and unit-style verification
# ──────────────────────────────────────────────────────────────────────


class RecordingNotifier:
    """Notifier that records every outbound message instead of sending it.

    Useful for the simulated-webhook CLI command and any future tests.
    Implements the Notifier protocol structurally.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # [(phone, body), ...]

    def notify(self, phone: str, body: str) -> None:
        self.sent.append((phone, body))
