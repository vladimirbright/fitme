"""The pseudonymized context sent to the LLM (A§8.2) and the free-text scrubber it shares
with every agent that handles user-typed text (`plan_revise`, `session_adjust`,
`result_parse`).

`UserContext` and `build_user_context` are pure: no I/O, no DB access. `services/` (later
milestones) reads `profiles`, `screening_flags` and history from the DB, resolves the allowed
exercise ids (`services.catalog.available_exercises` + `domain.catalog.
LOCATION_DEFAULT_EQUIPMENT`/the user's own `home_equipment` selection), and passes the results
in here as plain values. `llm/` therefore never imports `db/` or `services/` (A§7.2 layering:
`services` depends on `llm`, not the other way around).

Allowed in a `UserContext` (A§8.2): the internal `user_id`, bucket enums, flag **codes**,
catalog ids, and numbers from `set_logs`. **Never** allowed, and structurally impossible to
pass in: `telegram_user_id`, `chat_id`, a name, or `screening_notes` — none of them has a
field on this type or a parameter on `build_user_context`, and `model_config = extra="forbid"`
means even a caller that tries to smuggle one in via `UserContext.model_validate({...})` gets
a validation error instead of a silently-accepted extra key.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from fitme.domain.enums import (
    AgeBucket,
    BarbellExperience,
    Equipment,
    Experience,
    Focus,
    Location,
    Preference,
    ScreeningFlag,
    WeightBucket,
)
from fitme.domain.models import Block, Plan, Workout

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

# Order matters (A§8.2 `scrub()`): an email's local part would read as a bare @handle once its
# `@domain` half is redacted first, so emails are stripped before handles; URLs (schemed,
# `tg://`, or a bare `label.tld[/path]` domain shape) are stripped before phone numbers, since
# a URL's digits/dots/dashes could otherwise look like one. `[redacted]` deliberately isn't
# itself a valid handle/email/URL/phone shape, so a second pass over already-scrubbed text
# can't re-match it.
#
# Unicode-aware throughout (`\w` under Python's default str-pattern behavior matches letters
# from any script, Cyrillic included), so `иван@почта.рф` and `@иван` are caught the same way
# their Latin-script equivalents are.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
# A URI with an explicit scheme (`https://`, `tg://resolve?domain=...`) or a bare `www.` — the
# unbroken `\S+` after it is deliberate: a URL doesn't contain spaces, so this never runs past
# the actual link into surrounding prose.
_SCHEMED_URL_RE = re.compile(r"(?:https?://|www\.|tg://)\S+", re.IGNORECASE)
# A bare domain-shaped string with no scheme (`t.me/ivan_petrov`, `vk.com/id12345`,
# `ivanov.ru/profile`, `иванов.рф`): one or more `label.` segments followed either by a TLD
# from `_KNOWN_TLDS` (path optional), or by any letters-only 2+ character TLD-shaped suffix
# *with* a `/path` (a link is a link even with an unlisted TLD; an unlisted-TLD suffix with no
# path is too likely to be ordinary punctuation — "Mr.Olympia style", "did 3x10.Next time"). A
# digit right after the dot never matches either branch (`[a-zA-Z]`/the literal TLD strings are
# letters-only), so `42.5kg`/`82.5` survive.
_KNOWN_TLDS = (
    "com",
    "ru",
    "me",
    "org",
    "net",
    "io",
    "info",
    "рф",
    "рус",
    "ua",
    "by",
    "kz",
    "de",
    "uk",
    "co",
    "app",
    "dev",
    "ly",
    "gg",
    "tv",
)
_BARE_DOMAIN_RE = re.compile(
    r"\b[\w-]+(?:\.[\w-]+)*\."
    r"(?:(?:" + "|".join(_KNOWN_TLDS) + r")\b(?:/\S*)?"
    r"|[a-zA-Z]{2,}/\S+)",
    re.UNICODE | re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"@\w{2,32}", re.UNICODE)
# A phone-shaped number:
#  - a leading "+" followed by a run of digits (with -, ., space or parens allowed *within*
#    the run); or
#  - the Russian/parenthesized domestic shape: an optional "8"/"7"/"+7" trunk prefix, an
#    optional area code (parens optional), then three digit groups of 3-3-2-2 (all
#    separators optional, so "8 (916) 123-45-67", "(916) 123-45-67", "8-916-123-45-67" and
#    "916 123 45 67" all match the same way); or
#  - a bare unbroken run of 10+ digits.
# This deliberately does NOT match a load ramp or rep list — "100 100 100 kg", "squat 140 150
# 160 170", "12 12 10 10 8 8 6 6" — because none of those has a group of exactly 3 digits
# followed by two groups of exactly 2, nor 10+ digits with no separator; nor a hyphenated date
# ("2024-05-01") or a comma-separated result ("жим 102,5 кг на 3"), for the same reason.
_PHONE_RE = re.compile(
    r"(?<![\w@.])(?:"
    r"\+\d[\d\-.\s()]{5,}\d"
    r"|(?:(?:\+7|8|7)[\s\-]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}"
    r"|\d{10,}"
    r")(?!\w)"
)

_REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Strip @handles, emails, phone numbers and URLs (schemed, `tg://`, or a bare
    `label.tld[/path]` domain) from free text before it's sent to the LLM (A§8.2). Applied to
    every piece of user-typed text an agent sees: a plan-revise request, a session-adjust
    request, a result-entry message."""
    text = _EMAIL_RE.sub(_REDACTED, text)
    text = _SCHEMED_URL_RE.sub(_REDACTED, text)
    text = _BARE_DOMAIN_RE.sub(_REDACTED, text)
    text = _HANDLE_RE.sub(_REDACTED, text)
    text = _PHONE_RE.sub(_REDACTED, text)
    return text


