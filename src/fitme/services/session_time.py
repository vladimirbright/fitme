"""ADR 0016: how long a workout takes, estimated from what it prescribes.

An estimate, never a measurement (nothing here reads a clock or a log): arithmetic over the
prescription with a few named constants (`TimeModel`):

- **work per set**: reps × `seconds_per_rep`, the prescribed seconds, or meters at running
  pace (cardio and agility work) or walking pace (carries and the rest);
- **unilateral** exercises (`Exercise.unilateral`) do the target on each side, with a short
  switch between sides;
- **single blocks**: sets × work plus the rest *between* sets; the rest after the last set is
  the move to the next block, which its `setup` covers;
- **supersets** (ADR 0009 rest semantics): each round is every exercise's work and its
  transition (the `rest_seconds` of every exercise but the last); the last exercise's rest is
  the round rest, taken between rounds, not after the last one. An exercise with fewer sets
  than the block's rounds sits out the later ones;
- **setup** per block, by equipment: loading a barbell or a rack, adjusting a machine or a
  cable, picking up dumbbells, nothing for bodyweight.

Warm-up and cool-down blocks are estimated the same way. `TimeModel.tolerance` is the
documented uncertainty: a workout is over its budget only when the estimate exceeds the
session length by more than it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fitme.domain.catalog import Catalog, Exercise, intensity_mode
from fitme.domain.enums import Equipment, ExerciseKind, IntensityMode, Measure
from fitme.domain.models import Block, Prescription, Workout

# Equipment that takes real time to set up: plates to load, a rack or a bench to set.
_HEAVY_SETUP = frozenset(
    {
        Equipment.BARBELL,
        Equipment.EZ_BAR,
        Equipment.TRAP_BAR,
        Equipment.SMITH_MACHINE,
        Equipment.RACK,
        Equipment.SLED,
    }
)


@dataclass(frozen=True, slots=True)
class TimeModel:
    """The estimate's constants. Coarse on purpose: the result is "about N minutes"."""

    seconds_per_rep: float = 3.5  # a controlled rep, including the turnaround
    run_seconds_per_meter: float = 0.4  # shuttles, sprints, jogs (about 2.5 m/s with turns)
    walk_seconds_per_meter: float = 1.0  # carries, sled pushes, walking lunges
    side_switch_seconds: int = 10  # changing sides on a unilateral exercise
    heavy_setup_seconds: int = 120  # a barbell, a rack, a sled
    setup_seconds: int = 45  # dumbbells, a kettlebell, a machine, a cable, a box
    bodyweight_setup_seconds: int = 15  # nothing to set up but a place
    # The estimate's uncertainty: over budget means more than 15% above the session length.
    tolerance: float = 1.15


DEFAULT_MODEL = TimeModel()


@dataclass(frozen=True, slots=True)
class WorkoutTime:
    """A workout's estimate, block by block (seconds, in block order)."""

    block_seconds: tuple[float, ...]

    @property
    def seconds(self) -> float:
        return sum(self.block_seconds)

    @property
    def minutes(self) -> float:
        return self.seconds / 60


def _sides(exercise: Exercise | None) -> int:
    return 2 if exercise is not None and exercise.unilateral else 1


def set_work_seconds(
    item: Prescription, exercise: Exercise | None, model: TimeModel = DEFAULT_MODEL
) -> float:
    """One set's working time, both sides of a unilateral exercise included."""
    if item.measure == Measure.TIME and item.seconds is not None:
        work = float(item.seconds)
    elif item.measure == Measure.DISTANCE and item.meters is not None:
        running = exercise is not None and (
            exercise.kind == ExerciseKind.CARDIO
            or intensity_mode(exercise, item.measure) in (IntensityMode.SPEED, IntensityMode.PACE)
        )
        per_meter = model.run_seconds_per_meter if running else model.walk_seconds_per_meter
        work = item.meters * per_meter
    else:
        work = (item.reps_max or item.reps_min or 0) * model.seconds_per_rep
    sides = _sides(exercise)
    return work * sides + (sides - 1) * model.side_switch_seconds


def setup_seconds(exercises: list[Exercise | None], model: TimeModel = DEFAULT_MODEL) -> float:
    """Getting a block ready: the most demanding equipment of its exercises decides."""
    equipment = {
        item for exercise in exercises if exercise is not None for item in exercise.equipment
    }
    if equipment & _HEAVY_SETUP:
        return float(model.heavy_setup_seconds)
    if equipment:
        return float(model.setup_seconds)
    return float(model.bodyweight_setup_seconds)


def block_seconds(block: Block, catalog: Catalog, model: TimeModel = DEFAULT_MODEL) -> float:
    exercises = [catalog.by_id(item.exercise_id) for item in block.items]
    total = setup_seconds(exercises, model)
    if block.kind == "single":
        item, exercise = block.items[0], exercises[0]
        return (
            total
            + item.sets * set_work_seconds(item, exercise, model)
            + max(0, item.sets - 1) * item.rest_seconds
        )
    rounds = max(item.sets for item in block.items)
    round_rest = block.items[-1].rest_seconds
    for round_index in range(rounds):
        for position, (item, exercise) in enumerate(zip(block.items, exercises, strict=True)):
            if item.sets <= round_index:
                continue
            total += set_work_seconds(item, exercise, model)
            if position < len(block.items) - 1:
                total += item.rest_seconds  # the transition to the next exercise
        if round_index < rounds - 1:
            total += round_rest
    return total


def estimate(workout: Workout, catalog: Catalog, model: TimeModel = DEFAULT_MODEL) -> WorkoutTime:
    """The workout's estimated length (see the module docstring). Pure."""
    return WorkoutTime(
        block_seconds=tuple(block_seconds(block, catalog, model) for block in workout.blocks)
    )


def display_minutes(workout: Workout, catalog: Catalog, model: TimeModel = DEFAULT_MODEL) -> int:
    """The estimate as the plan view shows it: rounded to five minutes (it is no finer)."""
    return max(5, round(estimate(workout, catalog, model).minutes / 5) * 5)


def over_budget(
    workout: Workout, catalog: Catalog, session_minutes: int, model: TimeModel = DEFAULT_MODEL
) -> bool:
    return estimate(workout, catalog, model).minutes > session_minutes * model.tolerance
