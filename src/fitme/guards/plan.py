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
**An increase applied in the trailing 7 days is not counted twice (A§7):** when the highest
applied `to_kg` for the exercise (`ctx.applied_to_kg_7d`) is above `min(current, history_max)`,
it becomes the reference (`reference_load_kg`). Keeping a confirmed load is then no increase,
and only the part above it counts toward the cap; the earlier change was guarded when it was
applied. `check_ceiling` always runs for a `kg` load, increase or not, as an absolute backstop;
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
from fitme.domain.models import Load, Plan, Prescription, Workout
from fitme.guards import ceiling, checkins, screening
from fitme.guards.context import GuardContext
from fitme.guards.progression import check_weekly_increment

_CATALOG_RULE = "plan.catalog_id"
_LOCATION_RULE = "plan.location_fit"
_EQUIPMENT_RULE = "plan.equipment_fit"
_SCHEDULE_LENGTH_RULE = "plan.schedule_length"
_SCHEDULE_KEYS_RULE = "plan.schedule_workout_keys"

_REFERENCE_LOAD_RULE = "plan.reference_load"
_KG_LOADABLE_RULE = "plan.kg_loadable"

# Every rule `load_verdicts` can emit (A§7.3 "when an LLM proposes a load ... a violating
# proposal is rejected, and the engine's value is used"): a failing verdict with one of these
# rules is a *load* violation the caller may repair by substituting the load engine's value
# for that one prescription; any other failing rule is structural (non-catalog id,
# contraindication, equipment/location, schedule) and needs a new proposal.
LOAD_RULES: frozenset[str] = frozenset(
    {
        "ceiling.historical_max",  # guards.ceiling
        "progression.weekly_cap",  # guards.progression
        "checkins.increase_allowed",  # guards.checkins
        _REFERENCE_LOAD_RULE,
        _KG_LOADABLE_RULE,
    }
)


def reference_load_kg(ctx: GuardContext, exercise_id: str) -> float | None:
    """The reference an increase is measured against (A§7): `min(current, history_max)` over
    the known ones, lifted to the highest `to_kg` applied in the trailing 7 days when that is
    higher. `None` with no current load and no history (the ceiling guard then blocks any kg
    proposal on its own). No validation here; `load_verdicts` fails closed on garbage first."""
    known = [
        value
        for value in (ctx.current_load_kg.get(exercise_id), ctx.history_max_kg.get(exercise_id))
        if value is not None
    ]
    if not known:
        return None
    reference_kg = min(known)
    applied_kg = ctx.applied_to_kg_7d.get(exercise_id)
    if applied_kg is not None and applied_kg > reference_kg:
        return applied_kg
    return reference_kg


def load_verdicts(exercise: Exercise, load: Load, ctx: GuardContext) -> list[GuardVerdict]:
    """The load-related verdicts for one prescription (ceiling always; weekly cap and
    check-ins only for an actual increase over the reference load — see the module
    docstring). Public so `services/` can re-judge a single prescription after substituting
    the load engine's value, with the same code `validate_plan` uses."""
    proposed_kg = load.kg if load.kind == "kg" else None
    if proposed_kg is not None and not exercise.kg_loadable:
        # A§4.4 "non-kg exercises": a kg load here is meaningless (a jog, a stretch, a push-up)
        # and the ceiling/cap checks below would only be checking a number that can't exist.
        return [
            GuardVerdict(
                rule=_KG_LOADABLE_RULE,
                ok=False,
                detail=f"{exercise.id}: {proposed_kg:g} kg proposed on a non-kg-loadable exercise",
            )
        ]
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
    applied_kg = ctx.applied_to_kg_7d.get(exercise.id)
    if applied_kg is not None and (not math.isfinite(applied_kg) or applied_kg <= 0):
        verdicts.append(
            GuardVerdict(
                rule=_REFERENCE_LOAD_RULE,
                ok=False,
                detail=(
                    f"{exercise.id}: applied_to_kg_7d {applied_kg!r} is not a finite positive "
                    "number"
                ),
            )
        )
        return verdicts  # fail closed, same as above

    # The reference is min(current, history_max) when both are known (A§7): a stale
    # prescription below the historical max must still be checked as a real increase, not
    # silently rebased up to the max. With only one of the two known, use that one; with
    # neither, there's no reference to compute against (the ceiling guard above already
    # blocks any bare kg proposal with no history at all). An increase applied this week
    # lifts the reference (`reference_load_kg`), so it isn't counted a second time.
    reference_kg = reference_load_kg(ctx, exercise.id)

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
        verdicts.append(checkins.increase_allowed(exercise, ctx.checkins, ctx.flagged_areas))

    return verdicts


def validate_plan(plan: Plan, ctx: GuardContext) -> list[GuardVerdict]:
    """Every guard verdict for `plan`, in `ctx`. Some verdicts are plan-wide (schedule
    length/keys); most are per-prescription. The caller decides what to do with a `False`
    verdict (retry, refuse); this function only reports."""
    verdicts: list[GuardVerdict] = []

    verdicts.append(screening.plan_allowed(ctx.flags, ctx.holds))

    workout_keys = {workout.key for workout in plan.workouts}
    # The profile's `sessions_per_week` is a *default* (what a new plan is built with when
    # the user says nothing else), not a rule: a plan the user asks for with fewer or more
    # days — a 2-day travel plan next to a 3-day main plan — is theirs to have. Only "at
    # least one training day" is enforced. (A weekday listed twice is allowed: it is only
    # ambiguous for history-import session linking, which handles that itself.)
    days = len(plan.schedule)
    verdicts.append(
        GuardVerdict(
            rule=_SCHEDULE_LENGTH_RULE,
            ok=days >= 1,
            detail=(
                f"schedule has {days} day(s) (profile default: {ctx.sessions_per_week}); "
                "a plan needs at least one training day"
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
        verdicts.extend(_workout_prescription_verdicts(workout, ctx))

    return verdicts


def validate_workout(workout: Workout, ctx: GuardContext) -> list[GuardVerdict]:
    """A§6.5.1: the verdicts for *one* workout trained today — the screening gate plus every
    per-prescription check (`prescription_verdicts`) — without the plan-wide schedule rules,
    which say nothing about whether today's workout is safe. `/train` judges a session's
    workout with this; `/plan` judges a whole plan with `validate_plan`."""
    return [
        screening.plan_allowed(ctx.flags, ctx.holds),
        *_workout_prescription_verdicts(workout, ctx),
    ]


def prescription_verdicts(prescription: Prescription, ctx: GuardContext) -> list[GuardVerdict]:
    """Every per-prescription verdict: catalog id, location and equipment fit, contraindication
    (`screening.exercise_allowed`) and the load verdicts (`load_verdicts`)."""
    exercise_id = prescription.exercise_id
    exercise = ctx.catalog.by_id(exercise_id)
    if exercise is None:
        return [
            GuardVerdict(
                rule=_CATALOG_RULE, ok=False, detail=f"{exercise_id} is not a catalog exercise"
            )
        ]
    return [
        _exercise_catalog_verdict(exercise_id),
        _location_verdict(exercise, ctx),
        _equipment_verdict(exercise, ctx),
        screening.exercise_allowed(exercise, ctx.flags),
        *load_verdicts(exercise, prescription.load, ctx),
    ]


def _workout_prescription_verdicts(workout: Workout, ctx: GuardContext) -> list[GuardVerdict]:
    verdicts: list[GuardVerdict] = []
    for block in workout.blocks:
        for prescription in block.items:
            verdicts.extend(prescription_verdicts(prescription, ctx))
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
