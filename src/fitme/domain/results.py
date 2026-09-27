"""LLM output domain models for the session-logging and recap agents (A§8.1, M4):
`ParsedResults`/`SetResult` (`result_parse`), and `Recap`/`PlanChange` (`recap`).

Pure pydantic v2, no I/O — same discipline as `domain/models.py`. Every model forbids extra
fields, and every numeric field rejects NaN/infinity (`allow_inf_nan=False`): an LLM
hallucinating an out-of-range or non-finite number is a validation error at parse time, not
silently-accepted noise a guard has to catch later.

`PlanChange` is deliberately minimal and structural (A§8.1): it names *what* to change
(swap an exercise, change a rep range), never a load. Loads come only from the deterministic
load engine (`services/loads.py`, A§6.5 step 6, A§7.3) — an LLM-proposed `PlanChange` that
tried to carry a `kg` value would let a model's number reach a plan without ever passing
through a guard, which is exactly what AGENTS.md §2 rules out.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)


class SetResult(BaseModel):
    """One prescribed set's actual outcome, as parsed from the user's free text (A§6.5 step
    5, `result_parse` agent) or entered as "according to plan". `set_index` matches the
    prescribed set it reports on; a set the user didn't do is `skipped=True` with no reps or
    load, mirroring `set_logs` (A§4.2: "a set not performed is skipped=1 with actual_* NULL,
    so missing sets are visible") rather than silently omitting it from the list.
    """

    model_config = _STRICT_CONFIG

    set_index: Annotated[int, Field(ge=1, le=10)]
    reps: Annotated[int, Field(ge=0, le=100)] | None = None
    # gt=0: a real load is always positive. le=500: a coordinator-set plausibility ceiling —
    # no catalog exercise or realistic implement combination reaches this, so a model
    # reporting more is hallucinating, not describing a real (if extreme) lift.
    load_kg: float | None = Field(default=None, gt=0, le=500)  # None: bodyweight/not reported
    skipped: bool = False

    @model_validator(mode="after")
    def _skipped_reports_nothing_else(self) -> SetResult:
        if self.skipped and (self.reps is not None or self.load_kg is not None):
            raise ValueError("a skipped set must not report reps or load")
        if not self.skipped and self.reps is None:
            raise ValueError("reps is required for a set that wasn't skipped")
        return self


class ParsedResults(BaseModel):
    """`result_parse` agent output (A§8.1). `safety_signal` is set whenever the free text
    reports pain, dizziness, numbness, chest discomfort, a popped/snapped event, trouble
    breathing or fainting (prompts/result_parse.v1.md) — it can only *add* a halt on top of
    the deterministic stop-word guard (A§7.2), never clear one. `unclear` means the model
    couldn't confidently parse the text into `sets`; `result_parse` never escalates on this
    (A§8.5 rule 3) — the caller re-asks the user instead.
    """

    model_config = _STRICT_CONFIG

    sets: list[SetResult]
    safety_signal: bool
    unclear: bool


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


PlanChange = Annotated[SwapExercise | ChangeReps, Field(discriminator="kind")]


class Recap(BaseModel):
    """`recap` agent output (A§8.1, A§6.5 step 6): a short neutral summary of the engine's
    already-computed numbers, in the user's language, plus optional structural suggestions.
    The agent explains the engine's numbers; it never invents or changes them (A§6.5 step 6:
    "The recap agent only writes a short explanation of those numbers")."""

    model_config = _STRICT_CONFIG

    text: Annotated[str, Field(max_length=2000)]
    suggestions: list[PlanChange] = Field(default_factory=list)
