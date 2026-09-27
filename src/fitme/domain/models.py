"""Plan/prescription domain models (A§4.5). Pure pydantic v2, no I/O.

These are the LLM output types (`PlanProposal`), the shape of `plan_versions.body`, and what
`guards.plan.validate_plan` checks. Every model forbids extra fields: an LLM hallucinating an
unexpected key is a validation error at parse time, not silently-ignored noise.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fitme.domain.enums import RefusalCode

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

# Caps on model-authored display text (plan name, workout title, prescription note): a
# hallucinated essay is a validation error, not a wall of text in a Telegram message.
NAME_MAX_LENGTH = 60
TITLE_MAX_LENGTH = 60
NOTE_MAX_LENGTH = 200


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
    # Short cue; no medical language (AGENTS.md §3). Model-authored display text is capped
    # here and wording-checked before display (`fitme.i18n.wording`).
    note: Annotated[str, Field(max_length=NOTE_MAX_LENGTH)] | None = None

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
    title: Annotated[str, Field(max_length=TITLE_MAX_LENGTH)]
    blocks: list[Block]


class ScheduledDay(BaseModel):
    model_config = _STRICT_CONFIG

    weekday: Annotated[int, Field(ge=0, le=6)]
    workout_key: str


class Plan(BaseModel):
    model_config = _STRICT_CONFIG

    name: Annotated[str, Field(max_length=NAME_MAX_LENGTH)]
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

# A§8.1 `session_adjust` agent output type: a revised `Workout` for today's session, or a
# `Refusal` (e.g. the request is out of scope, or unsafe on its face). Named separately from
# `PlanProposal` even though the shape (`X | Refusal`) is the same, so `llm/agents.py` and its
# callers read as self-documenting rather than reusing a "plan" name for a single workout.
SessionAdjustProposal = Workout | Refusal
