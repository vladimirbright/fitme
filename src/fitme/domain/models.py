"""Plan/prescription domain models (A§4.5). Pure pydantic v2, no I/O.

These are the LLM output types (`PlanProposal`), the shape of `plan_versions.body`, and what
`guards.plan.validate_plan` checks. Every model forbids extra fields: an LLM hallucinating an
unexpected key is a validation error at parse time, not silently-ignored noise.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from fitme.domain.enums import RefusalCode

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


class Prescription(BaseModel):
    """One exercise's sets/reps/load within a `Block` (A§4.5)."""

    model_config = _STRICT_CONFIG

    exercise_id: str  # must exist in the catalog; checked by guards.plan.validate_plan
    sets: Annotated[int, Field(ge=1, le=10)]
    reps_min: Annotated[int, Field(ge=1, le=50)]
    reps_max: Annotated[int, Field(ge=1, le=50)]
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
    def _reps_min_le_reps_max(self) -> Prescription:
        if self.reps_min > self.reps_max:
            raise ValueError("reps_min must be <= reps_max")
        return self


class Block(BaseModel):
    """One exercise (`single`) or a superset of 2-4 exercises (A§4.5)."""

    model_config = _STRICT_CONFIG

    kind: Literal["single", "superset"]
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
    title: Annotated[str, BeforeValidator(_trim_title)]
    blocks: list[Block]


class ScheduledDay(BaseModel):
    model_config = _STRICT_CONFIG

    weekday: Annotated[int, Field(ge=0, le=6)]
    workout_key: str


class Plan(BaseModel):
    model_config = _STRICT_CONFIG

    name: Annotated[str, BeforeValidator(_trim_name)]
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
