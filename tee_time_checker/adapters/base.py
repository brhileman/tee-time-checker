"""Adapter protocol — the contract every booking-platform integration follows.

Each adapter knows how to talk to one platform (CPS, MemberSports, etc.).
The orchestrator in `search.py` doesn't know or care which one — it just
calls `search()` and gets back a normalized `list[TeeTime]`.

Targets vs adapters:
- An *adapter* is the platform integration (e.g. `CPSAdapter`).
- A *target* is one configured entry in `courses.toml` — the platform plus
  the platform-specific params (course IDs, tenant slug, API key, etc.).
- One adapter handles many targets. The same `CPSAdapter` instance handles
  Westminster and Fossil Trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from tee_time_checker.domain import SearchCriteria, TeeTime


@dataclass(frozen=True, slots=True)
class Target:
    """One configured search destination from `courses.toml`.

    `params` is platform-specific and only the matching adapter understands
    its shape. Keeps the protocol simple — no `Union`s of param types here.
    """

    slug: str           # stable id used in CLI / logs / SearchCriteria.course_filter
    name: str           # human-readable, used in summaries
    adapter: str        # which adapter handles this target ("cps", "membersports", ...)
    timezone: str       # IANA tz name, e.g. "America/Denver"
    booking_url: str    # deep-link the user opens to actually book
    params: dict[str, Any]


class Adapter(Protocol):
    """All adapters implement this. Stateless — instantiate once, reuse."""

    name: str  # short identifier matching `Target.adapter`

    def search(self, target: Target, criteria: SearchCriteria) -> list[TeeTime]:
        """Return all slots at this target that match the criteria.

        Implementation contract:
        - Filter by `criteria.players` (slot must accept >= that many).
        - Honor `criteria.holes` where the platform supports it; ignore otherwise.
        - DO NOT filter by `criteria.window` — that's done centrally so the
          window definitions stay consistent across platforms.
        - Always return tz-aware `start_time` in the target's local timezone.
        - On API failure, raise; the orchestrator decides how to handle it.
        """
        ...
