"""ADR 0016: a transparent weekly workload tally — not a fatigue model.

Every prescription in a scheduled workout is given a **stimulus** and a number of **load
units** per set, and the body **regions** whose demand it adds to. The units are a coarse,
documented weighting (`UNITS`), so that a heavy deadlift set, a 30-second rope interval, a
footwork drill and a stretch don't count the same:

| stimulus       | what                                                   | units per set     |
|----------------|--------------------------------------------------------|-------------------|
| `strength`     | a heavy compound lift (six reps or fewer)              | 1.0               |
|                | another compound lift, loaded or bodyweight            | 0.75              |
|                | isolation and accessory work                           | 0.5               |
|                | a trunk hold or slow core work                         | 0.3               |
| `power`        | loaded fast work: throws, swings, slams                | 0.6               |
| `plyometric`   | jumps, hops, sprints, shuttles                         | 0.5               |
| `technical`    | footwork and other skill drills done for speed         | 0.25              |
| `conditioning` | intervals, carries, circuits: by minutes of work       | 0.4/min (0.15–0.8)|
| `mobility`     | mobility, and anything in a warm-up block              | 0                 |

Regions are the overlaps worth watching across a week: the **hip hinge** (deadlifts,
Romanian deadlifts, hip thrusts — and kettlebell swings, which the catalog files as
conditioning but which load the hips and lower back the same way), **knee-dominant legs**
(squats, lunges, and every jump or sprint, plyometric or conditioning) and the
**shoulders** (pressing, pulling, raises, and upper-body conditioning like battle ropes).
`services.plan_quality` turns the tally into warnings; nothing here refuses a plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from fitme.domain.catalog import Catalog, Exercise, intensity_mode
from fitme.domain.enums import (
    BlockRole,
    Equipment,
    ExerciseKind,
    ExercisePattern,
    IntensityMode,
    Measure,
    Quality,
)
from fitme.domain.models import Plan, Prescription, Workout
from fitme.services import session_time


class Stimulus(StrEnum):
    STRENGTH = "strength"
    POWER = "power"
    PLYOMETRIC = "plyometric"
    TECHNICAL = "technical"
    CONDITIONING = "conditioning"
    MOBILITY = "mobility"


class Region(StrEnum):
    HINGE = "hip hinge"
    KNEE = "knee-dominant legs"
    SHOULDER = "shoulders"


@dataclass(frozen=True, slots=True)
class Units:
    heavy: float = 1.0
    compound: float = 0.75
    accessory: float = 0.5
    trunk: float = 0.3
    power: float = 0.6
    plyometric: float = 0.5
    technical: float = 0.25
    conditioning_per_minute: float = 0.4
    conditioning_min: float = 0.15
    conditioning_max: float = 0.8
    heavy_reps_max: int = 6


UNITS = Units()

_COMPOUND_PATTERNS = frozenset(
    {
        ExercisePattern.SQUAT,
        ExercisePattern.HINGE,
        ExercisePattern.HORIZONTAL_PUSH,
        ExercisePattern.HORIZONTAL_PULL,
        ExercisePattern.VERTICAL_PUSH,
        ExercisePattern.VERTICAL_PULL,
    }
)
_SHOULDER_PATTERNS = frozenset(
    {
        ExercisePattern.HORIZONTAL_PUSH,
        ExercisePattern.HORIZONTAL_PULL,
        ExercisePattern.VERTICAL_PUSH,
        ExercisePattern.VERTICAL_PULL,
        ExercisePattern.ACCESSORY,
    }
)


@dataclass(frozen=True, slots=True)
class Contribution:
    """One prescription's share of a session's workload."""

    exercise_id: str
    stimulus: Stimulus
    regions: frozenset[Region]
    units: float  # all sets


@dataclass(frozen=True, slots=True)
class SessionLoad:
    weekday: int | None  # `None` for a workout that is not scheduled
    workout_key: str
    contributions: tuple[Contribution, ...]

    def region_units(self, region: Region) -> float:
        return sum(item.units for item in self.contributions if region in item.regions)

    def stimulus_units(self, stimulus: Stimulus) -> float:
        return sum(item.units for item in self.contributions if item.stimulus == stimulus)


def is_heavy(item: Prescription, exercise: Exercise, *, heavy_reps_max: int = 6) -> bool:
    """A loaded compound strength lift at a low rep target: the review's "heavy"."""
    return (
        exercise.kind == ExerciseKind.COMPOUND
        and exercise.kg_loadable
        and item.measure == Measure.REPS
        and item.reps_max is not None
        and item.reps_max <= heavy_reps_max
        and Quality.STRENGTH in exercise.qualities
    )


def stimulus_of(item: Prescription, exercise: Exercise, role: BlockRole | None) -> Stimulus:
    if role in (BlockRole.WARM_UP, BlockRole.MOBILITY):
        return Stimulus.MOBILITY
    mode = intensity_mode(exercise, item.measure)
    # By what it mainly trains: a clean is power work even in triples (the review's order
    # check still treats a heavy one as tiring strength work).
    if mode == IntensityMode.SPEED:
        if exercise.kg_loadable:
            return Stimulus.POWER
        lower = bool({"knee", "ankle"} & set(exercise.loads_areas))
        if lower and Quality.POWER in exercise.qualities:
            return Stimulus.PLYOMETRIC
        return Stimulus.TECHNICAL
    if mode in (IntensityMode.PACE, IntensityMode.CARRY) or role == BlockRole.CONDITIONING:
        return Stimulus.CONDITIONING
    if exercise.kind == ExerciseKind.MOBILITY or (
        exercise.qualities and exercise.qualities[0] == Quality.MOBILITY
    ):
        return Stimulus.MOBILITY
    return Stimulus.STRENGTH


