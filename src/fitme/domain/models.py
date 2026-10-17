"""Plan/prescription domain models (A§4.5). Pure pydantic v2, no I/O.

These are the LLM output types (`PlanProposal`), the shape of `plan_versions.body`, and what
`guards.plan.validate_plan` checks. Every model forbids extra fields: an LLM hallucinating an
unexpected key is a validation error at parse time, not silently-ignored noise.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from fitme.domain.enums import BlockRole, Location, Measure, Quality, RefusalCode

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

# Caps on model-authored display text (plan name, workout title, prescription note). These
# used to be a hard `Field(max_length=...)`, rejecting the whole structured output — plan or
# workout — on one over-long field. A pasted real-world program (especially translated/
# transliterated Russian) routinely produces a name or title past the cap, and pydantic-ai's
# retry budget is shared across the whole run: two such rejections in a row exhausted it and
# turned an otherwise-fine plan into a bare `LLM_UNAVAILABLE` refusal. `_trim_to_limit` below
# trims instead, so a display-text length quirk can no longer sink a structurally valid plan.
NAME_MAX_LENGTH = 60
TITLE_MAX_LENGTH = 60
ACTIVITY_NAME_MAX_LENGTH = 60
NOTE_MAX_LENGTH = 200
# One pasted-plan exercise name the import agent couldn't map (`PlanImport.unmatched`): shown
# back to the user once, never stored in a plan body.
UNMATCHED_NAME_MAX_LENGTH = 60
UNMATCHED_MAX_COUNT = 30

# A§6.5.1 absolute load bounds, applied always (calibration included): no catalog exercise is
# realistically loaded beyond these, so a larger number is a typo, never a lift. Defined here
# (not in `guards/plausibility.py`, which imports them from here) so the domain model can bound
# `Prescription.declared_kg` without importing `guards` (A§2.1 dependency direction).
MAX_TOTAL_KG = 300.0
MAX_IMPLEMENT_KG = 60.0  # per dumbbell / kettlebell


def _trim_to_limit(value: object, limit: int) -> object:
    """`BeforeValidator` body shared by `name`/`title`/`note`: strip surrounding whitespace
    and truncate to `limit` characters, appending "…" when truncation actually happened, so
    the result is always <= `limit` characters. Non-`str` values (including `None`, for the
    optional `note` field) pass through unchanged — the ordinary type validation that runs
    right after still rejects a non-string/non-None value, and does so on the type, not on
    something this helper invented."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _trim_name(value: object) -> object:
    return _trim_to_limit(value, NAME_MAX_LENGTH)


def _trim_title(value: object) -> object:
    return _trim_to_limit(value, TITLE_MAX_LENGTH)


def _trim_activity_name(value: object) -> object:
    return _trim_to_limit(value, ACTIVITY_NAME_MAX_LENGTH)


def _trim_note(value: object) -> object:
    return _trim_to_limit(value, NOTE_MAX_LENGTH)


def _trim_unmatched(value: object) -> object:
    return _trim_to_limit(value, UNMATCHED_NAME_MAX_LENGTH)


def _cap_unmatched(value: object) -> object:
    """Keep at most `UNMATCHED_MAX_COUNT` entries (a runaway list is a display problem, not
    a reason to reject the whole transcription). Non-lists pass through to type validation."""
    if isinstance(value, list) and len(value) > UNMATCHED_MAX_COUNT:
        return value[:UNMATCHED_MAX_COUNT]
    return value


def plausible_declared_kg(value: object, *, per_implement: bool = False) -> float | None:
    """`Prescription.declared_kg` is untrusted display data (M8b: what the user's pasted plan
    said, transcribed by a model). Keep it only when it is a finite number, > 0 and within
    the absolute bound (`MAX_IMPLEMENT_KG` for a per-implement/single-implement load,
    `MAX_TOTAL_KG` otherwise); anything else is dropped (`None`), never an error. Pure: the
    guards and the load engine never read this value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    kg = float(value)
    bound = MAX_IMPLEMENT_KG if per_implement else MAX_TOTAL_KG
    if not math.isfinite(kg) or kg <= 0 or kg > bound:
        return None
    return kg


def _drop_implausible_declared_kg(value: object) -> object:
    """`BeforeValidator` for `Prescription.declared_kg`: the exercise-independent bound
    (`MAX_TOTAL_KG`); the caller that knows the catalog `load_unit` applies the tighter
    per-implement bound through `plausible_declared_kg` before storing."""
    if value is None:
        return None
    return plausible_declared_kg(value)


class Load(BaseModel):
    """An explicit load shown to the user (A§4.5, A§6.5): a kg number, "use your
    bodyweight", or "calibration: start here and log what you actually used"."""

    model_config = _STRICT_CONFIG

    kind: Literal["kg", "bodyweight", "calibration"]
    kg: float | None = None  # required iff kind == "kg"; must be > 0

    @model_validator(mode="after")
    def _kg_required_iff_kind_kg(self) -> Load:
        if self.kind == "kg":
            if self.kg is None:
                raise ValueError('Load.kg is required when kind == "kg"')
            if self.kg <= 0:
                raise ValueError("Load.kg must be > 0")
        elif self.kg is not None:
            raise ValueError(f'Load.kg must be None when kind == "{self.kind}"')
        return self


