"""Free-text assistant output types (ADR 0003). Pure pydantic v2, no I/O.

The `assistant` agent reads the owner's unprompted message (anything no button or pending
prompt claimed) and answers with exactly one of:

- `AssistantEdits`: a short list of typed edit operations, applied by deterministic code
  (`services.assistant`) after the same guards as the website's plan editor — the model never
  writes anything itself;
- `AssistantAction`: open an existing bot flow (plan list, a plan, a new plan, a plan
  revision, today's workout, stats);
- `AssistantReply`: a short plain answer or a clarifying question;
- `Refusal`: out of scope.

Every model forbids extra fields, like `domain.models`.
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
    _trim_name,
    _trim_title,
)

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

MAX_OPS = 20
REPLY_MAX_LENGTH = 1000
REQUEST_MAX_LENGTH = 1000


class SetPrescription(BaseModel):
    """Change one exercise's numbers in one workout. Omitted fields stay as they are."""

    model_config = _STRICT_CONFIG

    op: Literal["set_prescription"]
    plan_id: int
    workout_key: str
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
    plan_id: int
    workout_key: str
    exercise_id: str
    new_exercise_id: str
    load: Load | None = None


class AddExercise(BaseModel):
    """Append one exercise as a single block at the end of a workout."""

    model_config = _STRICT_CONFIG

    op: Literal["add_exercise"]
    plan_id: int
    workout_key: str
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)]
    reps_min: Annotated[int, Field(ge=1, le=50)]
    reps_max: Annotated[int, Field(ge=1, le=50)]
    load: Load
    rest_seconds: Annotated[int, Field(ge=0, le=600)] = 90


class RemoveExercise(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["remove_exercise"]
    plan_id: int
    workout_key: str
    exercise_id: str


class SetSchedule(BaseModel):
    """Replace the plan's weekly schedule (weekday 0 = Monday)."""

    model_config = _STRICT_CONFIG

    op: Literal["set_schedule"]
    plan_id: int
    days: Annotated[list[ScheduledDay], Field(min_length=1, max_length=7)]


class RenameWorkout(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["rename_workout"]
    plan_id: int
    workout_key: str
    title: Annotated[str, BeforeValidator(_trim_title), Field(min_length=1)]


class RenamePlan(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["rename_plan"]
    plan_id: int
    name: Annotated[str, BeforeValidator(_trim_name), Field(min_length=1)]


class SetDefaultPlan(BaseModel):
    model_config = _STRICT_CONFIG

    op: Literal["set_default_plan"]
    plan_id: int


class FixLoggedSet(BaseModel):
    """Correct one already-logged set of a finished session. `set_number` is 1-based within
    the exercise, as the user counts. `load_kg` is `None` for a bodyweight set."""

    model_config = _STRICT_CONFIG

    op: Literal["fix_logged_set"]
    session_id: int
    exercise_id: str
    set_number: Annotated[int, Field(ge=1, le=10)]
    reps: Annotated[int, Field(ge=1, le=100)]
    load_kg: float | None = None


PlanBodyOp = SetPrescription | SwapExercise | AddExercise | RemoveExercise | SetSchedule
PlanOp = PlanBodyOp | RenameWorkout | RenamePlan | SetDefaultPlan
EditOp = Annotated[PlanOp | FixLoggedSet, Field(discriminator="op")]

PLAN_BODY_OPS = (SetPrescription, SwapExercise, AddExercise, RemoveExercise, SetSchedule)


class AssistantEdits(BaseModel):
    """Edits the owner asked for. One message edits **one** plan, or fixes logged sets of
    **one** session — never both, never several (keeps Undo a single step)."""

    model_config = _STRICT_CONFIG

    ops: Annotated[list[EditOp], Field(min_length=1, max_length=MAX_OPS)]

    @model_validator(mode="after")
    def _one_target(self) -> AssistantEdits:
        plan_ids = {op.plan_id for op in self.ops if not isinstance(op, FixLoggedSet)}
        session_ids = {op.session_id for op in self.ops if isinstance(op, FixLoggedSet)}
        if plan_ids and session_ids:
            raise ValueError("edit either one plan or one session's logged sets, not both")
        if len(plan_ids) > 1 or len(session_ids) > 1:
            raise ValueError("all ops must target the same plan_id (or the same session_id)")
        return self


class AssistantAction(BaseModel):
    """Open an existing bot flow. `plan_id` is needed for `show_plan`/`revise_plan`.
    `request` is the owner's own words: required for `revise_plan` (the rewrite), optional
    for `new_plan` (what the new plan should be; without it the bot asks when plans exist).
    The `*_block` actions apply to the current block of the in-progress workout only."""

    model_config = _STRICT_CONFIG

    action: Literal[
        "show_plans",
        "show_plan",
        "new_plan",
        "revise_plan",
        "train",
        "stats",
        # During an in-progress workout, for the block on screen — exactly the block's
        # buttons (✅ / ✏️ / ⏭). `log_block_results` sends the user's *own* message to the
        # result parser, never a model paraphrase of it.
        "log_block_as_planned",
        "log_block_results",
        "skip_block",
    ]
    plan_id: int | None = None
    request: Annotated[str, Field(max_length=REQUEST_MAX_LENGTH)] | None = None

    @model_validator(mode="after")
    def _arguments_match_action(self) -> AssistantAction:
        if self.action in ("show_plan", "revise_plan") and self.plan_id is None:
            raise ValueError(f"{self.action} needs plan_id")
        if self.action == "revise_plan" and not (self.request or "").strip():
            raise ValueError("revise_plan needs the request text")
        return self


class AssistantReply(BaseModel):
    """A short plain answer, or a clarifying question when the request is ambiguous."""

    model_config = _STRICT_CONFIG

    message: Annotated[str, Field(min_length=1, max_length=REPLY_MAX_LENGTH)]


AssistantProposal = AssistantEdits | AssistantAction | AssistantReply | Refusal

__all__ = [
    "MAX_OPS",
    "NAME_MAX_LENGTH",
    "PLAN_BODY_OPS",
    "TITLE_MAX_LENGTH",
    "AddExercise",
    "AssistantAction",
    "AssistantEdits",
    "AssistantProposal",
    "AssistantReply",
    "EditOp",
    "FixLoggedSet",
    "PlanBodyOp",
    "PlanOp",
    "RemoveExercise",
    "RenamePlan",
    "RenameWorkout",
    "SetDefaultPlan",
    "SetPrescription",
    "SetSchedule",
    "SwapExercise",
]
