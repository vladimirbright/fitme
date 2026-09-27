"""AGENTS.md §2 "Progression cap" / A§7: the weekly increment cap.

`increases_7d` must be read from `decisions.load_changes` (append-only, across every decision
kind), never from `set_logs`, so deleting a training log can never reset the cap and unlock a
second increase in the same week (A§9.4). That's the caller's responsibility
(`services/loads.py`); this module only checks the numbers it's given.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from fitme.domain.catalog import Exercise
from fitme.domain.guard_types import GuardVerdict

_RULE = "progression.weekly_cap"


def check_weekly_increment(
    exercise: Exercise,
    *,
    increases_7d: Sequence[float],
    proposed_increase_kg: float,
    cap_kg: float,
) -> GuardVerdict:
    """`proposed_increase_kg` is the size of the increase being proposed right now (a delta,
    not an absolute working weight); `increases_7d` are the increase deltas already applied to
    this exercise in the trailing 7 days (A§4.3 `decisions.load_changes`, any kind). Proposals
    that would push the trailing-7-day total over `cap_kg` are rejected outright, never
    clamped down to what would fit (AGENTS.md §2).

    Fails closed (rejects) on a non-finite or non-positive `proposed_increase_kg`/`cap_kg`,
    or on **any** non-finite entry in `increases_7d`: load inputs must be finite and positive
    (A§7, B1). A non-finite `increases_7d` entry is data corruption in the append-only log, so
    it fails the whole check rather than being silently dropped from the sum (a silent drop
    would mean a corrupted row could only ever make this guard *more* permissive, never less).
    A **decrease** entry (e.g. `-10.0`, from a load drop logged the same week) never offsets
    the sum — only positive entries count towards the cap, so a `-10` followed by a `+10` is
    still a full 10 kg increase for cap purposes, not a net zero.
    """
    if not math.isfinite(proposed_increase_kg) or proposed_increase_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: proposed_increase_kg {proposed_increase_kg!r} is not a "
                "finite positive number"
            ),
        )
    if not math.isfinite(cap_kg) or cap_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"{exercise.id}: cap_kg {cap_kg!r} is not a finite positive number",
        )
    for entry in increases_7d:
        if not math.isfinite(entry):
            return GuardVerdict(
                rule=_RULE,
                ok=False,
                detail=(
                    f"{exercise.id}: increases_7d contains a non-finite value ({entry!r}); "
                    "failing closed"
                ),
            )

    positive_increases = [x for x in increases_7d if x > 0]
    total = round(sum(positive_increases) + proposed_increase_kg, 6)
    if total > cap_kg:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: {total:g} kg of increases in the trailing 7 days would "
                f"exceed the {cap_kg:g} kg weekly cap"
            ),
        )
    return GuardVerdict(
        rule=_RULE,
        ok=True,
        detail=f"{exercise.id}: {total:g} kg of increases in the trailing 7 days, within cap",
    )
