"""Natural-language parser — turn an SMS into a `SearchCriteria`.

The user texts something like "tee time tomorrow afternoon for 2 at
westminster" and we need a structured query the search orchestrator can
use. The Claude API does the extraction; we constrain the output with a
Pydantic schema and `client.messages.parse()`.

Design notes:

- **Prompt caching.** The system prompt (parsing rules + course list) is
  stable across requests; today's date and the user's message vary. We
  put the static part in `system` with `cache_control: ephemeral` and
  the volatile part in the user turn. Adding/removing a course busts
  the cache once; otherwise reads cost ~10% of the input.

- **Model choice.** Default is `claude-opus-4-7` per the Claude API
  skill's hard rule. SMS parsing is the canonical "simple extraction"
  case where Haiku 4.5 is plenty (~5x cheaper, ~3x faster); flip
  `MODEL_ID` if cost matters.

- **No extended thinking.** SMS parsing isn't reasoning-heavy — adaptive
  thinking would add latency for no quality gain. Thinking is off by
  default on Opus 4.7 so we just don't pass the field.

- **Confidence vs clarification.** Rather than emit a confidence score
  and let downstream code interpret it, the model decides directly
  whether to ask back (`needs_clarification=true` plus a prepared
  message). Keeps the dialog logic in one place — the prompt.
"""

from __future__ import annotations

import json
from datetime import date as date_cls
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

# Default per the Claude API skill. For SMS extraction, Haiku 4.5 is a
# safe optimization — change this if the cost of Opus matters.
MODEL_ID = "claude-opus-4-7"

# Output schema. Optional fields default to `None` (not present); the
# search orchestrator translates `None` to its own defaults.
Window = Literal["morning", "midday", "afternoon", "any"]


class ParsedSearch(BaseModel):
    """Structured output from `parse()`.

    When `needs_clarification` is True, the SMS layer replies with
    `clarification_message` instead of running a search. Otherwise
    the other fields populate a `SearchCriteria` (with sensible
    defaults filled in for any None fields).
    """

    date: date_cls | None = Field(
        None,
        description="The day to play, ISO YYYY-MM-DD. Null if missing/unclear.",
    )
    players: int | None = Field(
        None,
        ge=1,
        le=9,
        description="Party size, 1-9. Null if missing/unclear.",
    )
    window: Window | None = Field(
        None,
        description="Time-of-day bucket. Null = any.",
    )
    holes: Literal[9, 18] | None = Field(
        None,
        description="Round length. Null = 18.",
    )
    courses: list[str] | None = Field(
        None,
        description=(
            "Course slugs from the configured list. "
            "Null = search every configured course."
        ),
    )
    needs_clarification: bool = Field(
        False,
        description="True when the parser can't proceed without asking back.",
    )
    clarification_message: str | None = Field(
        None,
        description=(
            "One-sentence question to send back to the user "
            "when needs_clarification is true."
        ),
    )
    is_refinement: bool = Field(
        False,
        description=(
            "True when the user is refining or modifying a previous search "
            "rather than starting a fresh one (e.g. 'nothing earlier?', "
            "'change to afternoon', 'try southwest instead')."
        ),
    )


