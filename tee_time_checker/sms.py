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

from tee_time_checker import state
from tee_time_checker.config import load_targets
from tee_time_checker.domain import SearchCriteria, TimeWindow
from tee_time_checker.parser import ParsedSearch, parse
from tee_time_checker.search import build_default_registry, search
from tee_time_checker.summary import format_sms_summary

if TYPE_CHECKING:
    from tee_time_checker.watcher import Notifier

log = logging.getLogger(__name__)


_HELP_TEXT = (
    "Reply with what you're looking for, e.g.:\n"
    "  tee time tomorrow afternoon for 2\n"
    "  saturday morning for 4 at westminster\n\n"
    "Commands:\n"
    "  WATCH — keep checking for 24h after a no-match reply\n"
    "  STOP  — cancel any active watch\n"
    "  HELP  — show this message"
)


def handle_sms(
    phone: str,
    body: str,
    *,
    notifier: "Notifier",
    today: date_cls | None = None,
) -> None:
    """Process one inbound SMS. Sends replies via the notifier.

    Idempotent on its own state writes: the same body delivered twice
    won't double-fire (Twilio occasionally retries on transient errors).
    """
    body = body.strip()
    if not body:
        return  # Twilio sometimes delivers empty bodies on edge cases.
    today = today or date_cls.today()

    # Commands first — exact-match short keywords. Long messages skip the
    # command check so "stop trying to find me a foursome" doesn't trigger
    # the cancel branch.
    cmd = _detect_command(body)
    if cmd is not None:
        _handle_command(cmd, phone, notifier=notifier)
        return

    _handle_natural_language(phone, body, today=today, notifier=notifier)


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


def _handle_command(cmd: str, phone: str, *, notifier: "Notifier") -> None:
    if cmd == "HELP":
        notifier.notify(phone, _HELP_TEXT)
        return

    if cmd == "STOP":
        cancelled = state.cancel_watch(phone)
        state.clear_pending(phone)
        notifier.notify(
            phone,
            "Watch cancelled." if cancelled else "No active watch to cancel.",
        )
        return

    if cmd == "WATCH":
        # The user can reply WATCH only after sending a complete search
        # that found nothing — we stash that ParsedSearch in pending and
        # use it here.
        pending = state.get_pending(phone)
        if (
            pending is None
            or pending.needs_clarification
            or pending.date is None
            or pending.players is None
        ):
            notifier.notify(
                phone,
                "I don't have a complete search to watch yet. "
                "Send a request like 'tee time tomorrow afternoon for 2' "
                "first; if nothing's available, reply WATCH.",
            )
            return

        criteria = _build_criteria(pending)
        # Delay first re-check by 8-13 min — we already searched on the
        # original turn and got nothing, no value in immediately re-running.
        state.start_watch(phone, criteria, initial_check_delay_minutes=10)
        state.clear_pending(phone)

        notifier.notify(
            phone,
            "Watching for the next 24h. I'll text when something opens up. "
            "Reply STOP to cancel.",
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
) -> None:
    registry = build_default_registry()
    targets = load_targets(known_adapters=set(registry.keys()))
    course_display_names = {t.slug: t.name for t in targets}

    prior = state.get_pending(phone)

    parsed = parse(
        body,
        today=today,
        course_display_names=course_display_names,
        previous=prior,
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

    # Complete parse — run the search.
    criteria = _build_criteria(parsed)
    result = search(criteria, targets, registry)

    if result.tee_times:
        # Hit. Clear any partial state and reply with the summary.
        state.clear_pending(phone)
        notifier.notify(phone, format_sms_summary(result))
        return

    # Miss. Save the COMPLETE parse so a follow-up `WATCH` reply can
    # turn it into a watch — see `_handle_command(cmd='WATCH')`. Reuses
    # the pending_conversations table (parsed_json + 30-min TTL) since
    # the shape is identical.
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
    return SearchCriteria(
        date=parsed.date,
        players=parsed.players,
        window=TimeWindow(parsed.window or "any"),
        holes=parsed.holes or 18,
        course_filter=parsed.courses,
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
