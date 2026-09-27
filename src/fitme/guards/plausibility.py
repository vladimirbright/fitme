"""Deterministic plausibility check over a *parsed* set result before it is shown for
confirmation or stored (A§6.5 step 5, M7).

The `result_parse` agent turns free text into numbers. A typo ("425" for 42.5), a combined
two-dumbbell total reported on a `per_implement` exercise, or a kg figure on an exercise that
takes no kg load would, once stored in `set_logs`, raise the historical max — and with it the
ceiling every later prescription is checked against (AGENTS.md §2 "ceiling on absolute
load"). This guard is what keeps such a value out of the log: an implausible parse is treated
as unclear and the user is asked again. Nothing is stored.

Pure: domain objects in, verdicts out, no I/O (A§7).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from fitme.domain.catalog import Exercise
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import MAX_IMPLEMENT_KG, MAX_TOTAL_KG, Load
from fitme.domain.results import SetResult

_RULE = "plausibility.parsed_load"

# A parsed load above this multiple of the reference load is a typo or a misread, not a lift.
_MAX_RATIO = 2.0
# ... and so is one further above the reference than this many catalog increments.
_MAX_INCREMENTS_ABOVE = 3
# Float tolerance for "exactly twice the prescribed load" (a combined two-implement total).
_TOTAL_PATTERN_TOLERANCE = 1e-6
# A§6.5.1 absolute bounds, applied always (calibration included): no catalog exercise is
# realistically loaded beyond these, so a larger number is a typo, never a lift. The values
# live in `domain.models` (`MAX_TOTAL_KG`/`MAX_IMPLEMENT_KG`, re-exported here) so the domain
# model can bound a display-only hint with the same numbers.


def _absolute_bound_kg(exercise: Exercise) -> float:
    return MAX_TOTAL_KG if exercise.load_unit == "total" else MAX_IMPLEMENT_KG


def check_parsed_load(
    exercise: Exercise,
    prescribed: Load,
    parsed_kg: float | None,
    *,
    history_max_kg: float | None = None,
) -> GuardVerdict:
    """Whether `parsed_kg` (one parsed set's `load_kg`) is a plausible load for one set of
    `exercise` prescribed at `prescribed` (A§6.5.1). Implausible when any of these holds:

    - it is a kg value on an exercise that is not kg-loadable (A§4.4);
    - it is not a finite positive number (fail closed, A§7);
    - it is above the absolute bound: `MAX_TOTAL_KG` for a `total` load, `MAX_IMPLEMENT_KG`
      for a `per_implement`/`single_implement` one — always, calibration included;
    - against the reference `R` — the prescribed kg, or for a `calibration`/`bodyweight`
      prescription the known `history_max_kg` — it is above `2 × R`;
    - against `R`, it is above `R + 3 × increment_kg`;
    - the exercise is `per_implement` and it is exactly `2 × P` for a prescribed kg `P` (a
      combined two-dumbbell total where the per-dumbbell figure was expected).

    `None` (bodyweight / not reported) is always plausible. With a `calibration` or
    `bodyweight` prescription and no history there is no reference, so only the first three
    rules apply.
    """
    if parsed_kg is None:
        return GuardVerdict(rule=_RULE, ok=True, detail=f"{exercise.id}: no load reported")
    if not exercise.kg_loadable:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"{exercise.id}: {parsed_kg!r} kg reported on a non-kg-loadable exercise",
        )
    if not math.isfinite(parsed_kg) or parsed_kg <= 0:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=f"{exercise.id}: reported load {parsed_kg!r} is not a finite positive number",
        )
    bound_kg = _absolute_bound_kg(exercise)
    if parsed_kg > bound_kg:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: reported {parsed_kg:g} kg is above the absolute bound of "
                f"{bound_kg:g} kg for a {exercise.load_unit} load"
            ),
        )
    prescribed_kg = prescribed.kg if prescribed.kind == "kg" else None
    reference_kg = prescribed_kg
    reference_name = "prescribed"
    if reference_kg is None and history_max_kg is not None and _is_valid_kg(history_max_kg):
        reference_kg = history_max_kg
        reference_name = "historical max"
    if reference_kg is None:
        return GuardVerdict(
            rule=_RULE,
            ok=True,
            detail=(
                f"{exercise.id}: {parsed_kg:g} kg reported against a {prescribed.kind} load with "
                "no history; within the absolute bound"
            ),
        )
    if parsed_kg > reference_kg * _MAX_RATIO:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: reported {parsed_kg:g} kg is more than {_MAX_RATIO:g}× the "
                f"{reference_name} {reference_kg:g} kg"
            ),
        )
    limit_kg = reference_kg + _MAX_INCREMENTS_ABOVE * exercise.increment_kg
    if parsed_kg > limit_kg:
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: reported {parsed_kg:g} kg is more than {_MAX_INCREMENTS_ABOVE} "
                f"increments ({exercise.increment_kg:g} kg) above the {reference_name} "
                f"{reference_kg:g} kg"
            ),
        )
    if (
        prescribed_kg is not None
        and exercise.load_unit == "per_implement"
        and abs(parsed_kg - 2 * prescribed_kg) <= _TOTAL_PATTERN_TOLERANCE
    ):
        return GuardVerdict(
            rule=_RULE,
            ok=False,
            detail=(
                f"{exercise.id}: reported {parsed_kg:g} kg is exactly twice the prescribed "
                f"{prescribed_kg:g} kg each — looks like a combined two-implement total"
            ),
        )
    return GuardVerdict(
        rule=_RULE,
        ok=True,
        detail=(
            f"{exercise.id}: {parsed_kg:g} kg is plausible against the {reference_name} "
            f"{reference_kg:g} kg"
        ),
    )


def _is_valid_kg(value: float) -> bool:
    return math.isfinite(value) and value > 0


def check_parsed_results(
    exercise: Exercise,
    prescribed: Load,
    sets: Sequence[SetResult],
    *,
    history_max_kg: float | None = None,
) -> list[GuardVerdict]:
    """`check_parsed_load` over every parsed set of one prescription (skipped sets carry no
    load and are always fine). The caller treats any failing verdict as "unclear": ask again,
    store nothing."""
    return [
        check_parsed_load(exercise, prescribed, item.load_kg, history_max_kg=history_max_kg)
        for item in sets
    ]


__all__ = ["MAX_IMPLEMENT_KG", "MAX_TOTAL_KG", "check_parsed_load", "check_parsed_results"]
