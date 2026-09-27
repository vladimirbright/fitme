"""A§7 ceiling guard: never propose above the user's logged historical maximum plus one
increment. With no history, only a calibration load is allowed — the first session for an
exercise is data collection, not progression (AGENTS.md §2)."""

from __future__ import annotations

import math

from fitme.domain.guard_types import GuardVerdict

_RULE = "ceiling.historical_max"


def check_ceiling(
    *, history_max_kg: float | None, proposed_load_kg: float | None, increment_kg: float
) -> GuardVerdict:
    """`proposed_load_kg` is the absolute working weight being proposed, or `None` for a
    non-numeric load (`bodyweight`/`calibration`) — the ceiling only ever constrains a
    numeric `kg` proposal, so a non-kg load always passes this guard.

    Fails closed (rejects) on a non-finite or non-positive `proposed_load_kg`,
    `history_max_kg` or `increment_kg`: load inputs must be finite and positive (A§7).
    """
    if proposed_load_kg is None:
        return GuardVerdict(rule=_RULE, ok=True, detail="not a kg load; ceiling doesn't apply")
    if not math.isfinite(proposed_load_kg) or proposed_load_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"proposed_load_kg {proposed_load_kg!r} is not a finite positive number",
        )
    if not math.isfinite(increment_kg) or increment_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"increment_kg {increment_kg!r} is not a finite positive number",
        )
    if history_max_kg is None:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{proposed_load_kg:g} kg proposed with no logged history for this exercise; "
                "only a calibration load is allowed"
            ),
        )
    if not math.isfinite(history_max_kg) or history_max_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"history_max_kg {history_max_kg!r} is not a finite positive number",
        )
    ceiling_kg = history_max_kg + increment_kg
    if proposed_load_kg > ceiling_kg:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{proposed_load_kg:g} kg exceeds the ceiling of {ceiling_kg:g} kg (history "
                f"max {history_max_kg:g} kg + one increment of {increment_kg:g} kg)"
            ),
        )
    return GuardVerdict(
        rule=_RULE, ok=True, detail=f"{proposed_load_kg:g} kg is within the ceiling"
    )
