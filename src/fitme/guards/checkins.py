"""A§7: for every area an exercise loads **that the user flagged** (`<area>_injury_current`
answered yes — the only areas check-ins are asked for, A§6.5), the latest check-in must be
`fine` before an increase. A missing, `unknown`, `worse` or `pain` answer blocks it (AGENTS.md
§2: "Silence is not consent"). Areas the user did not flag need no check-in."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from fitme.domain.catalog import Exercise
from fitme.domain.enums import AREA_FLAG_TO_LOADS_AREA, CheckinAnswer
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.screening import ScreeningFlagState

_RULE = "checkins.increase_allowed"


def flagged_areas_from(flags: Sequence[ScreeningFlagState]) -> frozenset[str]:
    """The catalog `loads_areas` names of every current-injury area flag answered "yes"
    (A§4.2, `AREA_FLAG_TO_LOADS_AREA`); `hernia` has no loads area and is never included."""
    return frozenset(
        AREA_FLAG_TO_LOADS_AREA[state.flag]
        for state in flags
        if state.value == "yes" and state.flag in AREA_FLAG_TO_LOADS_AREA
    )


def increase_allowed(
    exercise: Exercise,
    checkins: Mapping[str, CheckinAnswer],
    flagged_areas: Collection[str],
) -> GuardVerdict:
    """`checkins` maps a catalog `loads_areas` name (A§4.4) to that area's latest answer;
    `flagged_areas` is the set of areas the user flagged (`flagged_areas_from`). For each
    area the exercise loads that is also flagged, the answer must be `fine`; no entry at all
    counts as `unknown`, never as `fine`. Unflagged areas need nothing.

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
        if area not in flagged_areas:
            continue
        answer = checkins.get(area, CheckinAnswer.UNKNOWN)
        if answer != CheckinAnswer.FINE:
            return GuardVerdict(
                rule=_RULE,
                ok=False,
                detail=(
                    f"{exercise.id}: check-in for flagged area '{area}' is {answer.value}, "
                    "blocks an increase"
                ),
            )
    checked = sorted(area for area in exercise.loads_areas if area in flagged_areas)
    return GuardVerdict(
        rule=_RULE,
        ok=True,
        detail=(
            f"{exercise.id}: flagged area(s) {', '.join(checked)} checked in fine"
            if checked
            else f"{exercise.id}: loads no flagged area; no check-in needed"
        ),
    )
