"""User profile onboarding — first-time setup flow.

Two-step conversation:
  1. Ask for zipcode
  2. Ask for usual courses, max drive time, or "anywhere"

After both steps the profile is complete and the user can start searching.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tee_time_checker import state
from tee_time_checker.geo import zip_coords

if TYPE_CHECKING:
    from tee_time_checker.adapters.base import Target
    from tee_time_checker.watcher import Notifier

_WELCOME = (
    "Hey, I'm Daly. Grip it and rip it.\n\n"
    "First things first — what's your zip code? I'll use it to find courses near you."
)

_ASK_COURSES_TEMPLATE = (
    "Got it. Here's what I can search:\n\n"
    "{course_list}\n\n"
    "Name your go-to course(s) or say **anywhere** if you don't give a shit."
)

_DONE = "You're locked and loaded. Send me a tee time request whenever you're ready."

_BAD_ZIP = "Hmm, I don't recognize that zip — gotta be a Colorado zip. Try again."

_NO_COURSES_MATCHED_TEMPLATE = (
    "Hmm, I don't have that one. Here's what I can search:\n\n"
    "{course_list}\n\n"
    "Pick any of those or say **anywhere** if you don't give a shit."
)


def needs_onboarding(phone: str) -> bool:
    profile = state.get_profile(phone)
    return profile is None or profile.onboarding_step != "done"


def onboarding_step(phone: str) -> str:
    profile = state.get_profile(phone)
    return "zip" if profile is None else profile.onboarding_step


def start_onboarding(phone: str, *, notifier: "Notifier") -> None:
    state.upsert_profile(phone, onboarding_step="zip")
    notifier.notify(phone, _WELCOME)


def handle_onboarding(
    phone: str,
    body: str,
    *,
    notifier: "Notifier",
    targets: list["Target"],
) -> bool:
    """Handle one onboarding message. Returns True if onboarding is still in progress."""
    profile = state.get_profile(phone)
    if profile is None or profile.onboarding_step == "done":
        return False

    if profile.onboarding_step == "zip":
        zipcode = _extract_zip(body)
        if zipcode is None or zip_coords(zipcode) is None:
            notifier.notify(phone, _BAD_ZIP)
            return True
        state.upsert_profile(phone, zipcode=zipcode, onboarding_step="courses")
        course_list = "\n".join(f"• {t.name}" for t in sorted(targets, key=lambda t: t.name))
        notifier.notify(phone, _ASK_COURSES_TEMPLATE.format(course_list=course_list))
        return True

    if profile.onboarding_step == "courses":
        body_lower = body.strip().lower()

        # "anywhere" / don't care → proximity only, use default drive limit
        if any(w in body_lower for w in ("anywhere", "any", "don't give", "idgaf", "don't care", "doesn't matter")):
            state.upsert_profile(phone, favorite_slugs=[], onboarding_step="done")
            notifier.notify(phone, _DONE)
            return False

        # Named courses — with optional exclusions ("anywhere but knolls")
        favorites, excluded = _match_courses_with_exclusions(body, targets)

        if not favorites and not excluded:
            course_list = "\n".join(f"• {t.name}" for t in sorted(targets, key=lambda t: t.name))
            notifier.notify(phone, _NO_COURSES_MATCHED_TEMPLATE.format(course_list=course_list))
            return True

        state.upsert_profile(phone, favorite_slugs=favorites, excluded_slugs=excluded, onboarding_step="done")
        notifier.notify(phone, _DONE)
        return False

    return False


def _extract_zip(text: str) -> str | None:
    m = re.search(r"\b(\d{5})\b", text)
    return m.group(1) if m else None


def _extract_drive_minutes(text: str) -> int | None:
    """Parse '30 minutes', '30 mins', '1 hour', '45 min', etc. Returns minutes or None."""
    text = text.lower()
    # "X hour(s)" → X * 60
    m = re.search(r"(\d+(?:\.\d+)?)\s*hour", text)
    if m:
        return int(float(m.group(1)) * 60)
    # "X min(utes)"
    m = re.search(r"(\d+)\s*min", text)
    if m:
        return int(m.group(1))
    return None


def _match_courses_with_exclusions(
    body: str, targets: list["Target"]
) -> tuple[list[str], list[str]]:
    """Return (favorite_slugs, excluded_slugs) parsed from body.

    Exclusion markers: "but", "except", "not", "no", "never", "minus".
    Anything after an exclusion marker is treated as excluded.
    Anything before is treated as a favorite (unless it's "anywhere"/"any").
    """
    body_lower = body.lower()

    # Split on exclusion markers to find the exclusion portion.
    exclusion_split = re.split(r"\b(but not|except|but|not|no|never|minus)\b", body_lower, maxsplit=1)

    include_text = exclusion_split[0]
    exclude_text = exclusion_split[2] if len(exclusion_split) > 2 else ""

    anywhere = any(w in include_text for w in ("anywhere", "any", "all", "don't care", "idgaf"))

    favorites: list[str] = []
    if not anywhere:
        favorites = _match_courses(include_text, targets)

    excluded = _match_courses(exclude_text, targets) if exclude_text else []

    return favorites, excluded


def _match_courses(text: str, targets: list["Target"]) -> list[str]:
    text_lower = text.lower()
    matched = []
    for t in targets:
        if t.slug.replace("-", " ") in text_lower or t.name.lower() in text_lower:
            matched.append(t.slug)
            continue
        words = [w for w in t.name.lower().split() if len(w) >= 4]
        if any(w in text_lower for w in words):
            matched.append(t.slug)
    return list(dict.fromkeys(matched))