# A timed set may be a whole activity session (a 90-minute class as one set), not only a
# hold or a conditioning interval.
MAX_SET_SECONDS = 10800
MAX_SET_METERS = 2000

# A non-catalog activity item (a class, a run, a walk): `Prescription.exercise_id` in this
# namespace names no catalog exercise; the item carries its own `activity_name` and is always
# one timed bodyweight set block. No catalog id may use this prefix (a test checks it).
ACTIVITY_PREFIX = "activity:"
_ACTIVITY_ID = re.compile(r"activity:[a-z0-9_]{3,60}")


def is_activity_id(exercise_id: str) -> bool:
    """Whether `exercise_id` is a non-catalog activity id (`activity:<slug>`, slug 3..60
    characters of `a-z`, `0-9`, `_`)."""
    return _ACTIVITY_ID.fullmatch(exercise_id) is not None


def activity_fallback_name(exercise_id: str) -> str:
    """A readable name for an activity id where its item's `activity_name` isn't at hand
    (a logged set, a recap line): the slug in words ("activity:bjj_class" → "Bjj class"),
    so the raw id is never shown."""
    slug = exercise_id.removeprefix(ACTIVITY_PREFIX).replace("_", " ").strip()
    return slug[:1].upper() + slug[1:] if slug else exercise_id


class Prescription(BaseModel):
    """One exercise's sets and per-set target within a `Block` (A§4.5). ADR 0009: a set is
    prescribed in exactly one `measure` — `reps` (reps_min..reps_max), `time` (`seconds`) or
    `distance` (`meters`) — and the matching field is required while the others stay empty.
    Bodies stored before ADR 0009 have no `measure` and parse as `reps`."""

    model_config = _STRICT_CONFIG

    # A catalog id (checked by guards.plan.validate_plan), or an `activity:<slug>` id for a
    # non-catalog activity (`is_activity_id`), which then needs `activity_name`.
    exercise_id: str
    # Display name of an activity item ("BJJ class"); `None` for every catalog exercise.
    activity_name: Annotated[str, BeforeValidator(_trim_activity_name)] | None = None
    sets: Annotated[int, Field(ge=1, le=10)]
    measure: Measure = Measure.REPS
    reps_min: Annotated[int, Field(ge=1, le=50)] | None = None
    reps_max: Annotated[int, Field(ge=1, le=50)] | None = None
    seconds: Annotated[int, Field(ge=5, le=MAX_SET_SECONDS)] | None = None
    meters: Annotated[int, Field(ge=5, le=MAX_SET_METERS)] | None = None
    # ADR 0009: target reps in reserve (structured intensity), mostly with a `calibration`
    # load: "pick a weight that leaves about this many reps in reserve". Optional.
    rir: Annotated[int, Field(ge=0, le=5)] | None = None
    load: Load
    rest_seconds: Annotated[int, Field(ge=0, le=600)]
    # Short cue; no medical language (AGENTS.md §3). Model-authored display text is trimmed
    # to the cap here (never rejected for length) and wording-checked before display
    # (`fitme.i18n.wording`).
    note: Annotated[str, BeforeValidator(_trim_note)] | None = None
    # M8b, display only: the kg the user's *pasted* plan declared for this prescription, kept
    # as a hint next to a `calibration` load ("your plan says 80 kg; start at or below it and
    # log what you used"). Never read by the guards or the load engine — the prescribed load
    # is `load`, full stop. Untrusted: out-of-range values are dropped at parse time
    # (`_drop_implausible_declared_kg`), and `services.planning` only ever sets it from a
    # pasted plan's own numbers, never from a model's free choice.
    declared_kg: Annotated[float | None, BeforeValidator(_drop_implausible_declared_kg)] = None

    @model_validator(mode="after")
    def _target_matches_measure(self) -> Prescription:
        has_reps = self.reps_min is not None or self.reps_max is not None
        if self.measure == Measure.REPS:
            if self.reps_min is None or self.reps_max is None:
                raise ValueError("measure 'reps' needs reps_min and reps_max")
            if self.reps_min > self.reps_max:
                raise ValueError("reps_min must be <= reps_max")
            if self.seconds is not None or self.meters is not None:
                raise ValueError("measure 'reps' takes no seconds or meters")
        elif self.measure == Measure.TIME:
            if self.seconds is None:
                raise ValueError("measure 'time' needs seconds")
            if has_reps or self.meters is not None:
                raise ValueError("measure 'time' takes no reps or meters")
        else:
            if self.meters is None:
                raise ValueError("measure 'distance' needs meters")
            if has_reps or self.seconds is not None:
                raise ValueError("measure 'distance' takes no reps or seconds")
        return self

    @model_validator(mode="after")
    def _activity_shape(self) -> Prescription:
        if not is_activity_id(self.exercise_id):
            if self.activity_name is not None:
                raise ValueError("activity_name is only for an activity: item")
            return self
        if not self.activity_name:
            raise ValueError("an activity: item needs activity_name")
        if self.measure != Measure.TIME:
            raise ValueError("an activity: item is measured in time")
        if self.load.kind != "bodyweight":
            raise ValueError("an activity: item takes a bodyweight load")
        if self.rir is not None:
            raise ValueError("an activity: item takes no rir")
        if self.declared_kg is not None:
            raise ValueError("an activity: item takes no declared_kg")
        return self