class FlagSummary(BaseModel):
    """One screening flag's answer, reduced to its **code** (A§8.2) — no free text, no
    clearance detail beyond yes/no/unknown."""

    model_config = _STRICT_CONFIG

    flag: ScreeningFlag
    value: str  # "yes" | "no" | "unknown" (domain.screening.ScreeningFlagState's Literal)


class ExerciseHistorySummary(BaseModel):
    """Per-exercise history (A§8.2): last working load, last reps, historical max. Every
    field is optional — no history for an exercise yet is a normal, common case (A§7.3: the
    first session is calibration, not progression)."""

    model_config = _STRICT_CONFIG

    exercise_id: str
    last_working_load_kg: float | None = Field(default=None, gt=0)
    last_reps: Annotated[int, Field(ge=0, le=100)] | None = None
    historical_max_kg: float | None = Field(default=None, gt=0)


class UserContext(BaseModel):
    """The pseudonymized context every plan/session-generating agent sees (A§8.2). See the
    module docstring for what's deliberately absent."""

    model_config = _STRICT_CONFIG

    user_id: int
    language: str
    age_bucket: AgeBucket
    weight_bucket: WeightBucket
    experience: Experience
    barbell_experience: BarbellExperience
    preferences: list[Preference]
    focus: Focus
    location: Location
    equipment: list[Equipment]
    sessions_per_week: Annotated[int, Field(ge=1, le=6)]
    session_minutes: Annotated[int, Field(ge=1)]
    flags: list[FlagSummary]
    # Catalog ids the LLM may use, already filtered by location/equipment/contraindications
    # (`services.catalog.available_exercises`). Prompts state this is the *only* allowed set.
    allowed_exercise_ids: list[str]
    history: list[ExerciseHistorySummary] = Field(default_factory=list)


