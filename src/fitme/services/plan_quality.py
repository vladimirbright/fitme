"""ADR 0009: a deterministic review of a plan's programming — not safety (the guards in
`fitme.guards` are), but obvious contradictions a model's plan can contain:

- a workout that says it trains a quality (`Workout.qualities`) with no exercise that
  directly trains it, or a requested quality nothing in the plan trains;
- heavy lifting with no warm-up first, or power/speed work placed after heavy strength work;
- a superset of two heavy lifts, with mismatched sets, or with a long rest between its
  exercises (a superset's rests are transitions; the round's rest is on its last exercise);
- weekly shoulder or heavy lower-body volume above conservative limits that shrink when the
  user also trains a sport, and heavy lower-body days back to back.

Each finding is machine text for the planner's one repair round (A§6.4 step 4's feedback
retry); a finding that survives it is logged with the plan, never a refusal. The limits are
a few explicit numbers (`WorkloadLimits`), not a training-load model: they catch the plan that
is obviously off, and say nothing finer than that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import ExerciseKind, ExercisePattern, Measure, Quality, Sport
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Plan, Prescription, Workout

# Exercises that train the shoulder girdle as work rather than as a support (a plank or a
# carry loads it too, but isn't counted).
_SHOULDER_PATTERNS = frozenset(
    {
        ExercisePattern.HORIZONTAL_PUSH,
        ExercisePattern.HORIZONTAL_PULL,
        ExercisePattern.VERTICAL_PUSH,
        ExercisePattern.VERTICAL_PULL,
        ExercisePattern.ACCESSORY,
        ExercisePattern.CONDITIONING,
    }
)
_LOWER_PATTERNS = frozenset({ExercisePattern.SQUAT, ExercisePattern.HINGE})
_FAST_QUALITIES = frozenset({Quality.POWER, Quality.ROTATIONAL_POWER, Quality.AGILITY})
# Sports that already load the shoulders or the legs heavily, per weekly session.
_SHOULDER_SPORTS = frozenset(
    {Sport.BOXING_MARTIAL_ARTS, Sport.SWIMMING, Sport.CLIMBING, Sport.RACKET_SPORT}
)
_LEG_SPORTS = frozenset({Sport.RUNNING, Sport.CYCLING, Sport.TEAM_SPORT})


@dataclass(frozen=True, slots=True)
class WorkloadLimits:
    """Weekly working sets, deliberately generous: a plan above them is off on its face."""

    heavy_reps_max: int = 6  # a kg compound at or below this rep target counts as heavy
    shoulder_sets: int = 36
    lower_heavy_sets: int = 24
    sport_shoulder_sets_per_session: int = 3  # taken off `shoulder_sets` per sport session
    sport_lower_sets_per_session: int = 2
    sport_floor_sets: int = 12  # never below this, however much sport
    superset_transition_seconds: int = 45


DEFAULT_LIMITS = WorkloadLimits()


@dataclass(frozen=True, slots=True)
class ReviewContext:
    catalog: Catalog
    requested: Sequence[Quality] = ()
    sport: Sport | None = None
    sport_sessions_per_week: int | None = None
    limits: WorkloadLimits = DEFAULT_LIMITS
    # The request itself names goals ("power", "foot speed"): every workout then has to say
    # which of them it trains, so its title and exercises can be checked against each other.
    require_declared: bool = False


def _finding(rule: str, detail: str) -> GuardVerdict:
    return GuardVerdict(rule=f"quality.{rule}", ok=False, detail=detail)


def _items(workout: Workout) -> list[tuple[int, Prescription]]:
    """Every prescription with its block index."""
    return [(index, item) for index, block in enumerate(workout.blocks) for item in block.items]


def is_heavy(
    item: Prescription, exercise: Exercise, limits: WorkloadLimits = DEFAULT_LIMITS
) -> bool:
    """A loaded compound lift at a low rep target: what needs a warm-up, what shouldn't
    precede power work, and what two of shouldn't share a superset."""
    return (
        exercise.kind == ExerciseKind.COMPOUND
        and exercise.kg_loadable
        and item.measure == Measure.REPS
        and item.reps_max is not None
        and item.reps_max <= limits.heavy_reps_max
        and Quality.STRENGTH in exercise.qualities
    )


def _is_fast(exercise: Exercise) -> bool:
    return bool(exercise.qualities) and exercise.qualities[0] in _FAST_QUALITIES


def _is_warm_up(exercise: Exercise) -> bool:
    return Quality.MOBILITY in exercise.qualities or (
        exercise.kind == ExerciseKind.CARDIO and Quality.CONDITIONING in exercise.qualities
    )


