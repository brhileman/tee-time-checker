"""Load `courses.toml` into a list of `Target`s.

`courses.toml` lives at the project root. Each `[[targets]]` block becomes
one `Target`. The `params` table is opaque to this loader — only the
matching adapter understands its keys.

Targets whose adapter isn't yet registered are skipped with a stderr
warning; this lets us list every eventual target now and have them
"light up" as adapters land, without the orchestrator silently treating
them as "no results."
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from tee_time_checker.adapters.base import Target

# Default location: <repo root>/courses.toml. Resolved relative to this
# file so it works no matter where the CLI is invoked from.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "courses.toml"


def load_targets(
    path: Path | None = None,
    *,
    known_adapters: set[str] | None = None,
) -> list[Target]:
    """Parse the config file, returning targets whose adapter is registered.

    `known_adapters`: set of adapter names that have implementations. Targets
    referencing other adapters are skipped with a warning. Pass `None` to
    accept all targets (useful for tests).
    """
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with config_path.open("rb") as f:
        data = tomllib.load(f)

    raw_targets = data.get("targets", [])
    targets: list[Target] = []
    for entry in raw_targets:
        try:
            target = Target(
                slug=entry["slug"],
                name=entry["name"],
                adapter=entry["adapter"],
                timezone=entry["timezone"],
                booking_url=entry["booking_url"],
                params=entry.get("params", {}),
                area=entry.get("area"),
            )
        except KeyError as e:
            raise ValueError(
                f"Missing required field {e} in target {entry.get('slug', '?')!r}"
            ) from None

        if known_adapters is not None and target.adapter not in known_adapters:
            print(
                f"warning: skipping target {target.slug!r} — "
                f"adapter {target.adapter!r} not yet implemented",
                file=sys.stderr,
            )
            continue

        targets.append(target)

    return targets
