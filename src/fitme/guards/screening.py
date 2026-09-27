"""A§7 / AGENTS.md §2 "Screening before first plan": clearance for red flags and
`other_unlisted`, open health holds, and per-exercise contraindications."""

from __future__ import annotations

from collections.abc import Sequence

from fitme.domain.catalog import Exercise
from fitme.domain.enums import NEEDS_CLEARANCE_FLAGS, RED_FLAGS, HealthHoldReason
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.screening import ScreeningFlagState

_PLAN_RULE = "screening.plan_allowed"
_INCOMPLETE_RULE = "screening.incomplete"
_EXERCISE_RULE = "screening.exercise_allowed"

# Stable order for the completeness check, so the guard's first-found detail is deterministic
# rather than depending on `flags`' incoming order (which reflects DB row order, not this
# check's business logic).
_RED_FLAGS_IN_ORDER = sorted(RED_FLAGS, key=lambda flag: flag.value)


def plan_allowed(
    flags: Sequence[ScreeningFlagState], holds: Sequence[HealthHoldReason]
) -> GuardVerdict:
    """`holds` is the user's currently *open* health holds (already filtered by the caller,
    e.g. `selectors.training.list_open_health_holds`); any open hold refuses outright (A§6.6)
    without even looking at `flags`.

    Every red flag (AGENTS.md §2 "Silence is not consent") must have an explicit `yes`/`no`
    answer: one missing from `flags` entirely, or present with `value == "unknown"`, refuses
    with `RefusalCode.SCREENING_INCOMPLETE` (B4) — a missing answer is never treated as "no".
    Only once every red flag is answered does clearance matter: any red flag or
    `other_unlisted` answered "yes" without `clearance == "yes"` refuses (A§5.6).
    """
    if holds:
        return GuardVerdict(
            rule=_PLAN_RULE,
            ok=False,
            detail=f"{len(holds)} open health hold(s) block plan generation",
        )

    answered = {state.flag: state for state in flags}
    for red_flag in _RED_FLAGS_IN_ORDER:
        state = answered.get(red_flag)
        if state is None or state.value == "unknown":
            return GuardVerdict(
                rule=_INCOMPLETE_RULE,
                ok=False,
                detail=f"{red_flag.value} has no explicit yes/no answer",
            )

    for state in flags:
        if (
            state.flag in NEEDS_CLEARANCE_FLAGS
            and state.value == "yes"
            and state.clearance != "yes"
        ):
            return GuardVerdict(
                rule=_PLAN_RULE,
                ok=False,
                detail=f"{state.flag.value} is 'yes' without doctor clearance",
            )
    return GuardVerdict(
        rule=_PLAN_RULE, ok=True, detail="no open hold, every red flag answered and cleared"
    )


def exercise_allowed(exercise: Exercise, flags: Sequence[ScreeningFlagState]) -> GuardVerdict:
    """A current-injury area flag answered "yes" contraindicates any exercise that lists it
    in `contraindicated_by`, regardless of clearance: clearance is about permission to train
    at all (a red-flag condition), not about a specific injury area."""
    active = {state.flag for state in flags if state.value == "yes"}
    hit = sorted(flag.value for flag in active & set(exercise.contraindicated_by))
    if hit:
        return GuardVerdict(
            rule=_EXERCISE_RULE,
            ok=False,
            detail=f"{exercise.id} is contraindicated by: {', '.join(hit)}",
        )
    return GuardVerdict(
        rule=_EXERCISE_RULE, ok=True, detail=f"{exercise.id} has no active contraindication"
    )
