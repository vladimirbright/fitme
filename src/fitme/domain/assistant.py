"""Free-text assistant types (ADR 0003, ADR 0004). Pure pydantic v2, no I/O.

The `assistant` agent works with tools (`llm.agents.AssistantTools`): read-only lookups, and
*staging* tools that check an action against the current state right away and record it for
after the run. Its final output is only what it says to the owner: `AssistantTurn`, or a
`Refusal`. Nothing it stages is written before deterministic code applies it through the
guards (`services.assistant`).

The edit operations below are the arguments of the staging tools. A plan edit names the
workout (`workout_key`); an edit of today's workout during a workout doesn't need one.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from fitme.domain.models import (
    NAME_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    Load,
    Refusal,
    ScheduledDay,
    _trim_title,
)

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

MAX_OPS = 20
MESSAGE_MAX_LENGTH = 2000


class SetPrescription(BaseModel):
    """Change one exercise's numbers. Omitted fields stay as they are."""

    model_config = _STRICT_CONFIG

    op: Literal["set_prescription"]
    workout_key: str | None = None
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)] | None = None
    reps_min: Annotated[int, Field(ge=1, le=50)] | None = None
    reps_max: Annotated[int, Field(ge=1, le=50)] | None = None
    load: Load | None = None
    rest_seconds: Annotated[int, Field(ge=0, le=600)] | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> SetPrescription:
        fields = (self.sets, self.reps_min, self.reps_max, self.load, self.rest_seconds)
        if all(value is None for value in fields):
            raise ValueError("set_prescription must change at least one field")
        return self


class SwapExercise(BaseModel):
    """Replace one exercise with another, keeping sets/reps/rest. The load is reset to
    `calibration` unless `load` is given (a different exercise has a different reference)."""

    model_config = _STRICT_CONFIG

    op: Literal["swap_exercise"]
    workout_key: str | None = None
    exercise_id: str
    new_exercise_id: str
    load: Load | None = None


class AddExercise(BaseModel):
    """Append one exercise as a single block at the end of a workout."""

    model_config = _STRICT_CONFIG

    op: Literal["add_exercise"]
    workout_key: str | None = None
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)]
    reps_min: Annotated[int, Field(ge=1, le=50)]
    reps_max: Annotated[int, Field(ge=1, le=50)]
    load: Load
    rest_seconds: Annotated[int, Field(ge=0, le=600)] = 90


class RemoveExercise(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["remove_exercise"]
    workout_key: str | None = None
    exercise_id: str


class SetSchedule(BaseModel):
    """Replace the plan's weekly schedule (weekday 0 = Monday)."""

    model_config = _STRICT_CONFIG

    op: Literal["set_schedule"]
    days: Annotated[list[ScheduledDay], Field(min_length=1, max_length=7)]


class RenameWorkout(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["rename_workout"]
    workout_key: str | None = None
    title: Annotated[str, BeforeValidator(_trim_title), Field(min_length=1)]


AnyWorkoutOp = SetPrescription | SwapExercise | AddExercise | RemoveExercise
AnyPlanOp = AnyWorkoutOp | SetSchedule | RenameWorkout
WorkoutOp = Annotated[AnyWorkoutOp, Field(discriminator="op")]
PlanOp = Annotated[AnyPlanOp, Field(discriminator="op")]


class FixLoggedSet(BaseModel):
    """Correct one already-logged set of a finished session. `set_number` is 1-based within
    the exercise, as the user counts. `load_kg` is `None` to keep the logged kg (or for a
    bodyweight set)."""

    model_config = _STRICT_CONFIG

    exercise_id: str
    set_number: Annotated[int, Field(ge=1, le=10)]
    reps: Annotated[int, Field(ge=1, le=100)]
    load_kg: float | None = None


class AssistantTurn(BaseModel):
    """What the assistant says to the owner after this turn: what it did, what it couldn't,
    or a question. The effects of its tools are shown separately, built from the data."""

    model_config = _STRICT_CONFIG

    message: Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_LENGTH)]


AssistantOutput = AssistantTurn | Refusal

__all__ = [
    "MAX_OPS",
    "NAME_MAX_LENGTH",
    "TITLE_MAX_LENGTH",
    "AddExercise",
    "AnyPlanOp",
    "AnyWorkoutOp",
    "AssistantOutput",
    "AssistantTurn",
    "FixLoggedSet",
    "PlanOp",
    "RemoveExercise",
    "RenameWorkout",
    "SetPrescription",
    "SetSchedule",
    "SwapExercise",
    "WorkoutOp",
]
