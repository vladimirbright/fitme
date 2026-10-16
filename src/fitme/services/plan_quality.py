"""ADR 0009, ADR 0011: a deterministic review of a plan's programming — not safety (the
guards in `fitme.guards` are), but obvious contradictions a model's plan can contain:

- **coverage**: a workout that says it trains a quality (`Workout.qualities`, main objective
  first) with no working exercise that directly trains it, a main objective neither trained
  nor supported by enough of the session, or a requested quality nothing in the plan trains.
  An exercise *directly* trains the qualities its catalog entry lists; it *supports* a few
  related ones (`SUPPORTS`: strength supports power, power supports agility, ...); anything
  else is unrelated. Warm-up blocks don't count: jump rope as a warm-up is not footwork work;
- **roles and order**: a block whose stated role (`Block.role`; inferred when absent) its
  exercises don't fit, a narrow technique drill as the warm-up, a warm-up after the main work,
  heavy lifting with no warm-up first, power or speed work after strength or conditioning;
- **prescription**: power work in long sets or with almost no rest;
- **supersets**: two heavy lifts, mismatched sets, a long rest between its exercises (a
  superset's rests are transitions; the round's rest is on its last exercise);
- **the week**: shoulder or heavy lower-body volume above conservative limits that shrink
  when the user also trains a sport, heavy lower-body days back to back, a body region
  trained in only one of two or three weekly sessions (a split nobody asked for), and a
  session clearly longer than the user's session length.

Each finding is machine text for the planner's one repair round (A§6.4 step 4's feedback
retry); a finding that survives it is logged with the plan, never a refusal — these are
warnings, the guards are the errors. The limits are a few explicit numbers
(`WorkloadLimits`), not a training-load model: they catch the plan that is obviously off,
and say nothing finer than that.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from fitme.domain.catalog import Catalog, Exercise, intensity_mode
from fitme.domain.enums import (
    BlockRole,
    ExerciseKind,
    ExercisePattern,
    IntensityMode,
    Measure,
    Quality,
    Sport,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, Plan, Prescription, Workout
from fitme.llm.context import UserContext
from fitme.services import programming
from fitme.services.exercise_search import requested_qualities

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
# Body regions for the weekly distribution check: a region all in one session of a two- or
# three-session week is a split.
_REGIONS: dict[str, frozenset[ExercisePattern]] = {
    "lower body": _LOWER_PATTERNS,
    "pressing": frozenset({ExercisePattern.HORIZONTAL_PUSH, ExercisePattern.VERTICAL_PUSH}),
    "pulling": frozenset({ExercisePattern.HORIZONTAL_PULL, ExercisePattern.VERTICAL_PULL}),
}
# ADR 0011: which qualities an exercise *supports* without training them directly. Small and
# conservative on purpose: a strength base supports power, power supports quick feet and
# rotation, a stable trunk supports rotation, and the two capacity qualities support each
# other. Nothing supports mobility or core stability but their own exercises.
SUPPORTS: dict[Quality, frozenset[Quality]] = {
    Quality.STRENGTH: frozenset({Quality.HYPERTROPHY}),
    Quality.HYPERTROPHY: frozenset({Quality.STRENGTH, Quality.MUSCULAR_ENDURANCE}),
    Quality.POWER: frozenset({Quality.STRENGTH, Quality.ROTATIONAL_POWER}),
    Quality.ROTATIONAL_POWER: frozenset({Quality.POWER, Quality.CORE_STABILITY}),
    Quality.AGILITY: frozenset({Quality.POWER}),
    Quality.MUSCULAR_ENDURANCE: frozenset({Quality.CONDITIONING, Quality.HYPERTROPHY}),
    Quality.CONDITIONING: frozenset({Quality.MUSCULAR_ENDURANCE}),
    Quality.CORE_STABILITY: frozenset(),
    Quality.MOBILITY: frozenset(),
}
# What a stated block role asks of its exercises (ADR 0011): at least one of these qualities.
# `ACCESSORY` takes anything that isn't a heavy lift; `WARM_UP` is checked separately.
_ROLE_QUALITIES: dict[BlockRole, frozenset[Quality]] = {
    BlockRole.POWER: _FAST_QUALITIES,
    BlockRole.STRENGTH: frozenset({Quality.STRENGTH, Quality.HYPERTROPHY}),
    BlockRole.CORE: frozenset({Quality.CORE_STABILITY, Quality.ROTATIONAL_POWER}),
    BlockRole.CONDITIONING: frozenset(
        {Quality.CONDITIONING, Quality.MUSCULAR_ENDURANCE, Quality.AGILITY}
    ),
    BlockRole.MOBILITY: frozenset({Quality.MOBILITY}),
}
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
    # ADR 0011 coverage: an exercise that directly trains a quality counts 1, one that only
    # supports it 0.5. A stated quality needs a direct exercise; the main objective (the
    # workout's first quality) needs 1.5 — two direct exercises, or one plus supporting work.
    main_objective_coverage: float = 1.5
    # Power and speed sets: beyond this many reps a set stops being fast (unless the exercise
    # is also conditioning), and below this rest the next set isn't fresh either.
    power_reps_max: int = 10
    power_rest_min_seconds: int = 45
    # A region with at least this many weekly sets, all in one session of a 2-3 session week.
    split_region_sets: int = 6
    # Session length: estimated minutes above `session_minutes` times this are reported. The
    # estimate is coarse (about 4 s a rep, 1.2 s a meter, a minute of setup per block).
    duration_tolerance: float = 1.25
    seconds_per_rep: float = 4.0
    seconds_per_meter: float = 1.2
    block_setup_seconds: int = 60


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
    # ADR 0011: the user's session length (minutes), and whether a split is what they asked
    # for (preference or request), which turns the split check off.
    session_minutes: int | None = None
    split_wanted: bool = False


def context_for(
    catalog: Catalog,
    user_context: UserContext,
    requested: Sequence[Quality] = (),
    *,
    request: str | None = None,
) -> ReviewContext:
    """The review's context for one user and request: the requested qualities, the other
    sport, the session length and whether a split is wanted (a preference or the request's
    own words). `require_declared` when the request text itself names goals."""
    return ReviewContext(
        catalog=catalog,
        requested=requested,
        sport=user_context.sport,
        sport_sessions_per_week=user_context.sport_sessions_per_week,
        require_declared=bool(requested_qualities(request)),
        session_minutes=user_context.session_minutes,
        split_wanted=programming.split_wanted(user_context, request),
    )


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
    """Warm-up-like: mainly mobility, or easy cardio that is mainly conditioning (jump rope,
    a jog). Quick feet or shadow boxing (mainly agility) are work, not a warm-up."""
    primary = exercise.qualities[0] if exercise.qualities else None
    return (
        exercise.kind == ExerciseKind.MOBILITY
        or primary == Quality.MOBILITY
        or (exercise.kind == ExerciseKind.CARDIO and primary == Quality.CONDITIONING)
    )


def _inferred_role(block: Block, catalog: Catalog, *, opening: bool) -> BlockRole | None:
    """A block's role when the planner stated none (plans made before ADR 0011): the leading
    blocks of warm-up-like exercises are the warm-up; then heavy lifts are strength, and the
    first exercise's main quality decides the rest."""
    exercises = [
        (item, exercise)
        for item in block.items
        if (exercise := catalog.by_id(item.exercise_id)) is not None
    ]
    if not exercises:
        return None
    if opening and all(_is_warm_up(exercise) for _, exercise in exercises):
        return BlockRole.WARM_UP
    if any(is_heavy(item, exercise) for item, exercise in exercises):
        return BlockRole.STRENGTH
    first = exercises[0][1]
    primary = first.qualities[0] if first.qualities else None
    if primary in _FAST_QUALITIES:
        return BlockRole.POWER
    if primary == Quality.CONDITIONING:
        return BlockRole.CONDITIONING
    if primary == Quality.CORE_STABILITY:
        return BlockRole.CORE
    if primary == Quality.MOBILITY:
        return BlockRole.MOBILITY
    if primary == Quality.STRENGTH and first.kind == ExerciseKind.COMPOUND:
        return BlockRole.STRENGTH
    return BlockRole.ACCESSORY


