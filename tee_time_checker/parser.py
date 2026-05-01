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

CONFIGURED COURSES (slug → display name):
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

NON-SEARCH MESSAGES:
- Greetings, thanks, random text → set `needs_clarification=true` with: \
"I help find golf tee times. Try: 'tee time tomorrow afternoon for 2'."
- Don't try to extract anything from non-search messages.

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
    client: anthropic.Anthropic | None = None,
    model: str = MODEL_ID,
) -> ParsedSearch:
    """Parse an SMS message into a structured search request.

    `course_display_names` maps slug → human display name and is rendered
    into the cached system prompt. Mutating this dict invalidates the
    cache on the next call.

    Caching: the system prompt is `cache_control`-marked, so subsequent
    calls within the (default ~5 min) TTL pay the read price (~10% of
    input cost) instead of the full cost. Today's date is intentionally
    placed in the user turn so it doesn't affect the cached prefix.
    """
    client = client or anthropic.Anthropic()

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        course_list=_render_course_list(course_display_names)
    )

    # Volatile content (today's date, user message) belongs in the user
    # turn, AFTER the cached system prompt — see prompt-caching guidance.
    user_msg = f"Today's date: {today.isoformat()}\n\nUser SMS: {text}"

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


def _render_course_list(course_display_names: dict[str, str]) -> str:
    """Render the slug→name list block deterministically.

    Sorts by slug to keep the rendered prompt bytes stable across calls —
    non-deterministic ordering would silently kill the prompt cache.
    """
    return "\n".join(
        f"- {slug}: {course_display_names[slug]}"
        for slug in sorted(course_display_names)
    )
