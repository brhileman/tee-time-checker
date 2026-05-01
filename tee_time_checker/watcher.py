"""WATCH scheduler — turns active watches into periodic search runs.

Lifecycle of a single watch:

    start_watch (state)
        ↓
    every ~10 min (jittered 8–13 min):  process_due()
        - if expired → mark expired, send "watched 24h, no luck"
        - else run search via existing orchestrator
            - if slots > 0 → mark fired, send SMS summary, done
            - else        → record check, schedule next tick

Scheduling shape: a single APScheduler interval job ticks once a minute
and calls `process_due()`, which loads `list_due_watches()` and processes
each. Per-watch jitter lives in `next_check_at` (8-13 min added per
non-firing tick), not in the scheduler trigger.

Notifications go through a `Notifier` callable so this module is
agnostic to the eventual transport. `PrintNotifier` (the default for
local CLI use) prints to stdout with an SMS-shaped header. The Twilio
implementation will plug in here in phase 10 by passing a different
notifier to `process_due()` / `run_forever()`.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from tee_time_checker import state
from tee_time_checker.config import load_targets
from tee_time_checker.search import build_default_registry, search
from tee_time_checker.summary import format_sms_summary
from tee_time_checker.state import Watch

log = logging.getLogger(__name__)

# Jitter bounds for the next check after a non-firing tick. Keeps
# load on platform APIs randomized — many users with overlapping
# watches won't all hit the same minute.
_NEXT_CHECK_MIN_MINUTES = 8
_NEXT_CHECK_MAX_MINUTES = 13


# ──────────────────────────────────────────────────────────────────────
# Notifier protocol
# ──────────────────────────────────────────────────────────────────────


class Notifier(Protocol):
    """Sends an SMS-shaped message to a phone number.

    The watcher doesn't care HOW; we'll plug Twilio in here for phase 10.
    """

    def notify(self, phone: str, body: str) -> None: ...


@dataclass
class PrintNotifier:
    """Default notifier — print to stdout with an SMS-shaped envelope.

    Useful for local development and the `tt watch run` CLI command.
    The output mimics what the Twilio version will send, so reading
    the terminal is a faithful preview of the eventual SMS.
    """

    def notify(self, phone: str, body: str) -> None:
        print()
        print(f"━━━ [SMS to {phone}] ━━━")
        print(body)
        print(f"━━━ ({len(body)} chars, {(len(body) // 70) + 1} segment(s)) ━━━")
        print()


# ──────────────────────────────────────────────────────────────────────
# Tick processor
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TickResult:
    """What `process_due` did this cycle — surfaced for tests / CLI debug."""

    checked: int
    fired: int
    expired: int
    errors: int


def process_due(
    notifier: Notifier | None = None,
    *,
    now: datetime | None = None,
) -> TickResult:
    """One sweep over due watches. Safe to call manually for testing.

    The function:
    - finalizes any watch past its expiry,
    - re-runs the search for the rest,
    - notifies + marks fired on first hit,
    - re-schedules with fresh jitter on miss.

    Per-watch errors don't abort the sweep — a single flaky platform
    shouldn't black out the others.
    """
    notifier = notifier or PrintNotifier()
    now = now or datetime.now(tz=ZoneInfo("UTC"))

    due = state.list_due_watches(now=now)
    if not due:
        return TickResult(checked=0, fired=0, expired=0, errors=0)

    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))

    fired = 0
    expired = 0
    errors = 0

    for watch in due:
        try:
            outcome = _process_one(watch, targets=targets, registry=registry, notifier=notifier, now=now)
            if outcome == "fired":
                fired += 1
            elif outcome == "expired":
                expired += 1
        except Exception as e:
            log.exception("watch %s failed: %s", watch.phone, e)
            errors += 1

    return TickResult(checked=len(due), fired=fired, expired=expired, errors=errors)


def _process_one(
    watch: Watch,
    *,
    targets,
    registry,
    notifier: Notifier,
    now: datetime,
) -> str:
    """Process one due watch. Returns 'fired' | 'checked' | 'expired'."""
    # Expiry check happens FIRST so we don't run a search for a watch
    # we're about to kill anyway.
    if now >= watch.expires_at:
        state.mark_watch_expired(watch.phone, expired_at=now)
        notifier.notify(
            watch.phone,
            "Watched 24h with no luck — nothing matching your search came up. "
            "Reply with a new request to start over.",
        )
        return "expired"

    result = search(watch.criteria, targets, registry)

    if result.tee_times:
        # Hit — notify-once-then-stop semantics.
        state.mark_watch_fired(watch.phone, slot_count=len(result.tee_times), fired_at=now)
        notifier.notify(watch.phone, format_sms_summary(result))
        return "fired"

    # Miss — schedule next check with fresh jitter.
    next_at = now + timedelta(
        minutes=random.uniform(_NEXT_CHECK_MIN_MINUTES, _NEXT_CHECK_MAX_MINUTES)
    )
    state.record_check(
        watch.phone,
        slot_count=0,
        next_check_at=next_at,
        checked_at=now,
    )
    return "checked"


# ──────────────────────────────────────────────────────────────────────
# Foreground runner (for `tt watch run` — local dev)
# ──────────────────────────────────────────────────────────────────────


def run_forever(*, notifier: Notifier | None = None, tick_seconds: int = 60) -> None:
    """Block forever, ticking every `tick_seconds` seconds.

    Use APScheduler so the tick is robust to the function taking longer
    than the interval (it skips overlapping fires) and so we can later
    add other periodic jobs (purge_expired_pending, adapter health
    pings) without rolling our own loop.
    """
    notifier = notifier or PrintNotifier()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: process_due(notifier),
        trigger="interval",
        seconds=tick_seconds,
        next_run_time=datetime.now(tz=ZoneInfo("UTC")),  # fire immediately on start
        id="process_due",
        max_instances=1,  # don't overlap if a tick runs long
    )
    scheduler.add_job(
        state.purge_expired_pending,
        trigger="interval",
        minutes=15,
        id="purge_expired_pending",
        max_instances=1,
    )
    scheduler.start()
    log.info("watcher running (tick=%ds); ctrl-c to stop", tick_seconds)
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