def block_roles(workout: Workout, catalog: Catalog) -> list[BlockRole | None]:
    """Each block's role: as stated, or inferred (`_inferred_role`)."""
    roles: list[BlockRole | None] = []
    opening = True
    for block in workout.blocks:
        role = block.role or _inferred_role(block, catalog, opening=opening)
        opening = opening and role == BlockRole.WARM_UP
        roles.append(role)
    return roles


def _working_exercises(workout: Workout, catalog: Catalog) -> tuple[list[Exercise], list[Exercise]]:
    """(the working exercises, the warm-up ones), each once. A workout of nothing but
    warm-up counts its warm-up as the work."""
    roles = block_roles(workout, catalog)
    working: dict[str, Exercise] = {}
    warm: dict[str, Exercise] = {}
    for block, role in zip(workout.blocks, roles, strict=True):
        target = warm if role == BlockRole.WARM_UP else working
        for item in block.items:
            exercise = catalog.by_id(item.exercise_id)
            if exercise is not None:
                target.setdefault(exercise.id, exercise)
    if not working:
        return list(warm.values()), []
    return list(working.values()), list(warm.values())


def coverage(quality: Quality, exercises: Sequence[Exercise]) -> tuple[int, int]:
    """(exercises that directly train `quality`, exercises that only support it)."""
    direct = sum(1 for exercise in exercises if quality in exercise.qualities)
    supporting = sum(
        1
        for exercise in exercises
        if quality not in exercise.qualities
        and any(own in SUPPORTS.get(quality, frozenset()) for own in exercise.qualities)
    )
    return direct, supporting