def build_user_context(
    *,
    user_id: int,
    language: str,
    age_bucket: AgeBucket,
    weight_bucket: WeightBucket,
    experience: Experience,
    barbell_experience: BarbellExperience,
    preferences: Sequence[Preference],
    focus: Focus,
    location: Location,
    equipment: Sequence[Equipment],
    sessions_per_week: int,
    session_minutes: int,
    flags: Sequence[FlagSummary],
    allowed_exercise_ids: Sequence[str],
    history: Mapping[str, ExerciseHistorySummary] | Sequence[ExerciseHistorySummary] = (),
) -> UserContext:
    """Build a `UserContext` from already-fetched values. Pure — no I/O — and every parameter
    is named explicitly: there's no way to pass a `telegram_user_id`, `chat_id`, name, or
    `screening_notes` through this function, because none of them has a parameter here at
    all (see the module docstring)."""
    history_list = list(history.values()) if isinstance(history, Mapping) else list(history)
    return UserContext(
        user_id=user_id,
        language=language,
        age_bucket=age_bucket,
        weight_bucket=weight_bucket,
        experience=experience,
        barbell_experience=barbell_experience,
        preferences=list(preferences),
        focus=focus,
        location=location,
        equipment=list(equipment),
        sessions_per_week=sessions_per_week,
        session_minutes=session_minutes,
        flags=list(flags),
        allowed_exercise_ids=list(allowed_exercise_ids),
        history=history_list,
    )


@dataclass(frozen=True, slots=True)
class RenderedInput:
    """One rendering path (B2): `text` is the exact `user_prompt` string sent to
    `agent.run(...)`; `payload` is the same content as a plain JSON-safe dict, which
    `decisions.llm_input` stores verbatim (A§4.3) — the two are guaranteed to match because
    `text` is `json.dumps(payload, ...)`, never built separately."""

    text: str
    payload: dict[str, object]


def render_user_prompt(
    context: UserContext | None = None,
    *,
    language: str | None = None,
    request: str | None = None,
    current_plan: Plan | None = None,
    workout: Workout | None = None,
    planned_block: Block | None = None,
    result_text: str | None = None,
    guard_feedback: Sequence[str] | None = None,
    load_units: Mapping[str, str] | None = None,
) -> RenderedInput:
    """Build the one `user_prompt` every agent factory's caller sends (B2: "one rendering
    path"), so `plan_generate`/`plan_revise`/`session_adjust`/`result_parse` never each grow
    their own ad hoc JSON-shaping. The rest are the per-agent extras (A§8.1's "Input" column)
    and are included only when given.

    A§8.2 per-agent minimization: `context` is optional, not automatic — `result_parse`
    doesn't need (and must not get) the allowed-ids list or history, so its caller passes
    `language` directly instead of a full `context` (e.g.
    `render_user_prompt(language=lang, planned_block=block, result_text=text)`). Every other
    agent passes its full `context`, which already carries `language`.

    Every free-text field (`request`, `result_text`) is passed through `scrub()` here,
    unconditionally — a caller can never forget to scrub, because there is no way to reach
    the model with this function without going through it.

    `guard_feedback` (A§6.4 step 4: "re-prompt once with the list of violations") is the
    previous attempt's failing guard details, verbatim — they are machine-built strings
    (catalog ids, rule names, kg numbers), never user text, so they aren't scrubbed.

    `load_units` (A§4.4 "loads are per implement") maps each exercise id in `planned_block`
    to its catalog `load_unit`, so `result_parse` knows whether a reported kg is a total, per
    implement (each dumbbell/kettlebell), or a single implement.
    """
    payload: dict[str, object] = {}
    if context is not None:
        payload["context"] = context.model_dump(mode="json")
    elif language is not None:
        payload["language"] = language
    else:
        raise ValueError("render_user_prompt needs either context or an explicit language")
    if current_plan is not None:
        payload["current_plan"] = current_plan.model_dump(mode="json")
    if workout is not None:
        payload["workout"] = workout.model_dump(mode="json")
    if planned_block is not None:
        payload["planned_block"] = planned_block.model_dump(mode="json")
    if request is not None:
        payload["user_request"] = scrub(request)
    if result_text is not None:
        payload["result_text"] = scrub(result_text)
    if guard_feedback is not None:
        payload["guard_feedback"] = list(guard_feedback)
    if load_units is not None:
        payload["load_units"] = dict(load_units)
    text = json.dumps(payload, ensure_ascii=False)
    return RenderedInput(text=text, payload=payload)