_SYSTEM_PROMPT_TEMPLATE = """\
You parse SMS messages from golfers asking about tee times. Your output \
is a structured object the search system uses to query booking sites.

REQUIRED to run a search — if either is missing/unclear, set \
`needs_clarification=true` and put a one-sentence question in \
`clarification_message`:

- `date` — resolve relative references ("today", "tomorrow", "saturday", \
"this saturday", "next sunday") to a single ISO date YYYY-MM-DD. \
"This weekend" alone is ambiguous (Sat or Sun?) — ask back. Refuse dates \
more than 14 days out (most courses won't accept bookings beyond that).
- `players` — integer 1-9.

OPTIONAL with sensible defaults — leave null if the user didn't say:
- `window` — "morning" (open–10am), "midday" (10am–2pm), "afternoon" \
(2pm–close), "any" (full day).
- `holes` — 9 or 18.
- `courses` — list of slugs from below. Null means search all.

AREA / COURSE CLARIFICATION:
{location_clarification}

CONFIGURED COURSES (slug → display name → area):
{course_list}

COURSE MATCHING RULES:
- Match user-mentioned courses to slugs case-insensitively and tolerantly. \
Common shorthand: "walnut" or "walnut creek" or "legacy ridge" → \
"westminster" (those are the two courses that share the Westminster CPS \
tenant). "fossil" → "fossil-trace". "common ground" → "commonground".
- "Riverdale" alone is ambiguous (Dunes or Knolls?) — ask back which one. \
But "riverdale dunes" and "riverdale knolls" both unambiguously match \
their slugs.
- A user can list multiple courses in one message ("westminster or \
riverdale dunes") — return all matching slugs.
- If a user mentions a course we don't have, set \
`needs_clarification=true` and list the configured course display names \
in the message.

AREA MATCHING RULES:
- Users may request courses by area/location instead of by name. \
Resolve any geographic reference (e.g. "north side", "near downtown", \
"south Denver", "west suburbs", "close to Thornton") to the slugs whose \
area tag best matches. Return all matching slugs.
- Area tags in use: "northwest", "northeast", "southwest", "southeast". \
Use common sense to map vague references — "north side" could match both \
"northwest" and "northeast", in which case return all slugs from both. \
"west side" matches "northwest" and "southwest", etc.
- If both a course name AND an area are mentioned, union the results.
- If the area reference is too vague to map confidently, ask back with the \
area options.

NON-SEARCH MESSAGES:
- Greetings, thanks, random text → set `needs_clarification=true` with: \
"I help find golf tee times. Try: 'tee time tomorrow afternoon for 2'."
- Don't try to extract anything from non-search messages.

FOLLOW-UP REFINEMENTS:
- If the user turn contains a "Last completed search" line, the user \
previously ran a full search with those criteria. Use it as context \
for follow-up messages.
- If the new message is clearly a refinement of that search \
("nothing earlier?", "what about morning?", "try the southwest courses", \
"change to afternoon", "any 9-hole options?", "how about tomorrow instead?") \
→ set `is_refinement=true`, carry forward all non-overridden fields from \
the last search, and apply the change. Do NOT ask for location again if \
the last search already had a course/area preference.
- If the user has an active watch and says something like "change it to \
afternoon" or "watch southwest instead" → set `is_refinement=true` and \
update the relevant criteria field.
- A clearly NEW request (different date, completely new topic) → \
`is_refinement=false`, treat fresh.

PRIOR PARTIAL PARSE (multi-turn dialog):
- If the user turn contains a "Previous partial parse" line, the user \
already supplied those values in earlier message(s). Carry every \
non-null field forward into your output unless the new SMS clearly \
overrides it (e.g. they said "saturday" before, now they say "actually \
sunday" — use sunday).
- Once all required fields (date, players) are filled, run the search — \
do NOT ask back again. A bare "2" in reply to a previous "how many \
players?" should produce a complete parse, not another clarification.
- If the user changes the subject (asks something unrelated to tee \
times), discard the prior partial and treat the new message as a fresh \
request.

EXAMPLES:

User SMS: "tee time for 2 saturday afternoon"
→ date: <next Saturday>, players: 2, window: "afternoon", \
holes: null, courses: null, needs_clarification: false

User SMS: "tomorrow at westminster"
→ date: <tomorrow>, players: null, courses: ["westminster"], \
needs_clarification: true, \
clarification_message: "Got it — Westminster tomorrow. \
How many players?"

User SMS: "9 hole round at riverdale this weekend for 4"
→ needs_clarification: true, \
clarification_message: "Riverdale has two courses (Dunes and Knolls), \
and 'this weekend' could be Saturday or Sunday — which day and which \
course?"

User SMS: "tee time"
→ needs_clarification: true, \
clarification_message: "I need a few details — what date, time of day, \
and how many players?"
"""