def _coverage_findings(workout: Workout, ctx: ReviewContext) -> list[GuardVerdict]:
    findings: list[GuardVerdict] = []
    key = workout.key
    working, warm = _working_exercises(workout, ctx.catalog)
    for position, quality in enumerate(workout.qualities):
        direct, supporting = coverage(quality, working)
        if direct == 0:
            only_warm = coverage(quality, warm)[0] > 0
            findings.append(
                _finding(
                    "goal_coverage",
                    f"workout {key} is for {quality.value}, but none of its exercises "
                    f"directly trains {quality.value}"
                    + (" (only its warm-up does)" if only_warm else ""),
                )
            )
            continue
        score = direct + 0.5 * supporting
        if position == 0 and score < ctx.limits.main_objective_coverage:
            findings.append(
                _finding(
                    "weak_coverage",
                    f"workout {key}'s main objective is {quality.value}, but only {direct} "
                    f"working exercise trains it and nothing else in the session supports it; "
                    f"add work that develops {quality.value} or make it a secondary objective",
                )
            )
    return findings


def _role_findings(workout: Workout, ctx: ReviewContext) -> list[GuardVerdict]:
    """Stated roles against the exercises, a technique drill as the warm-up, and order."""
    findings: list[GuardVerdict] = []
    catalog = ctx.catalog
    key = workout.key
    roles = block_roles(workout, catalog)
    seen_main = False
    tiring: str | None = None  # the first strength or conditioning block's first exercise
    for block, role in zip(workout.blocks, roles, strict=True):
        exercises = [
            (item, exercise)
            for item in block.items
            if (exercise := catalog.by_id(item.exercise_id)) is not None
        ]
        ids = ", ".join(exercise.id for _, exercise in exercises)
        if role == BlockRole.WARM_UP:
            for item, exercise in exercises:
                if exercise.specific:
                    findings.append(
                        _finding(
                            "role",
                            f"workout {key}: {exercise.id} is a narrow technique drill, not "
                            "general preparation; warm up with a general activity and keep "
                            "the drill for skill work",
                        )
                    )
                elif is_heavy(item, exercise):
                    findings.append(
                        _finding("role", f"workout {key}: heavy {exercise.id} in the warm-up block")
                    )
            if seen_main:
                findings.append(
                    _finding("order", f"workout {key}: a warm-up ({ids}) after the main work")
                )
        else:
            seen_main = True
        stated = block.role
        wanted = _ROLE_QUALITIES.get(stated) if stated is not None else None
        if stated is not None and wanted is not None:
            for item, exercise in exercises:
                carry = intensity_mode(exercise, item.measure) == IntensityMode.CARRY
                fits = bool(wanted & set(exercise.qualities)) or (
                    stated == BlockRole.CONDITIONING and carry
                )
                if not fits:
                    findings.append(
                        _finding(
                            "role",
                            f"workout {key}: {exercise.id} doesn't train what its "
                            f"{stated.value} block is for (it trains "
                            f"{', '.join(q.value for q in exercise.qualities)})",
                        )
                    )
        if role == BlockRole.POWER and tiring is not None:
            findings.append(
                _finding(
                    "order",
                    f"workout {key}: {ids} (power/speed) comes after {tiring}; put fast work "
                    "before strength and conditioning work, while fresh",
                )
            )
        if role in (BlockRole.STRENGTH, BlockRole.CONDITIONING) and tiring is None and exercises:
            tiring = (
                f"{'heavy ' if any(is_heavy(i, e) for i, e in exercises) else ''}"
                f"{exercises[0][1].id}"
            )
    return findings


