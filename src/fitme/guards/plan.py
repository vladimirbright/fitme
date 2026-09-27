"""A§7 `validate_plan`: every structural and safety check a proposed `Plan` must pass before
it's shown to the user (A§6.4 step 4).

**Reference load (A§7):** an increase is measured against the exercise's *current working
load* — the prescribed load of the last completed session, falling back to the active plan
version's prescription (`ctx.current_load_kg`) — never against the historical max alone. When
both the current load and the historical max are known, the reference is
`min(current, history_max)`: a stale prescription (current lower than a max set earlier, e.g.
a since-edited plan) must not let a jump straight to `history_max` skip the weekly-cap and
check-in gates that a real, current-relative increase would have to pass. If there's history
but no known current load, `ctx.history_max_kg` is used as the reference instead, and the
increase checks still run (fail closed: better to over-check than to silently skip them).
`check_ceiling` always runs for a `kg` load, increase or not, as an absolute backstop;
`check_weekly_increment`/`checkins.increase_allowed` only run for an actual increase
(`proposed_kg > reference_kg`), using `proposed_kg - reference_kg` as the delta.

**Fails closed on a non-finite/non-positive current load (B1):** a NaN/inf `current_load_kg`
must not silently pass every verdict — Python's `NaN > x` and `x > NaN` are both always
`False`, so a NaN reference would otherwise make `is_increase` false and skip every
increase-only check entirely (a "value" that silently disables the guards). A `current_kg`
that's present but not finite/positive produces a failing verdict directly instead.
"""

from __future__ import annotations

import math

from fitme.domain.catalog import Exercise
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Load, Plan
from fitme.guards import ceiling, checkins, screening
from fitme.guards.context import GuardContext
from fitme.guards.progression import check_weekly_increment

_CATALOG_RULE = "plan.catalog_id"
_LOCATION_RULE = "plan.location_fit"
_EQUIPMENT_RULE = "plan.equipment_fit"
_SCHEDULE_LENGTH_RULE = "plan.schedule_length"
_SCHEDULE_KEYS_RULE = "plan.schedule_workout_keys"
_REFERENCE_LOAD_RULE = "plan.reference_load"


def _load_verdicts(exercise: Exercise, load: Load, ctx: GuardContext) -> list[GuardVerdict]:
    proposed_kg = load.kg if load.kind == "kg" else None
    history_max_kg = ctx.history_max_kg.get(exercise.id)
    verdicts = [
        ceiling.check_ceiling(
            history_max_kg=history_max_kg,
            proposed_load_kg=proposed_kg,
            increment_kg=exercise.increment_kg,
        )
    ]

    current_kg = ctx.current_load_kg.get(exercise.id)
    if current_kg is not None and (not math.isfinite(current_kg) or current_kg <= 0):
        verdicts.append(
            GuardVerdict(
                rule=_REFERENCE_LOAD_RULE,
                ok=False,
                detail=(
                    f"{exercise.id}: current_load_kg {current_kg!r} is not a finite positive number"
                ),
            )
        )
        return verdicts  # fail closed: never compute a reference from a garbage value

    # The reference is min(current, history_max) when both are known (A§7): a stale
    # prescription below the historical max must still be checked as a real increase, not
    # silently rebased up to the max. With only one of the two known, use that one; with
    # neither, there's no reference to compute against (the ceiling guard above already
    # blocks any bare kg proposal with no history at all).
    known = [v for v in (current_kg, history_max_kg) if v is not None]
    reference_kg = min(known) if known else None

    is_increase = (
        proposed_kg is not None and reference_kg is not None and proposed_kg > reference_kg
    )
    if is_increase:
        assert proposed_kg is not None and reference_kg is not None  # for mypy, per is_increase
        delta_kg = proposed_kg - reference_kg
        cap_kg = ctx.weekly_cap_kg.get(exercise.id, ctx.default_weekly_cap_kg)
        increases = ctx.increases_7d.get(exercise.id, ())
        verdicts.append(
            check_weekly_increment(
                exercise, increases_7d=increases, proposed_increase_kg=delta_kg, cap_kg=cap_kg
            )
        )
        verdicts.append(checkins.increase_allowed(exercise, ctx.checkins))

    return verdicts


def validate_plan(plan: Plan, ctx: GuardContext) -> list[GuardVerdict]:
    """Every guard verdict for `plan`, in `ctx`. Some verdicts are plan-wide (schedule
    length/keys); most are per-prescription. The caller decides what to do with a `False`
    verdict (retry, refuse); this function only reports."""
    verdicts: list[GuardVerdict] = []

    verdicts.append(screening.plan_allowed(ctx.flags, ctx.holds))

    workout_keys = {workout.key for workout in plan.workouts}
    verdicts.append(
        GuardVerdict(
            rule=_SCHEDULE_LENGTH_RULE,
            ok=len(plan.schedule) == ctx.sessions_per_week,
            detail=(
                f"schedule has {len(plan.schedule)} day(s), profile asks for "
                f"{ctx.sessions_per_week} session(s) per week"
            ),
        )
    )
    missing_keys = sorted({day.workout_key for day in plan.schedule} - workout_keys)
    verdicts.append(
        GuardVerdict(
            rule=_SCHEDULE_KEYS_RULE,
            ok=not missing_keys,
            detail=(
                "every scheduled workout key exists"
                if not missing_keys
                else f"schedule references missing workout key(s): {', '.join(missing_keys)}"
            ),
        )
    )

    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                exercise_id = prescription.exercise_id
                exercise = ctx.catalog.by_id(exercise_id)
                if exercise is None:
                    verdicts.append(
                        GuardVerdict(
                            rule=_CATALOG_RULE,
                            ok=False,
                            detail=f"{exercise_id} is not a catalog exercise",
                        )
                    )
                    continue
                verdicts.append(_exercise_catalog_verdict(exercise_id))
                verdicts.append(_location_verdict(exercise, ctx))
                verdicts.append(_equipment_verdict(exercise, ctx))
                verdicts.append(screening.exercise_allowed(exercise, ctx.flags))
                verdicts.extend(_load_verdicts(exercise, prescription.load, ctx))

    return verdicts


def _exercise_catalog_verdict(exercise_id: str) -> GuardVerdict:
    return GuardVerdict(rule=_CATALOG_RULE, ok=True, detail=f"{exercise_id} is a catalog exercise")


def _location_verdict(exercise: Exercise, ctx: GuardContext) -> GuardVerdict:
    ok = ctx.location in exercise.locations
    return GuardVerdict(
        rule=_LOCATION_RULE,
        ok=ok,
        detail=(
            f"{exercise.id}: fits {ctx.location.value}"
            if ok
            else f"{exercise.id}: not available at {ctx.location.value}"
        ),
    )


def _equipment_verdict(exercise: Exercise, ctx: GuardContext) -> GuardVerdict:
    missing = sorted(item.value for item in exercise.equipment if item not in ctx.equipment)
    return GuardVerdict(
        rule=_EQUIPMENT_RULE,
        ok=not missing,
        detail=(
            f"{exercise.id}: required equipment available"
            if not missing
            else f"{exercise.id}: missing equipment: {', '.join(missing)}"
        ),
    )
