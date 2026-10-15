"""ADR 0009: a prescription's measure (reps, time, distance) must be one its exercise accepts
(`Exercise.measures`). A mismatch — a plank as "3 × 1", a carry in reps — is repaired the
way a bad load is (A§7.3): deterministically, in place, and recorded in `guards_fired`, so
a model's slip never sinks an otherwise valid plan and never reaches the user."""

from __future__ import annotations

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import Equipment, ExerciseKind, Measure
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Plan, Prescription

MEASURE_FIT_RULE = "plan.measure_fit"
MEASURE_SUBSTITUTED_RULE = "measure.substituted"

# Neutral targets for an exercise's default measure: a hold or an interval, a steady
# cardio-machine bout, a carry, a reps range.
DEFAULT_REPS = (8, 12)
DEFAULT_SECONDS = 30
DEFAULT_MACHINE_CARDIO_SECONDS = 600
DEFAULT_METERS = 20


def set_target(
    item: Prescription,
    measure: Measure,
    *,
    reps: tuple[int, int] | None = None,
    seconds: int | None = None,
    meters: int | None = None,
) -> None:
    """Put `item` in `measure` with that measure's target, clearing the others. In place and
    unvalidated: callers re-validate (`Prescription.model_validate`) or judge afterwards."""
    item.measure = measure
    item.reps_min, item.reps_max = reps if reps is not None else (None, None)
    item.seconds = seconds
    item.meters = meters


def set_default_target(item: Prescription, exercise: Exercise) -> None:
    """`item` in `exercise`'s default measure (its first) with a neutral target."""
    measure = exercise.measures[0]
    if measure == Measure.TIME:
        machine_cardio = (
            exercise.kind == ExerciseKind.CARDIO and Equipment.MACHINE in exercise.equipment
        )
        seconds = DEFAULT_MACHINE_CARDIO_SECONDS if machine_cardio else DEFAULT_SECONDS
        set_target(item, measure, seconds=seconds)
    elif measure == Measure.DISTANCE:
        set_target(item, measure, meters=DEFAULT_METERS)
    else:
        set_target(item, measure, reps=DEFAULT_REPS)


def repair_measures(plan: Plan, catalog: Catalog) -> list[GuardVerdict]:
    """Every prescription in a measure its exercise doesn't accept is moved to the
    exercise's default measure. Returns the fired verdicts (a failing `plan.measure_fit` and
    an ok `measure.substituted` per repair); empty when everything fits. A non-catalog id is
    left alone (`guards.plan` reports it as a structural failure)."""
    fired: list[GuardVerdict] = []
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                exercise = catalog.by_id(prescription.exercise_id)
                if exercise is None or prescription.measure in exercise.measures:
                    continue
                before = prescription.measure.value
                accepted = ", ".join(measure.value for measure in exercise.measures)
                set_default_target(prescription, exercise)
                fired.append(
                    GuardVerdict(
                        rule=MEASURE_FIT_RULE,
                        ok=False,
                        detail=f"{exercise.id}: prescribed in {before}; accepts {accepted}",
                    )
                )
                fired.append(
                    GuardVerdict(
                        rule=MEASURE_SUBSTITUTED_RULE,
                        ok=True,
                        detail=(
                            f"{exercise.id}: {before} replaced by "
                            f"{prescription.measure.value} {target_label(prescription)}"
                        ),
                    )
                )
    return fired


def target_label(prescription: Prescription) -> str:
    """Machine text for logs and model feedback: "8-12 reps", "30 s", "20 m"."""
    if prescription.measure == Measure.TIME:
        return f"{prescription.seconds} s"
    if prescription.measure == Measure.DISTANCE:
        return f"{prescription.meters} m"
    return f"{prescription.reps_min}-{prescription.reps_max} reps"