def _prescription_findings(workout: Workout, ctx: ReviewContext) -> list[GuardVerdict]:
    """ADR 0011: power and speed work stays fast — short sets, enough rest between them."""
    findings: list[GuardVerdict] = []
    limits = ctx.limits
    roles = block_roles(workout, ctx.catalog)
    for block, role in zip(workout.blocks, roles, strict=True):
        if role in (BlockRole.WARM_UP, BlockRole.CONDITIONING):
            continue
        for item in block.items:
            exercise = ctx.catalog.by_id(item.exercise_id)
            if exercise is None or intensity_mode(exercise, item.measure) != IntensityMode.SPEED:
                continue
            if (
                item.measure == Measure.REPS
                and item.reps_max is not None
                and item.reps_max > limits.power_reps_max
                and Quality.CONDITIONING not in exercise.qualities
                and exercise.qualities[0] != Quality.AGILITY
            ):
                findings.append(
                    _finding(
                        "prescription",
                        f"workout {workout.key}: {exercise.id} is power work; past about "
                        f"{limits.power_reps_max} reps a set stops being fast — use fewer reps "
                        "per set",
                    )
                )
            if block.kind == "single" and item.rest_seconds < limits.power_rest_min_seconds:
                findings.append(
                    _finding(
                        "prescription",
                        f"workout {workout.key}: {exercise.id} is power/speed work with "
                        f"{item.rest_seconds} s rest; give it at least "
                        f"{limits.power_rest_min_seconds} s so each set stays fast",
                    )
                )
    return findings


def _work_seconds(item: Prescription, limits: WorkloadLimits) -> float:
    if item.measure == Measure.TIME and item.seconds is not None:
        return float(item.seconds)
    if item.measure == Measure.DISTANCE and item.meters is not None:
        return item.meters * limits.seconds_per_meter
    return (item.reps_max or 0) * limits.seconds_per_rep


def estimated_minutes(workout: Workout, limits: WorkloadLimits = DEFAULT_LIMITS) -> float:
    """A coarse session length: work, rests and transitions, and a minute of setup per
    block. A superset's round is its work plus transitions plus the round's rest."""
    total = 0.0
    for block in workout.blocks:
        total += limits.block_setup_seconds
        if block.kind == "superset":
            rounds = max(item.sets for item in block.items)
            per_round = sum(_work_seconds(item, limits) for item in block.items) + sum(
                item.rest_seconds for item in block.items
            )
            total += rounds * per_round
        else:
            item = block.items[0]
            total += item.sets * (_work_seconds(item, limits) + item.rest_seconds)
    return total / 60


