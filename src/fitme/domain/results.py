"""LLM output domain models for the session-logging and recap agents (A§8.1, M4):
`SetResult` (one logged set, from the assistant's `log_exercise_results` tool), and
`Recap`/`PlanChange` (`recap`).

Pure pydantic v2, no I/O — same discipline as `domain/models.py`. Every model forbids extra
fields, and every numeric field rejects NaN/infinity (`allow_inf_nan=False`): an LLM
hallucinating an out-of-range or non-finite number is a validation error at parse time, not
silently-accepted noise a guard has to catch later.

`PlanChange` is deliberately minimal and structural (A§8.1): it names *what* to change
(swap an exercise, change a rep range, a set count or a timed/distance target), never a
load. Loads come only from the deterministic
load engine (`services/loads.py`, A§6.5 step 6, A§7.3) — an LLM-proposed `PlanChange` that
tried to carry a `kg` value would let a model's number reach a plan without ever passing
through a guard, which is exactly what AGENTS.md §2 rules out.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fitme.domain.models import MAX_ACTIVITY_SECONDS, MAX_SET_METERS, MAX_SET_SECONDS

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

MAX_EXTRA_SETS = 5  # ADR 0012: sets the assistant may log beyond one prescription
MAX_SET_INDEX = 10 + MAX_EXTRA_SETS  # `Prescription.sets` is at most 10


class SetResult(BaseModel):
    """One prescribed set's actual outcome, as read from the user's free text by the
    assistant (A§6.5 step 5, ADR 0005) or entered as "according to plan". `set_index` matches the
    prescribed set it reports on; a set the user didn't do is `skipped=True` with no reps or
    load, mirroring `set_logs` (A§4.2: "a set not performed is skipped=1 with actual_* NULL,
    so missing sets are visible") rather than silently omitting it from the list.
    """

    model_config = _STRICT_CONFIG

    set_index: Annotated[int, Field(ge=1, le=MAX_SET_INDEX)]
    reps: Annotated[int, Field(ge=0, le=100)] | None = None
    # ADR 0009: a timed set's result in seconds, a distance set's in meters (instead of
    # `reps`; exactly one of the three for a performed set). Bounded at the activity maximum
    # because a logged activity set (a 90-minute class) is one result; a catalog exercise's
    # logged time is held to `MAX_SET_SECONDS` by `guards.plausibility.check_parsed_seconds`.
    seconds: Annotated[int, Field(ge=0, le=MAX_ACTIVITY_SECONDS)] | None = None
    meters: Annotated[int, Field(ge=0, le=5000)] | None = None
    # gt=0: a real load is always positive. le=500: a coordinator-set plausibility ceiling —
    # no catalog exercise or realistic implement combination reaches this, so a model
    # reporting more is hallucinating, not describing a real (if extreme) lift.
    load_kg: float | None = Field(default=None, gt=0, le=500)  # None: bodyweight/not reported
    skipped: bool = False

    @model_validator(mode="after")
    def _skipped_reports_nothing_else(self) -> SetResult:
        reported = [v for v in (self.reps, self.seconds, self.meters) if v is not None]
        if self.skipped and (reported or self.load_kg is not None):
            raise ValueError("a skipped set must not report reps, seconds, meters or load")
        if not self.skipped and len(reported) != 1:
            raise ValueError(
                "a set that wasn't skipped reports exactly one of reps, seconds or meters"
            )
        return self


class SwapExercise(BaseModel):
    """A `PlanChange`: replace one exercise with another, both by catalog id. The caller
    (M6/M7) is responsible for checking `to_exercise_id` is in the allowed list before
    applying it — this type only carries the *proposal*, it isn't itself a guard verdict."""

    model_config = _STRICT_CONFIG

    kind: Literal["swap_exercise"] = "swap_exercise"
    from_exercise_id: str
    to_exercise_id: str


class ChangeReps(BaseModel):
    """A `PlanChange`: change one exercise's rep range. Carries no load (see module
    docstring)."""

    model_config = _STRICT_CONFIG

    kind: Literal["change_reps"] = "change_reps"
    exercise_id: str
    reps_min: Annotated[int, Field(ge=1, le=50)]
    reps_max: Annotated[int, Field(ge=1, le=50)]

    @model_validator(mode="after")
    def _reps_min_le_reps_max(self) -> ChangeReps:
        if self.reps_min > self.reps_max:
            raise ValueError("reps_min must be <= reps_max")
        return self


class ChangeSets(BaseModel):
    """A `PlanChange` (ADR 0015): change one exercise's number of sets — e.g. one more after
    the user kept doing more than prescribed. Carries no load (see module docstring)."""

    model_config = _STRICT_CONFIG

    kind: Literal["change_sets"] = "change_sets"
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)]


class ChangeTarget(BaseModel):
    """A `PlanChange` (ADR 0016): change one timed or distance exercise's per-set target —
    a longer hold, a longer interval, a longer carry. Exactly one of `seconds` / `meters`,
    matching the prescription's measure, and `from_target` — the seconds or meters it was
    decided from, so a change made stale by a later edit is never applied. Made by
    `services.progression` only, never by a model. Carries no load (see module docstring)."""

    model_config = _STRICT_CONFIG

    kind: Literal["change_target"] = "change_target"
    exercise_id: str
    # A catalog exercise's target only (`services.progression` never changes an activity
    # item), so the catalog bound.
    from_target: Annotated[int, Field(ge=5, le=MAX_SET_SECONDS)]
    seconds: Annotated[int, Field(ge=5, le=MAX_SET_SECONDS)] | None = None
    meters: Annotated[int, Field(ge=5, le=MAX_SET_METERS)] | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> ChangeTarget:
        if (self.seconds is None) == (self.meters is None):
            raise ValueError("a target change sets exactly one of seconds or meters")
        return self


PlanChange = Annotated[
    SwapExercise | ChangeReps | ChangeSets | ChangeTarget, Field(discriminator="kind")
]
# ADR 0016: what the `recap` model may suggest. A timed or distance target is the app's alone
# (`services.progression`), never the model's, so `change_target` isn't in its schema.
ModelPlanChange = Annotated[SwapExercise | ChangeReps | ChangeSets, Field(discriminator="kind")]


class Recap(BaseModel):
    """`recap` agent output (A§8.1, A§6.5 step 6): a short neutral summary of the engine's
    already-computed numbers, in the user's language, plus optional structural suggestions.
    The agent explains the engine's numbers; it never invents or changes them (A§6.5 step 6:
    "The recap agent only writes a short explanation of those numbers")."""

    model_config = _STRICT_CONFIG

    text: Annotated[str, Field(max_length=2000)]
    suggestions: list[ModelPlanChange] = Field(default_factory=list)