def parse(
    text: str,
    *,
    today: date_cls,
    course_display_names: dict[str, str],
    course_areas: dict[str, str] | None = None,
    previous: ParsedSearch | None = None,
    last_search: ParsedSearch | None = None,
    has_location_default: bool = False,
    client: anthropic.Anthropic | None = None,
    model: str = MODEL_ID,
) -> ParsedSearch:
    """Parse an SMS message into a structured search request.

    `course_display_names` maps slug → human display name and is rendered
    into the cached system prompt. Mutating this dict invalidates the
    cache on the next call.

    `previous` carries forward fields the user supplied in earlier dialog
    turns. The model sees any non-null fields from the prior partial and
    merges them with the new message — a bare "2" after "walnut saturday
    morning" produces a complete search rather than another clarification.

    Caching: the system prompt is `cache_control`-marked, so subsequent
    calls within the (default ~5 min) TTL pay the read price (~10% of
    input cost) instead of the full cost. Today's date and the prior
    partial are placed in the user turn so they don't affect the prefix.
    """
    client = client or anthropic.Anthropic()

    if has_location_default:
        location_clarification = (
            "- The user has profile defaults (favorite courses or proximity). "
            "Do NOT ask for location — leave `courses` null and the search layer will apply their defaults."
        )
    else:
        location_clarification = (
            "- If the user did NOT mention any course name or area, set "
            "`needs_clarification=true` and ask: "
            "\"Where's your usual spot? Name a course, a part of town (northwest, northeast, "
            "southwest, southeast), or say 'anywhere' if you don't give a shit.\"\n"
            "- If they say 'anywhere', 'any', 'don't care', 'idgaf', or similar "
            "→ leave `courses` null and proceed.\n"
            "- Do NOT ask for location if they already specified a course or area."
        )

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        course_list=_render_course_list(course_display_names, course_areas),
        location_clarification=location_clarification,
    )

    # Volatile content (today's date, prior partial, user message) belongs
    # in the user turn AFTER the cached system prompt — see prompt-caching
    # guidance.
    user_lines = [f"Today's date: {today.isoformat()}"]
    if last_search is not None:
        last_data = last_search.model_dump(
            mode="json",
            exclude={"needs_clarification", "clarification_message", "is_refinement"},
        )
        last_filled = {k: v for k, v in last_data.items() if v is not None}
        if last_filled:
            user_lines.append(
                "Last completed search (use for follow-up refinements): "
                + json.dumps(last_filled)
            )
    if previous is not None:
        # Strip the dialog-control fields and null values; show the model
        # only what's actually been gathered.
        prior_data = previous.model_dump(
            mode="json",
            exclude={"needs_clarification", "clarification_message"},
        )
        prior_filled = {k: v for k, v in prior_data.items() if v is not None}
        if prior_filled:
            user_lines.append(
                "Previous partial parse (carry forward unless overridden): "
                + json.dumps(prior_filled)
            )
    user_lines.append(f"User SMS: {text}")
    user_msg = "\n".join(user_lines)

    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
        output_format=ParsedSearch,
    )

    return response.parsed_output


def _render_course_list(course_display_names: dict[str, str], course_areas: dict[str, str] | None = None) -> str:
    """Render the slug→name→area list block deterministically.

    Sorts by slug to keep the rendered prompt bytes stable across calls —
    non-deterministic ordering would silently kill the prompt cache.
    """
    lines = []
    for slug in sorted(course_display_names):
        area = (course_areas or {}).get(slug)
        area_str = f" (area: {area})" if area else ""
        lines.append(f"- {slug}: {course_display_names[slug]}{area_str}")
    return "\n".join(lines)
