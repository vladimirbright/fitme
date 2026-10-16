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
    MAX_SET_METERS,
    MAX_SET_SECONDS,
    NAME_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    Load,
    Refusal,
    ScheduledDay,
    _trim_title,
)

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)
MAX_SESSION_SET_NUMBER = 200  # a sanity bound only

MAX_OPS = 20
MESSAGE_MAX_LENGTH = 2000


class SetPrescription(BaseModel):
    """Change one exercise's numbers. Omitted fields stay as they are. ADR 0009: giving
    `seconds` makes the sets timed, `meters` makes them distance sets, and reps make them
    reps sets again (the other measure's target is dropped)."""

    model_config = _STRICT_CONFIG

    op: Literal["set_prescription"]
    workout_key: str | None = None
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)] | None = None
    reps_min: Annotated[int, Field(ge=1, le=50)] | None = None
    reps_max: Annotated[int, Field(ge=1, le=50)] | None = None
    seconds: Annotated[int, Field(ge=5, le=MAX_SET_SECONDS)] | None = None
    meters: Annotated[int, Field(ge=5, le=MAX_SET_METERS)] | None = None
    load: Load | None = None
    rest_seconds: Annotated[int, Field(ge=0, le=600)] | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> SetPrescription:
        fields = (
            self.sets,
            self.reps_min,
            self.reps_max,
            self.seconds,
            self.meters,
            self.load,
            self.rest_seconds,
        )
        if all(value is None for value in fields):
            raise ValueError("set_prescription must change at least one field")
        has_reps = self.reps_min is not None or self.reps_max is not None
        if sum((has_reps, self.seconds is not None, self.meters is not None)) > 1:
            raise ValueError("give reps, seconds or meters, not more than one of them")
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
    # ADR 0009: exactly one target — reps_min/reps_max, seconds or meters.
    reps_min: Annotated[int, Field(ge=1, le=50)] | None = None
    reps_max: Annotated[int, Field(ge=1, le=50)] | None = None
    seconds: Annotated[int, Field(ge=5, le=MAX_SET_SECONDS)] | None = None
    meters: Annotated[int, Field(ge=5, le=MAX_SET_METERS)] | None = None
    load: Load
    rest_seconds: Annotated[int, Field(ge=0, le=600)] = 90

    @model_validator(mode="after")
    def _one_target(self) -> AddExercise:
        has_reps = self.reps_min is not None and self.reps_max is not None
        partial_reps = (self.reps_min is None) != (self.reps_max is None)
        if partial_reps or sum((has_reps, self.seconds is not None, self.meters is not None)) != 1:
            raise ValueError("give reps_min and reps_max, or seconds, or meters")
        return self


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
    # Counted across the session (an exercise can be in several blocks, each with sets done
    # beyond the prescription, ADR 0012); `services.log_edit` refuses a number with no set.
    set_number: Annotated[int, Field(ge=1, le=MAX_SESSION_SET_NUMBER)]
    reps: Annotated[int, Field(ge=1, le=100)]
    load_kg: float | None = None


class AssistantTurn(BaseModel):
    """What the assistant says to the owner after this turn: what it did, what it couldn't,
    or a question. The effects of its tools are shown separately, built from the data."""

    model_config = _STRICT_CONFIG

    # Empty when the tools' effects say it all (assistant.v10: a turn that only logged sets;
    # the app shows what was written).
    message: Annotated[str, Field(max_length=MESSAGE_MAX_LENGTH)]


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
