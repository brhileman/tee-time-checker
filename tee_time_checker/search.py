"""Search orchestrator — fans criteria out across configured targets.

Responsibilities:
- Maintain the registry of adapter implementations (name -> instance).
- Apply `criteria.course_filter` to pick which targets to hit.
- Run each target's adapter and collect results.
- Apply the time-window filter centrally so every adapter agrees on what
  "afternoon" means.
- Surface per-target errors without aborting the whole search — one
  flaky platform shouldn't black out the others.

Concurrency: synchronous for v1. Each adapter call is sub-second; even
all 5 platforms serialized is well under the 15s Twilio budget. We can
parallelize with a thread pool later if real measurements show we need it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from tee_time_checker.adapters.base import Adapter, Target
from tee_time_checker.adapters.chronogolf import ChronogolfAdapter
from tee_time_checker.adapters.cps import CPSAdapter
from tee_time_checker.adapters.membersports import MemberSportsAdapter
from tee_time_checker.adapters.noteefy import NoteefyAdapter
from tee_time_checker.adapters.quick18 import Quick18Adapter
from tee_time_checker.adapters.teeitup import TeeItUpAdapter
from tee_time_checker.daylight import DaylightRisk, assess
from tee_time_checker.domain import SearchCriteria, TeeTime


def build_default_registry() -> dict[str, Adapter]:
    """Map adapter name -> instance for one search round.

    Adapter instances may carry per-round caches (see MemberSports — its
    response cache is shared across sibling-course targets). Build fresh
    per orchestrator call so the cache lifetime matches one search.
    Names here are the source of truth for `Target.adapter` in courses.toml.
    """
    return {
        "chronogolf": ChronogolfAdapter(),
        "cps": CPSAdapter(),
        "membersports": MemberSportsAdapter(),
        "noteefy": NoteefyAdapter(),
        "quick18": Quick18Adapter(),
        "teeitup": TeeItUpAdapter(),
    }


@dataclass(slots=True)
class TargetError:
    """One target failed; we keep the search going and surface this later."""

    target: Target
    error: str


@dataclass(slots=True)
class SearchResult:
    """Outcome of a fanned-out search across targets."""

    criteria: SearchCriteria
    tee_times: list[TeeTime]
    errors: list[TargetError]
    targets_searched: list[Target]


def search(
    criteria: SearchCriteria,
    targets: list[Target],
    registry: dict[str, Adapter] | None = None,
) -> SearchResult:
    """Run the search across all (filtered) targets and return aggregated hits."""
    registry = registry or build_default_registry()

    selected = _filter_targets(targets, criteria.course_filter)

    all_slots: list[TeeTime] = []
    errors: list[TargetError] = []

    for target in selected:
        adapter = registry.get(target.adapter)
        if adapter is None:
            # Should be caught earlier by the loader, but defend anyway.
            errors.append(TargetError(target, f"no adapter registered for {target.adapter!r}"))
            continue

        try:
            slots = adapter.search(target, criteria)
        except Exception as e:
            errors.append(TargetError(target, f"{type(e).__name__}: {e}"))
            print(f"warning: {target.slug} failed: {e}", file=sys.stderr)
            continue

        # Apply the window filter centrally — adapters return full-day data.
        # Also drop slots where a round won't finish before dark.
        for s in slots:
            if not criteria.window.contains(s.start_time, criteria.time_min, criteria.time_max):
                continue
            if assess(s.start_time, criteria.holes).risk in (DaylightRisk.TWILIGHT, DaylightRisk.AFTER_DARK):
                continue
            all_slots.append(s)

    # Stable ordering: by course alphabetically, then by start time.
    all_slots.sort(key=lambda t: (t.course_name.lower(), t.start_time))

    return SearchResult(
        criteria=criteria,
        tee_times=all_slots,
        errors=errors,
        targets_searched=selected,
    )


def _filter_targets(targets: list[Target], course_filter: list[str] | None) -> list[Target]:
    """Restrict to targets whose slug is in `course_filter`, if any provided."""
    if not course_filter:
        return targets
    wanted = {s.lower() for s in course_filter}
    return [t for t in targets if t.slug.lower() in wanted]