def _workout_findings(workout: Workout, ctx: ReviewContext) -> list[GuardVerdict]:
    findings: list[GuardVerdict] = []
    catalog = ctx.catalog
    resolved = [
        (index, item, exercise)
        for index, item in _items(workout)
        if (exercise := catalog.by_id(item.exercise_id)) is not None
    ]
    key = workout.key

    findings.extend(_coverage_findings(workout, ctx))
    if ctx.require_declared and not workout.qualities:
        findings.append(
            _finding("declared", f"workout {key}: state the qualities it trains (qualities)")
        )

    heavy = [(index, item) for index, item, exercise in resolved if is_heavy(item, exercise)]
    if heavy:
        roles = block_roles(workout, catalog)
        if not roles or roles[0] != BlockRole.WARM_UP:
            findings.append(
                _finding(
                    "warm_up",
                    f"workout {key}: heavy lifting ({heavy[0][1].exercise_id}) with no "
                    "warm-up block first (mobility or easy cardio for what follows)",
                )
            )
    findings.extend(_role_findings(workout, ctx))
    findings.extend(_prescription_findings(workout, ctx))

    if ctx.session_minutes:
        minutes = estimated_minutes(workout, ctx.limits)
        if minutes > ctx.session_minutes * ctx.limits.duration_tolerance:
            findings.append(
                _finding(
                    "duration",
                    f"workout {key} takes about {minutes:.0f} min, the session is "
                    f"{ctx.session_minutes} min; drop or shorten blocks",
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
    findings.extend(_split_findings(plan, ctx))
    return findings


def _split_findings(plan: Plan, ctx: ReviewContext) -> list[GuardVerdict]:
    """ADR 0011: with two or three sessions a week, a body region done in only one of them is
    trained once a week — a split. Fine when the user asked for one; otherwise sessions that
    each cover the main regions with different emphases spread the load better. Only regions
    with real weekly volume (`split_region_sets`) count."""
    keys = [day.workout_key for day in plan.schedule]
    if ctx.split_wanted or not 2 <= len(keys) <= 3 or len(set(keys)) < 2:
        return []
    by_key = {workout.key: workout for workout in plan.workouts}
    findings: list[GuardVerdict] = []
    for region, patterns in _REGIONS.items():
        sets_by_day: list[int] = []
        for workout_key in keys:
            workout = by_key.get(workout_key)
            sets = 0
            if workout is not None:
                for _, item in _items(workout):
                    exercise = ctx.catalog.by_id(item.exercise_id)
                    if exercise is not None and exercise.pattern in patterns:
                        sets += item.sets
            sets_by_day.append(sets)
        trained_days = [sets for sets in sets_by_day if sets > 0]
        if len(trained_days) == 1 and trained_days[0] >= ctx.limits.split_region_sets:
            only = keys[next(i for i, sets in enumerate(sets_by_day) if sets > 0)]
            findings.append(
                _finding(
                    "split",
                    f"{region} is trained only in workout {only} ({trained_days[0]} sets) of "
                    f"{len(keys)} weekly sessions; with this frequency, give each session some "
                    "of the main regions with a different emphasis, unless a split is intended",
                )
            )
    return findings


def review(plan: Plan, ctx: ReviewContext) -> list[GuardVerdict]:
    """Every finding for `plan` (failing `quality.*` verdicts), empty when there's nothing
    to fix. Pure."""
    findings: list[GuardVerdict] = []
    for workout in plan.workouts:
        findings.extend(_workout_findings(workout, ctx))
    # ADR 0011: the warm-up doesn't count (mobility excepted: it is what a warm-up trains).
    working: list[Exercise] = []
    warm: list[Exercise] = []
    for workout in plan.workouts:
        work, warm_up = _working_exercises(workout, ctx.catalog)
        working.extend(work)
        warm.extend(warm_up)
    trained = {quality for exercise in working for quality in exercise.qualities}
    trained |= {Quality.MOBILITY for exercise in warm if Quality.MOBILITY in exercise.qualities}
    for quality in ctx.requested:
        if quality not in trained:
            findings.append(
                _finding(
                    "request_coverage",
                    f"the request asks for {quality.value}; no working exercise in the plan "
                    f"directly trains it (see exercise_facts qualities)",
                )
            )
    findings.extend(_weekly_findings(plan, ctx))
    return findings
