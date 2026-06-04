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

    date: date_cls | None = None
    players: int | None = Field(None, ge=1, le=9)
    window: Window | None = None
    holes: Literal[9, 18] | None = None
    courses: list[str] | None = None
    needs_clarification: bool = False
    clarification_message: str | None = None
    is_refinement: bool = False
    target_time: str | None = None  # specific clock time as "HH:MM" (24h)


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
- `target_time` — specific clock time as "HH:MM" (24h) when user mentions \
an approximate time like "around 4:30", "at 5pm", "before 6". Set the \
appropriate `window` too (e.g. target_time "16:30" → window "afternoon"). \
For an explicit time RANGE ("10am to 3pm", "between noon and 4"), do NOT \
use target_time — just set `window` to the bucket that best covers it \
(the search layer extracts the exact range separately). Leave null if the \
user only gave a named window or no clock time.

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

VAGUE FOLLOW-UPS (when a "Last completed search" is in context):
- "Anything else?", "What else?", "Other options?", "Anything?", "Any \
other courses?" → set `needs_clarification=true` with a response like: \
"Whatcha looking for — different time, different course, or different day?"
- Do NOT re-run the same search for these. Ask what they want changed.

FOLLOW-UP REFINEMENTS:
- If the user turn contains a "Last completed search" line, the user \
previously ran a full search with those criteria. Use it as context \
for follow-up messages.
- If the new message is clearly a refinement of that search \
("nothing earlier?", "what about morning?", "try the southwest courses", \
"change to afternoon", "any 9-hole options?", "how about tomorrow instead?") \
→ set `is_refinement=true`, carry forward all non-overridden fields from \
the last search, and apply the change. For a refinement ONLY, do NOT ask \
for location again if the last search already had a course/area preference \
— carry it forward.
- If the user has an active watch and says something like "change it to \
afternoon" or "watch southwest instead" → set `is_refinement=true` and \
update the relevant criteria field.
- A message that states BOTH a date AND a party size on its own (e.g. \
"saturday for 4", "tee time tuesday 2 players", "saturday 9am-3pm for 4") \
is a fresh standalone search, NOT a refinement → `is_refinement=false`. \
Do NOT inherit courses/area from the last search. If this message does \
not itself mention a course, area, drive time, or "anywhere"/"usual", you \
MUST apply the AREA / COURSE CLARIFICATION rule above and ask the location \
question — even though a prior search exists. The presence of a "Last \
completed search" line does NOT make a self-contained request a refinement.
- Only treat a message as a refinement when it is elliptical — i.e. it \
leaves out the date or the party size and only makes sense relative to the \
last search ("nothing earlier?", "how about morning?", "any 9-hole \
options?", "try the southwest courses").

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
    location_defaults_label: str | None = None,
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

    if location_defaults_label is not None:
        location_clarification = (
            f"- The user has saved profile defaults: {location_defaults_label}. "
            "If they did NOT mention a course name or area in their message, set "
            "`needs_clarification=true` and ask exactly: "
            f"\"Your usual spots ({location_defaults_label}), somewhere new, within a drive, or anywhere — what's it gonna be?\"\n"
            "- If they reply 'usual', 'favorites', 'regular', 'my usual', 'same as always', "
            "or similar → leave `courses` null (profile defaults will apply) and proceed.\n"
            "- If they say 'anywhere', 'any', 'don't care', 'idgaf', or similar "
            "→ leave `courses` null and proceed.\n"
            "- If they mention a drive time ('within 30 minutes', '45 min drive', 'close by', "
            "'under an hour') → leave `courses` null (the search layer will filter by drive time "
            "from their zip) and proceed.\n"
            "- If they name a course or area → use those slugs.\n"
            "- Do NOT ask for location if they already specified a course or area."
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