def _workout_findings(workout: Workout, ctx: ReviewContext) -> list[GuardVerdict]:
    findings: list[GuardVerdict] = []
    catalog = ctx.catalog
    resolved = [
        (index, item, exercise)
        for index, item in _items(workout)
        if (exercise := catalog.by_id(item.exercise_id)) is not None
    ]
    key = workout.key

    trained = {quality for _, _, exercise in resolved for quality in exercise.qualities}
    for quality in workout.qualities:
        if quality not in trained:
            findings.append(
                _finding(
                    "goal_coverage",
                    f"workout {key} is for {quality.value}, but none of its exercises "
                    f"directly trains {quality.value}",
                )
            )
    if ctx.require_declared and not workout.qualities:
        findings.append(
            _finding("declared", f"workout {key}: state the qualities it trains (qualities)")
        )

    heavy = [(index, item) for index, item, exercise in resolved if is_heavy(item, exercise)]
    if heavy:
        first_block = workout.blocks[0] if workout.blocks else None
        warmed = first_block is not None and any(
            (exercise := catalog.by_id(item.exercise_id)) is not None and _is_warm_up(exercise)
            for item in first_block.items
        )
        if not warmed:
            findings.append(
                _finding(
                    "warm_up",
                    f"workout {key}: heavy lifting ({heavy[0][1].exercise_id}) with no "
                    "warm-up block first (mobility or easy cardio for what follows)",
                )
            )
        first_heavy = heavy[0][0]
        for index, item, exercise in resolved:
            if index > first_heavy and _is_fast(exercise) and not is_heavy(item, exercise):
                findings.append(
                    _finding(
                        "order",
                        f"workout {key}: {item.exercise_id} (power/speed) comes after heavy "
                        f"{heavy[0][1].exercise_id}; put fast work before heavy strength work",
                    )
                )

    for block in workout.blocks:
        if block.kind != "superset":
            continue
        ids = ", ".join(item.exercise_id for item in block.items)
        heavy_in_block = [
            item
            for item in block.items
            if (exercise := catalog.by_id(item.exercise_id)) is not None
            and is_heavy(item, exercise)
        ]
        if len(heavy_in_block) > 1:
            findings.append(
                _finding(
                    "superset",
                    f"workout {key}, superset {ids}: two heavy lifts compete; do them as "
                    "single blocks or pair one with a light, non-competing exercise",
                )
            )
        if len({item.sets for item in block.items}) > 1:
            findings.append(
                _finding(
                    "superset", f"workout {key}, superset {ids}: give each exercise the same sets"
                )
            )
        long_transitions = [
            item.exercise_id
            for item in block.items[:-1]
            if item.rest_seconds > ctx.limits.superset_transition_seconds
        ]
        if long_transitions:
            findings.append(
                _finding(
                    "superset",
                    f"workout {key}, superset {ids}: rest after {', '.join(long_transitions)} "
                    f"is the transition to the next exercise (at most "
                    f"{ctx.limits.superset_transition_seconds} s); the round's rest goes on the "
                    "last exercise",
                )
            )
    return findings


def _sport_sessions(ctx: ReviewContext, sports: frozenset[Sport]) -> int:
    if ctx.sport is None or ctx.sport not in sports:
        return 0
    return ctx.sport_sessions_per_week or 0


def _weekly_findings(plan: Plan, ctx: ReviewContext) -> list[GuardVerdict]:
    findings: list[GuardVerdict] = []
    catalog = ctx.catalog
    limits = ctx.limits
    by_key = {workout.key: workout for workout in plan.workouts}
    shoulder_sets = 0
    lower_heavy_sets = 0
    heavy_lower_days: list[int] = []
    for day in plan.schedule:
        workout = by_key.get(day.workout_key)
        if workout is None:
            continue
        day_heavy_lower = False
        for _, item in _items(workout):
            exercise = catalog.by_id(item.exercise_id)
            if exercise is None:
                continue
            if "shoulder" in exercise.loads_areas and exercise.pattern in _SHOULDER_PATTERNS:
                shoulder_sets += item.sets
            if exercise.pattern in _LOWER_PATTERNS and is_heavy(item, exercise, limits):
                lower_heavy_sets += item.sets
                day_heavy_lower = True
        if day_heavy_lower:
            heavy_lower_days.append(day.weekday)

    shoulder_cap = max(
        limits.sport_floor_sets,
        limits.shoulder_sets
        - limits.sport_shoulder_sets_per_session * _sport_sessions(ctx, _SHOULDER_SPORTS),
    )
    if shoulder_sets > shoulder_cap:
        findings.append(
            _finding(
                "shoulder_volume",
                f"{shoulder_sets} weekly sets of pressing, pulling, raises and arm conditioning "
                f"exceed {shoulder_cap}"
                + (" with the user's other sport" if shoulder_cap < limits.shoulder_sets else "")
                + "; cut overlapping shoulder work",
            )
        )
    lower_cap = max(
        limits.sport_floor_sets,
        limits.lower_heavy_sets
        - limits.sport_lower_sets_per_session * _sport_sessions(ctx, _LEG_SPORTS),
    )
    if lower_heavy_sets > lower_cap:
        findings.append(
            _finding(
                "lower_volume",
                f"{lower_heavy_sets} weekly sets of heavy squats and hinges exceed {lower_cap}",
            )
        )
    days = sorted(set(heavy_lower_days))
    if len(days) > 1:
        # Each heavy day and the next one, wrapping Sunday -> Monday.
        for first, second in zip(days, [*days[1:], days[0] + 7], strict=True):
            if second - first == 1:
                findings.append(
                    _finding(
                        "consecutive_heavy",
                        f"heavy lower-body work on consecutive days (weekdays {first} and "
                        f"{second % 7}); leave a day between them",
                    )
                )
    return findings


def review(plan: Plan, ctx: ReviewContext) -> list[GuardVerdict]:
    """Every finding for `plan` (failing `quality.*` verdicts), empty when there's nothing
    to fix. Pure."""
    findings: list[GuardVerdict] = []
    for workout in plan.workouts:
        findings.extend(_workout_findings(workout, ctx))
    trained = {
        quality
        for workout in plan.workouts
        for _, item in _items(workout)
        if (exercise := ctx.catalog.by_id(item.exercise_id)) is not None
        for quality in exercise.qualities
    }
    for quality in ctx.requested:
        if quality not in trained:
            findings.append(
                _finding(
                    "request_coverage",
                    f"the request asks for {quality.value}; no exercise in the plan directly "
                    f"trains it (see exercise_facts qualities)",
                )
            )
    findings.extend(_weekly_findings(plan, ctx))
    return findings
