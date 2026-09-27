"""A§7: any `unknown`, `worse` or `pain` check-in for an area the exercise loads blocks an
increase (AGENTS.md §2: "Silence is not consent" — an area with no check-in at all is
`unknown`, never treated as `fine`)."""

from __future__ import annotations

from collections.abc import Mapping

from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer
from fitme.domain.guard_types import GuardVerdict

_RULE = "checkins.increase_allowed"
_BLOCKING_ANSWERS = frozenset({CheckinAnswer.UNKNOWN, CheckinAnswer.WORSE, CheckinAnswer.PAIN})


def increase_allowed(exercise: Exercise, checkins: Mapping[str, CheckinAnswer]) -> GuardVerdict:
    """`checkins` maps a catalog `loads_areas` name (A§4.4) to that area's latest answer. An
    area the exercise loads with no entry in `checkins` at all is treated exactly like
    `unknown`, never like `fine`.

    Fails closed if `exercise.loads_areas` is empty: this guard only ever runs to gate a
    proposed numeric increase, and a kg-capable exercise is required to declare its
    `loads_areas` (`domain.catalog.Exercise`); an empty list reaching here means there's
    nothing to check an increase against, which must block it, not silently allow it.
    """
    if not exercise.loads_areas:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: no loads_areas configured; failing closed rather than "
                "allowing an unchecked increase"
            ),
        )
    for area in exercise.loads_areas:
        answer = checkins.get(area, CheckinAnswer.UNKNOWN)
        if answer in _BLOCKING_ANSWERS:
            return GuardVerdict(
                rule=_RULE,
                ok=False,
                detail=(
                    f"{exercise.id}: check-in for '{area}' is {answer.value}, blocks an increase"
                ),
            )
    return GuardVerdict(
        rule=_RULE, ok=True, detail=f"{exercise.id}: every loaded area checked in fine"
    )