def set_units(
    item: Prescription, exercise: Exercise, stimulus: Stimulus, units: Units = UNITS
) -> float:
    """Load units of one set (see the module table)."""
    if stimulus == Stimulus.MOBILITY:
        return 0.0
    if stimulus == Stimulus.POWER:
        return units.power
    if stimulus == Stimulus.PLYOMETRIC:
        return units.plyometric
    if stimulus == Stimulus.TECHNICAL:
        return units.technical
    if stimulus == Stimulus.CONDITIONING:
        minutes = session_time.set_work_seconds(item, exercise) / 60
        return min(
            units.conditioning_max,
            max(units.conditioning_min, minutes * units.conditioning_per_minute),
        )
    if is_heavy(item, exercise, heavy_reps_max=units.heavy_reps_max):
        return units.heavy
    if (
        exercise.pattern == ExercisePattern.CORE
        or intensity_mode(exercise, item.measure) == IntensityMode.NONE
    ):
        return units.trunk
    if exercise.pattern in _COMPOUND_PATTERNS and exercise.kind != ExerciseKind.ISOLATION:
        return units.compound
    return units.accessory


def regions_of(exercise: Exercise, stimulus: Stimulus) -> frozenset[Region]:
    """The regions whose weekly demand an exercise adds to (see the module docstring)."""
    areas = set(exercise.loads_areas)
    found: set[Region] = set()
    # A swing is filed as conditioning but is a loaded hip hinge; a medicine-ball slam or
    # throw loads the hips too, but it is a throw — the hinge region doesn't count it.
    hinge_like = exercise.pattern == ExercisePattern.HINGE or (
        exercise.kg_loadable
        and exercise.pattern in (ExercisePattern.CONDITIONING, ExercisePattern.ACCESSORY)
        and {"lower_back", "hip"} <= areas
        and Equipment.MEDICINE_BALL not in exercise.equipment
    )
    if hinge_like:
        found.add(Region.HINGE)
    # Jumps and sprints load the knees whatever their stimulus: a plyometric, or cardio such
    # as jump rope or sprint intervals done as conditioning.
    lower_cardio = (
        stimulus == Stimulus.CONDITIONING
        and exercise.kind == ExerciseKind.CARDIO
        and "knee" in areas
    )
    if exercise.pattern == ExercisePattern.SQUAT or stimulus == Stimulus.PLYOMETRIC or lower_cardio:
        found.add(Region.KNEE)
    upper_body = exercise.pattern in _SHOULDER_PATTERNS or (
        exercise.pattern == ExercisePattern.CONDITIONING and stimulus == Stimulus.CONDITIONING
    )
    if "shoulder" in areas and upper_body and not hinge_like:
        found.add(Region.SHOULDER)
    return frozenset(found)


def session_load(
    workout: Workout,
    catalog: Catalog,
    roles: Sequence[BlockRole | None],
    weekday: int | None = None,
    units: Units = UNITS,
) -> SessionLoad:
    """`roles` are the workout's block roles (`plan_quality.block_roles`)."""
    contributions: list[Contribution] = []
    for block, role in zip(workout.blocks, roles, strict=True):
        for item in block.items:
            exercise = catalog.by_id(item.exercise_id)
            if exercise is None:
                continue
            stimulus = stimulus_of(item, exercise, role)
            contributions.append(
                Contribution(
                    exercise_id=exercise.id,
                    stimulus=stimulus,
                    regions=regions_of(exercise, stimulus),
                    units=round(item.sets * set_units(item, exercise, stimulus, units), 3),
                )
            )
    return SessionLoad(weekday=weekday, workout_key=workout.key, contributions=tuple(contributions))


def week(
    plan: Plan,
    catalog: Catalog,
    roles_of: dict[str, list[BlockRole | None]],
    units: Units = UNITS,
) -> list[SessionLoad]:
    """Every scheduled day's session load, in weekday order. `roles_of` maps a workout key
    to its block roles."""
    by_key = {workout.key: workout for workout in plan.workouts}
    sessions: list[SessionLoad] = []
    for day in sorted(plan.schedule, key=lambda item: item.weekday):
        workout = by_key.get(day.workout_key)
        if workout is None:
            continue
        sessions.append(session_load(workout, catalog, roles_of[workout.key], day.weekday, units))
    return sessions


def summary(sessions: Sequence[SessionLoad]) -> dict[str, object]:
    """Machine data for logs and the planner's repair feedback: units by region and by
    stimulus, per session and for the week."""
    return {
        "sessions": [
            {
                "weekday": session.weekday,
                "workout": session.workout_key,
                "regions": {
                    region.value: round(session.region_units(region), 1) for region in Region
                },
                "stimuli": {
                    stimulus.value: round(session.stimulus_units(stimulus), 1)
                    for stimulus in Stimulus
                    if session.stimulus_units(stimulus) > 0
                },
            }
            for session in sessions
        ],
        "week": {
            region.value: round(sum(session.region_units(region) for session in sessions), 1)
            for region in Region
        },
    }