class Block(BaseModel):
    """One exercise (`single`) or a superset of 2-4 exercises (A§4.5). ADR 0009: a superset
    is done in rounds, its exercises in the listed order; each exercise's `rest_seconds` is
    the transition to the next one, and the last exercise's is the rest after the round
    (and before the next block) — the one authoritative round rest, shown once.

    ADR 0011: `role` is what the block does in its session (warm-up, power, main strength,
    ...). It comes before `items` so a model fills it before choosing the exercises; it is
    optional, and plans made before it have none."""

    model_config = _STRICT_CONFIG

    kind: Literal["single", "superset"]
    role: BlockRole | None = None
    items: list[Prescription]

    @model_validator(mode="after")
    def _item_count_matches_kind(self) -> Block:
        count = len(self.items)
        if self.kind == "single" and count != 1:
            raise ValueError(f"a single block must have exactly 1 item, got {count}")
        if self.kind == "superset" and not (2 <= count <= 4):
            raise ValueError(f"a superset block must have 2-4 items, got {count}")
        return self


class Workout(BaseModel):
    model_config = _STRICT_CONFIG

    key: str  # "A", "B", ...
    # ADR 0009: what this session is for, as the planner states it, main objective first;
    # checked against the exercises' own qualities (`services.plan_quality`). Empty for plans
    # made before it. ADR 0011: declared before `title` and `blocks`, so a model states the
    # session's objectives before it picks exercises (structured output follows field order).
    qualities: list[Quality] = Field(default_factory=list, max_length=4)
    title: Annotated[str, BeforeValidator(_trim_title)]
    blocks: list[Block]


class ScheduledDay(BaseModel):
    model_config = _STRICT_CONFIG

    weekday: Annotated[int, Field(ge=0, le=6)]
    workout_key: str


class Plan(BaseModel):
    model_config = _STRICT_CONFIG

    name: Annotated[str, BeforeValidator(_trim_name)]
    # Where this plan is trained, when it differs from the profile's location (an outdoor
    # running plan for a gym profile). The guards judge the plan's items here; `None` (every
    # plan made before it) means the profile's location.
    location: Location | None = None
    schedule: list[ScheduledDay]
    workouts: list[Workout]


class LoadChange(BaseModel):
    """One exercise's load change, recorded verbatim in `decisions.load_changes` (A§4.3,
    AGENTS.md §6). `from_kg`/`to_kg` are absolute working weights, both required and
    positive: a change *to* or *from* a non-kg (`bodyweight`/`calibration`) load isn't a
    tracked "increase" in the weekly-cap sense, so it has no `LoadChange` entry at all."""

    model_config = _STRICT_CONFIG

    exercise_id: str
    from_kg: float = Field(gt=0)
    to_kg: float = Field(gt=0)


class Refusal(BaseModel):
    """A refusal is a first-class plan-generation output (AGENTS.md §2: "Refusal is a valid
    output. The plan generator must be able to return 'no plan' with a reason.")."""

    model_config = _STRICT_CONFIG

    code: RefusalCode
    message: Annotated[str, Field(max_length=1000)]  # plain language, in the user's language


PlanProposal = Plan | Refusal


class PlanImport(BaseModel):
    """M8b `plan_import` agent output: the user's pasted program *transcribed* into a `Plan`,
    plus the exercise names it could not map to an allowed catalog id (`unmatched`) — listed
    back to the user, never invented into the plan. A separate wrapper rather than a field on
    `Plan`, so the stored plan body (`plan_versions.body`), the guards' input and the other
    agents' output schema stay exactly `Plan`; the unmatched names are user-text-derived
    display strings that live only in the draft decision's `proposal` and one message."""

    model_config = _STRICT_CONFIG

    plan: Plan
    unmatched: Annotated[
        list[Annotated[str, BeforeValidator(_trim_unmatched)]], BeforeValidator(_cap_unmatched)
    ] = Field(default_factory=list)


PlanImportProposal = PlanImport | Refusal

# A§8.1 `session_adjust` agent output type: a revised `Workout` for today's session, or a
# `Refusal` (e.g. the request is out of scope, or unsafe on its face). Named separately from
# `PlanProposal` even though the shape (`X | Refusal`) is the same, so `llm/agents.py` and its
# callers read as self-documenting rather than reusing a "plan" name for a single workout.
SessionAdjustProposal = Workout | Refusal
