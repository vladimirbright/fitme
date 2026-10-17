"""ADR 0016: session time estimates and the time budget, the weighted weekly workload, the
main objective's priority, and progression of work the load engine doesn't progress (plus
speed work, which the engine now holds). Fixed plans and logs through the real catalog; no
model is called."""

from __future__ import annotations

import pytest
from test_plan_quality import plan, rules, rx, single, superset, workout

from fitme.catalog import load_catalog
from fitme.db.records import SessionOutcome, SetLogRecord
from fitme.domain.catalog import Exercise
from fitme.domain.enums import BlockRole, CheckinAnswer, Quality, Sport
from fitme.domain.models import Block, Load, Prescription, Workout
from fitme.domain.results import ChangeReps, ChangeTarget
from fitme.services import programming, progression, session_time, time_budget, workload
from fitme.services.loads import ExerciseHistory, LoadKind, next_load
from fitme.services.plan_quality import ReviewContext, block_roles, review, weekly_load
from fitme.services.progression import TargetKind
from fitme.services.workload import Region, Stimulus

_CATALOG = load_catalog()
_MODEL = session_time.DEFAULT_MODEL


def _exercise(exercise_id: str) -> Exercise:
    exercise = _CATALOG.by_id(exercise_id)
    assert exercise is not None, exercise_id
    return exercise


def role(kind: BlockRole, *items: Prescription) -> Block:
    return Block(kind="superset" if len(items) > 1 else "single", role=kind, items=list(items))


def _seconds(*blocks: Block) -> float:
    return session_time.estimate(workout("A", "W", list(blocks)), _CATALOG).seconds


# --- Session duration --------------------------------------------------------------------------


def test_a_reps_set_counts_work_and_the_rests_between_sets_only() -> None:
    # Barbell bench press 4 × 6, 120 s rest: setup 120 + 4 × 6 × 3.5 + 3 × 120.
    bench = rx("barbell_bench_press", 4, reps=(4, 6), rest=120)
    assert _seconds(*single(bench)) == pytest.approx(120 + 4 * 6 * 3.5 + 3 * 120)


def test_a_timed_set_uses_its_own_duration_not_reps() -> None:
    # Jump rope 3 × 2 min, 45 s rest: setup 45 + 3 × 120 + 2 × 45.
    rope = rx("jump_rope_skipping", 3, seconds=120, rest=45)
    assert _seconds(*single(rope)) == pytest.approx(45 + 3 * 120 + 2 * 45)


def test_distance_runs_at_running_pace_and_carries_at_walking_pace() -> None:
    shuttle = rx("shuttle_run", 4, meters=40, rest=60)
    carry = rx("dumbbell_farmer_carry", 3, meters=40, rest=60)
    assert _seconds(*single(shuttle)) == pytest.approx(15 + 4 * 40 * 0.4 + 3 * 60)
    assert _seconds(*single(carry)) == pytest.approx(45 + 3 * 40 * 1.0 + 2 * 60)


def test_a_unilateral_exercise_counts_both_sides() -> None:
    row = _exercise("dumbbell_row")
    assert row.unilateral
    # 3 × 10 per arm, 75 s rest: each set is 10 reps per side plus a switch.
    item = rx("dumbbell_row", 3, reps=(10, 10), rest=75)
    per_set = 2 * 10 * 3.5 + _MODEL.side_switch_seconds
    assert session_time.set_work_seconds(item, row) == pytest.approx(per_set)
    assert _seconds(*single(item)) == pytest.approx(45 + 3 * per_set + 2 * 75)
    # An alternating or two-sided exercise is not doubled.
    assert not _exercise("dumbbell_bench_press").unilateral


def test_a_superset_counts_transitions_and_the_round_rest_between_rounds_only() -> None:
    block = superset(
        rx("dumbbell_bench_press", 3, reps=(10, 10), rest=20),
        rx("plank", 3, seconds=40, rest=90),
    )
    work = 10 * 3.5 + 40
    # setup 45 (dumbbells) + 3 rounds × (work + 20 s transition) + 2 round rests.
    assert _seconds(block) == pytest.approx(45 + 3 * (work + 20) + 2 * 90)
    # The same exercises as two single blocks rest after every set but the last of each.
    apart = single(
        rx("dumbbell_bench_press", 3, reps=(10, 10), rest=90), rx("plank", 3, seconds=40, rest=90)
    )
    assert _seconds(block) < _seconds(*apart)


def test_warm_up_cool_down_and_equipment_setup_are_part_of_the_session() -> None:
    warm = role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0))
    cool = role(BlockRole.MOBILITY, rx("mobility_cat_cow", 1, seconds=120, rest=0))
    lift = role(BlockRole.STRENGTH, rx("barbell_back_squat", 3, reps=(5, 5), rest=150))
    full = session_time.estimate(workout("A", "W", [warm, lift, cool]), _CATALOG)
    assert full.block_seconds[0] == pytest.approx(45 + 300)  # the rope is equipment
    assert full.block_seconds[2] == pytest.approx(15 + 120)  # nothing to set up
    assert full.block_seconds[1] == pytest.approx(120 + 3 * 5 * 3.5 + 2 * 150)  # loading a bar


def test_four_heavy_lifts_can_take_longer_than_an_eight_exercise_circuit() -> None:
    heavy = workout(
        "A",
        "Strength",
        single(
            *[
                rx(lift, 5, reps=(3, 5), rest=180)
                for lift in (
                    "barbell_back_squat",
                    "barbell_bench_press",
                    "barbell_deadlift",
                    "barbell_bent_over_row",
                )
            ]
        ),
    )
    circuit = workout(
        "B",
        "Circuit",
        [
            superset(
                *[
                    rx(name, 3, seconds=30, rest=10)
                    for name in (
                        "bodyweight_jumping_jacks",
                        "mountain_climbers",
                        "bodyweight_burpee",
                        "plank",
                    )
                ]
            ),
            superset(
                rx("pushup", 3, reps=(10, 10), rest=10, load={"kind": "bodyweight"}),
                rx("bodyweight_split_squat", 3, reps=(10, 10), rest=10),
                rx("side_plank", 3, seconds=20, rest=10),
                rx("march_in_place", 3, seconds=30, rest=60),
            ),
        ],
    )
    assert len(heavy.blocks) == 4 and sum(len(b.items) for b in circuit.blocks) == 8
    assert (
        session_time.estimate(heavy, _CATALOG).minutes
        > session_time.estimate(circuit, _CATALOG).minutes
    )


def _long_boxing_friday() -> Workout:
    """The observed Friday: footwork and endurance, plus three strength lifts."""
    return workout(
        "B",
        "Foot speed + endurance",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
            role(BlockRole.POWER, rx("stance_footwork_drill", 5, seconds=30, rest=60)),
            role(BlockRole.POWER, rx("lateral_line_hop", 4, seconds=15, rest=60)),
            role(BlockRole.POWER, rx("shuttle_run", 4, meters=40, rest=90)),
            role(BlockRole.STRENGTH, rx("barbell_deadlift", 3, reps=(4, 6), rest=180)),
            role(BlockRole.ACCESSORY, rx("dumbbell_seated_shoulder_press", 3, reps=(8, 10))),
            role(BlockRole.ACCESSORY, rx("machine_lat_pulldown", 3, reps=(8, 10))),
            role(BlockRole.CONDITIONING, rx("battle_rope_alternating_waves", 6, seconds=30)),
            role(BlockRole.MOBILITY, rx("mobility_cat_cow", 1, seconds=120, rest=0)),
        ],
        [Quality.AGILITY, Quality.CONDITIONING],
    )


def test_an_over_budget_workout_is_shortened_by_priority_without_cutting_rest() -> None:
    friday = _long_boxing_friday()
    rests = {item.exercise_id: item.rest_seconds for block in friday.blocks for item in block.items}
    # 45 min: dropping one accessory that serves no objective is enough; nothing else moves.
    lighter = _long_boxing_friday()
    fired = time_budget.fit_workout(lighter, _CATALOG, 45)
    assert fired[0].detail.count("removed") == 1 and "sets" not in fired[0].detail
    # 40 min: both redundant accessories go, then volume, never the main objective's.
    before = session_time.estimate(friday, _CATALOG).minutes
    assert before > 40 * _MODEL.tolerance
    fired = time_budget.fit_workout(friday, _CATALOG, 40)
    after = session_time.estimate(friday, _CATALOG).minutes
    assert after <= 40 * _MODEL.tolerance < before
    assert [verdict.rule for verdict in fired] == ["plan.time_budget"] and fired[0].ok
    kept = [item.exercise_id for block in friday.blocks for item in block.items]
    # The warm-up and the main objective's footwork are kept, at their full volume.
    assert kept[0] == "jump_rope_skipping"
    for drill, sets in (("stance_footwork_drill", 5), ("lateral_line_hop", 4)):
        assert drill in kept
        assert (
            next(i for b in friday.blocks for i in b.items if i.exercise_id == drill).sets == sets
        )
    # Off-objective accessories go before anything else.
    assert "dumbbell_seated_shoulder_press" not in kept
    assert "machine_lat_pulldown" not in kept
    # No rest was shortened anywhere.
    assert all(i.rest_seconds == rests[i.exercise_id] for b in friday.blocks for i in b.items)


def test_a_workout_that_fits_is_left_alone() -> None:
    short = workout(
        "A",
        "Strength",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            role(BlockRole.STRENGTH, rx("barbell_back_squat", 3, reps=(5, 5), rest=150)),
        ],
        [Quality.STRENGTH],
    )
    before = short.model_copy(deep=True)
    assert time_budget.fit_workout(short, _CATALOG, 60) == []
    assert short == before


def test_trimming_drops_a_secondary_objective_it_no_longer_trains() -> None:
    session = workout(
        "A",
        "Strength + core",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            *[
                role(BlockRole.STRENGTH, rx(lift, 5, reps=(3, 5), rest=180))
                for lift in ("barbell_back_squat", "barbell_bench_press", "barbell_deadlift")
            ],
            role(BlockRole.CORE, rx("plank", 3, seconds=45, rest=60)),
        ],
        [Quality.STRENGTH, Quality.CORE_STABILITY],
    )
    fired = time_budget.fit_workout(session, _CATALOG, 30)
    assert all(item.exercise_id != "plank" for block in session.blocks for item in block.items)
    assert session.qualities == [Quality.STRENGTH]
    assert "secondary objective dropped: core_stability" in fired[0].detail
    # The main objective's lifts are all still there, whatever the remaining length.
    assert sum(1 for block in session.blocks if block.role == BlockRole.STRENGTH) == 3


def test_the_review_reports_a_session_beyond_the_tolerance() -> None:
    friday = _long_boxing_friday()
    ctx = ReviewContext(_CATALOG, session_minutes=45)
    found = [f for f in review(plan([(4, "B")], friday), ctx) if f.rule == "quality.duration"]
    assert found and "(estimated)" in found[0].detail
    assert "quality.duration" not in rules(
        review(plan([(4, "B")], friday), ReviewContext(_CATALOG))
    )


# --- Weekly workload ------------------------------------------------------------------------


def test_sets_are_weighted_by_stimulus_not_counted_alike() -> None:
    def units(item: Prescription, block_role: BlockRole | None = None) -> float:
        exercise = _exercise(item.exercise_id)
        stimulus = workload.stimulus_of(item, exercise, block_role)
        return workload.set_units(item, exercise, stimulus)

    heavy = rx("barbell_deadlift", 1, reps=(3, 5))
    drill = rx("stance_footwork_drill", 1, seconds=30)
    rope = rx("battle_rope_alternating_waves", 1, seconds=30)
    stretch = rx("mobility_cat_cow", 1, seconds=60)
    assert units(heavy) == 1.0
    assert units(drill) == 0.25
    assert units(rope) == pytest.approx(0.2)
    assert units(stretch) == 0.0
    assert units(rx("jump_rope_skipping", 1, seconds=300), BlockRole.WARM_UP) == 0.0
    assert workload.stimulus_of(drill, _exercise(drill.exercise_id), None) == Stimulus.TECHNICAL
    hop = rx("lateral_line_hop", 1, reps=(6, 6))
    assert workload.stimulus_of(hop, _exercise(hop.exercise_id), None) == Stimulus.PLYOMETRIC
    swing = rx("kettlebell_swing", 1, reps=(10, 10))
    assert workload.stimulus_of(swing, _exercise(swing.exercise_id), None) == Stimulus.POWER


def test_a_kettlebell_swing_counts_as_hip_hinge_work_although_filed_as_conditioning() -> None:
    swing = _exercise("kettlebell_swing")
    assert swing.pattern.value == "conditioning"
    assert Region.HINGE in workload.regions_of(swing, Stimulus.POWER)
    assert Region.SHOULDER not in workload.regions_of(swing, Stimulus.POWER)
    ropes = _exercise("battle_rope_alternating_waves")
    assert workload.regions_of(ropes, Stimulus.CONDITIONING) == {Region.SHOULDER}


def _observed_two_day_week() -> list[Workout]:
    """The two-day boxing-support program from the report (warm-up and cool-down added)."""
    tuesday = workout(
        "A",
        "Power + strength",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
            role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 4, reps=(4, 5))),
            role(BlockRole.POWER, rx("kettlebell_swing", 4, reps=(10, 12), rest=60)),
            role(BlockRole.STRENGTH, rx("barbell_bench_press", 4, reps=(4, 6), rest=150)),
            role(BlockRole.STRENGTH, rx("barbell_romanian_deadlift", 3, reps=(6, 8), rest=120)),
            role(BlockRole.ACCESSORY, rx("dumbbell_row", 3, reps=(8, 10))),
            role(BlockRole.CORE, rx("cable_rotation", 3, reps=(10, 12), rest=45)),
        ],
        [Quality.POWER, Quality.STRENGTH],
    )
    return [tuesday, _long_boxing_friday()]


def _boxing(sessions: int = 3, minutes: int | None = None) -> ReviewContext:
    return ReviewContext(
        _CATALOG,
        sport=Sport.BOXING_MARTIAL_ARTS,
        sport_sessions_per_week=sessions,
        session_minutes=minutes,
    )


def test_the_observed_week_reports_hinge_overlap_and_an_unfocused_friday() -> None:
    week = plan([(1, "A"), (4, "B")], *_observed_two_day_week())
    found = review(week, _boxing())
    hinge = [f.detail for f in found if f.rule == "quality.hinge_overlap"]
    assert hinge and all(
        name in hinge[0]
        for name in ("kettlebell_swing", "barbell_romanian_deadlift", "barbell_deadlift")
    )
    off = [f.detail for f in found if f.rule == "quality.off_objective"]
    assert off and off[0].startswith("workout B is for agility, conditioning")
    assert "dumbbell_seated_shoulder_press" in off[0] and "machine_lat_pulldown" in off[0]
    assert "mobility_cat_cow" not in off[0]  # the cool-down is not off-objective work
    # Warnings for the repair round, never a refusal: every finding is a quality.* verdict.
    assert all(f.rule.startswith("quality.") and not f.ok for f in found)


def test_shoulder_load_from_presses_pulls_and_ropes_is_limited_more_with_boxing() -> None:
    upper = workout(
        "A",
        "Upper",
        single(
            rx("dumbbell_bench_press", 4),
            rx("dumbbell_seated_shoulder_press", 4),
            rx("dumbbell_row", 4),
            rx("battle_rope_alternating_waves", 6, seconds=30, rest=30),
        ),
    )
    week = plan([(0, "A"), (3, "A")], upper)
    assert "quality.shoulder_volume" not in rules(review(week, ReviewContext(_CATALOG)))
    detail = next(f.detail for f in review(week, _boxing(5)) if f.rule == "quality.shoulder_volume")
    assert "with the user's other sport" in detail and "battle_rope_alternating_waves" in detail


def test_demanding_hinge_days_back_to_back_are_reported_whatever_the_exercise() -> None:
    hinge_day = workout(
        "A",
        "Hinge",
        [
            role(BlockRole.WARM_UP, rx("mobility_cat_cow", 1, seconds=60, rest=0)),
            role(BlockRole.POWER, rx("kettlebell_swing", 5, reps=(10, 12), rest=60)),
        ],
    )
    back_to_back = review(plan([(1, "A"), (2, "A")], hinge_day), ReviewContext(_CATALOG))
    assert any(
        f.rule == "quality.consecutive_heavy" and "hip hinge" in f.detail for f in back_to_back
    )
    apart = review(plan([(1, "A"), (3, "A")], hinge_day), ReviewContext(_CATALOG))
    assert "quality.consecutive_heavy" not in rules(apart)


def test_a_legitimate_high_volume_hypertrophy_week_gets_warnings_at_most() -> None:
    def day(key: str, lifts: list[str]) -> Workout:
        return workout(
            key,
            "Hypertrophy",
            [
                role(BlockRole.WARM_UP, rx("mobility_cat_cow", 1, seconds=60, rest=0)),
                *[role(BlockRole.STRENGTH, rx(lift, 4, reps=(8, 12), rest=90)) for lift in lifts],
            ],
            [Quality.HYPERTROPHY],
        )

    upper = day(
        "A",
        [
            "barbell_bench_press",
            "barbell_bent_over_row",
            "dumbbell_seated_shoulder_press",
            "machine_lat_pulldown",
        ],
    )
    lower = day("B", ["barbell_back_squat", "barbell_romanian_deadlift", "dumbbell_step_up"])
    week = plan([(0, "A"), (1, "B"), (3, "A"), (4, "B")], upper, lower)
    found = review(
        week, ReviewContext(_CATALOG, requested=[Quality.HYPERTROPHY], split_wanted=True)
    )
    assert all(f.rule.startswith("quality.") for f in found)
    assert "quality.off_objective" not in rules(found)
    assert "quality.hinge_overlap" not in rules(found)  # two hinges a week are fine


def test_the_weekly_load_is_reported_per_session_and_region() -> None:
    week = plan([(1, "A"), (4, "B")], *_observed_two_day_week())
    summary = workload.summary(weekly_load(week, _CATALOG))
    sessions = summary["sessions"]
    assert isinstance(sessions, list) and [s["weekday"] for s in sessions] == [1, 4]
    totals = summary["week"]
    assert isinstance(totals, dict) and totals["hip hinge"] > 0 and totals["shoulders"] > 0


def test_the_brief_spreads_the_main_objectives_over_the_sessions() -> None:
    assert programming.session_objectives(
        2, [Quality.POWER, Quality.AGILITY, Quality.STRENGTH]
    ) == [
        "power",
        "agility",
    ]
    assert programming.session_objectives(3, [Quality.STRENGTH]) == ["strength"] * 3
    assert programming.session_objectives(2, []) == []


# --- Progression ----------------------------------------------------------------------------


_BENCH = _exercise("barbell_bench_press")


def _outcome(load: float, *, hit: bool, below: bool) -> SessionOutcome:
    return SessionOutcome(
        load_kg=load, planned_load_kg=load, hit_reps_max=hit, below_reps_min=below
    )


def _next(exercise: Exercise, *sessions: SessionOutcome, history_max: float = 62.5) -> Load:
    decision = next_load(
        exercise,
        ExerciseHistory(history_max_kg=history_max, sessions=sessions),
        {"shoulder": CheckinAnswer.FINE, "elbow_wrist": CheckinAnswer.FINE},
        [],
        cap_kg=2.5,
        flagged_areas=frozenset(),
    )
    return decision.load


def test_strength_progression_is_earned_and_held_when_reps_fall_short() -> None:
    # 4 × 4-6 at 60 kg: 6, 6, 6, 6 -> one increment.
    assert _next(_BENCH, _outcome(60, hit=True, below=False), history_max=60) == Load(
        kind="kg", kg=62.5
    )
    # 6, 6, 5, 4: inside the range but not at the top -> the same load, aim for more reps.
    assert _next(_BENCH, _outcome(62.5, hit=False, below=False)) == Load(kind="kg", kg=62.5)
    # Under the range once: hold; twice in a row: down, to the barbell's step.
    assert _next(_BENCH, _outcome(62.5, hit=False, below=True)) == Load(kind="kg", kg=62.5)
    assert _next(
        _BENCH, _outcome(62.5, hit=False, below=True), _outcome(62.5, hit=False, below=True)
    ) == Load(kind="kg", kg=55.0)


def test_power_work_gets_no_strength_style_load_increase() -> None:
    for exercise_id in ("medicine_ball_rotational_side_throw", "kettlebell_swing"):
        exercise = _exercise(exercise_id)
        decision = next_load(
            exercise,
            ExerciseHistory(history_max_kg=16.0, sessions=[_outcome(16.0, hit=True, below=False)]),
            {area: CheckinAnswer.FINE for area in exercise.loads_areas},
            [],
            cap_kg=2.5,
            flagged_areas=frozenset(),
        )
        assert decision.kind == LoadKind.HOLD_SPEED
        assert decision.load == Load(kind="kg", kg=16.0)
        # Short sets on fast work are not a reason to cut the load either.
        twice_short = next_load(
            exercise,
            ExerciseHistory(
                history_max_kg=16.0,
                sessions=[
                    _outcome(16.0, hit=False, below=True),
                    _outcome(16.0, hit=False, below=True),
                ],
            ),
            {},
            [],
            cap_kg=2.5,
            flagged_areas=frozenset(),
        )
        assert twice_short.load == Load(kind="kg", kg=16.0)


def test_power_work_without_history_is_still_calibration() -> None:
    throw = _exercise("medicine_ball_rotational_side_throw")
    decision = next_load(
        throw, ExerciseHistory(history_max_kg=None, sessions=[]), {}, [], 2.5, flagged_areas=()
    )
    assert decision.load == Load(kind="calibration")


def _set(
    item: Prescription, value: int | None, *, skipped: bool = False, session: int = 1
) -> SetLogRecord:
    return SetLogRecord(
        id=0,
        session_id=session,
        exercise_id=item.exercise_id,
        set_index=1,
        planned_load_kg=None,
        planned_reps_min=item.reps_min,
        planned_reps_max=item.reps_max,
        actual_load_kg=None,
        actual_reps=value if item.measure.value == "reps" else None,
        skipped=skipped,
        rpe=None,
        source="button",
        created_at="",
        planned_seconds=item.seconds,
        planned_meters=item.meters,
        actual_seconds=value if item.measure.value == "time" else None,
        actual_meters=value if item.measure.value == "distance" else None,
    )


def _session(item: Prescription, *values: int | None) -> list[SetLogRecord]:
    return [_set(item, value, skipped=value is None) for value in values]


def test_bodyweight_reps_progress_after_two_sessions_at_the_top_of_the_range() -> None:
    pushup = rx("pushup", 3, reps=(8, 12), load={"kind": "bodyweight"})
    exercise = _exercise("pushup")
    once = progression.next_target(pushup, exercise, [_session(pushup, 12, 12, 12)])
    assert once.kind == TargetKind.HOLD and once.change is None  # one session is not a trend
    twice = progression.next_target(
        pushup, exercise, [_session(pushup, 12, 12, 13), _session(pushup, 12, 12, 12)]
    )
    assert twice.kind == TargetKind.MORE
    assert twice.change == ChangeReps(exercise_id="pushup", reps_min=10, reps_max=14)
    mixed = progression.next_target(
        pushup, exercise, [_session(pushup, 12, 11, 10), _session(pushup, 12, 12, 12)]
    )
    assert mixed.kind == TargetKind.HOLD
    skipped = progression.next_target(
        pushup, exercise, [_session(pushup, 12, 12, None), _session(pushup, 12, 12, 12)]
    )
    assert skipped.change is None  # a skipped set never earns more


def test_sessions_at_another_target_are_not_evidence_for_this_one() -> None:
    pushup = rx("pushup", 3, reps=(10, 14), load={"kind": "bodyweight"})
    old = rx("pushup", 3, reps=(8, 12), load={"kind": "bodyweight"})
    decision = progression.next_target(
        pushup, _exercise("pushup"), [_session(pushup, 14, 14, 14), _session(old, 12, 12, 12)]
    )
    assert decision.kind == TargetKind.HOLD and decision.change is None


def test_repeated_underperformance_lowers_the_target_conservatively() -> None:
    pushup = rx("pushup", 3, reps=(8, 12), load={"kind": "bodyweight"})
    decision = progression.next_target(
        pushup, _exercise("pushup"), [_session(pushup, 9, 7, 6), _session(pushup, 8, 7, 6)]
    )
    assert decision.kind == TargetKind.LESS
    assert decision.change == ChangeReps(exercise_id="pushup", reps_min=6, reps_max=10)


def test_timed_and_distance_work_progress_one_variable_by_about_ten_percent() -> None:
    plank = rx("plank", 3, seconds=40, rest=45)
    decision = progression.next_target(
        plank, _exercise("plank"), [_session(plank, 40, 40, 45), _session(plank, 40, 40, 40)]
    )
    assert decision.change == ChangeTarget(exercise_id="plank", seconds=45)
    ropes = rx("battle_rope_alternating_waves", 6, seconds=30)
    decision = progression.next_target(
        ropes, _exercise(ropes.exercise_id), [_session(ropes, *[30] * 6)] * 2
    )
    assert decision.change == ChangeTarget(exercise_id=ropes.exercise_id, seconds=35)
    carry = rx("dumbbell_farmer_carry", 3, meters=40, rest=60)
    decision = progression.next_target(
        carry, _exercise(carry.exercise_id), [_session(carry, 40, 40, 40)] * 2
    )
    assert decision.change == ChangeTarget(exercise_id=carry.exercise_id, meters=45)


def test_at_the_top_of_a_useful_range_the_next_step_is_a_variation_not_more() -> None:
    plank = rx("plank", 3, seconds=120, rest=45)
    decision = progression.next_target(
        plank, _exercise("plank"), [_session(plank, 120, 120, 120)] * 2
    )
    assert decision.kind == TargetKind.VARIATION and decision.change is None


def test_speed_and_footwork_drills_keep_their_volume() -> None:
    drill = rx("stance_footwork_drill", 5, seconds=30, rest=60)
    decision = progression.next_target(
        drill, _exercise(drill.exercise_id), [_session(drill, *[30] * 5)] * 2
    )
    assert decision.kind == TargetKind.SPEED and decision.change is None


def test_kg_lifts_in_reps_are_left_to_the_load_engine() -> None:
    bench = rx("barbell_bench_press", 4, reps=(4, 6), load={"kind": "kg", "kg": 60.0})
    assert not progression.applies(bench, _BENCH)
    assert progression.next_target(bench, _BENCH, []).kind == TargetKind.NOT_APPLICABLE


def test_block_roles_feed_the_weekly_load() -> None:
    week = plan([(1, "A")], _observed_two_day_week()[0])
    roles = block_roles(week.workouts[0], _CATALOG)
    session = workload.session_load(week.workouts[0], _CATALOG, roles, 1)
    assert session.stimulus_units(Stimulus.MOBILITY) == 0
    assert session.region_units(Region.HINGE) == pytest.approx(4 * 0.6 + 3 * 0.75)


def test_no_unilateral_exercise_is_done_alternating() -> None:
    """A unilateral target is per side; an alternating move's reps are counted in total."""
    alternating = [
        exercise.id
        for exercise in _CATALOG.exercise
        if exercise.unilateral
        and any(
            word in exercise.instructions["en"].lower()
            for word in ("alternat", "each rep", "every rep")
        )
    ]
    assert alternating == []


def test_a_skipped_set_makes_a_session_inconclusive_not_short() -> None:
    pushup = rx("pushup", 3, reps=(8, 12), load={"kind": "bodyweight"})
    partial = _session(pushup, 6, None, 9)  # one short set next to a skipped one
    decision = progression.next_target(pushup, _exercise("pushup"), [partial, partial])
    assert decision.kind == TargetKind.HOLD and decision.change is None


def test_the_recap_model_cannot_suggest_a_timed_or_distance_target() -> None:
    from pydantic import ValidationError

    from fitme.domain.results import Recap

    with pytest.raises(ValidationError):
        Recap.model_validate(
            {
                "text": "",
                "suggestions": [{"kind": "change_target", "exercise_id": "plank", "seconds": 3600}],
            }
        )


def test_a_target_change_is_stale_once_the_plans_targets_differ() -> None:
    from fitme.services.recap import _one_target

    same = plan(
        [(0, "A"), (3, "B")],
        workout("A", "A", single(rx("plank", 3, seconds=30))),
        workout("B", "B", single(rx("plank", 3, seconds=30))),
    )
    edited = plan(
        [(0, "A"), (3, "B")],
        workout("A", "A", single(rx("plank", 3, seconds=30))),
        workout("B", "B", single(rx("plank", 3, seconds=90))),
    )
    change = ChangeTarget(exercise_id="plank", seconds=35)
    assert _one_target(same, change) and not _one_target(edited, change)


def test_the_active_workout_view_says_per_side_too() -> None:
    from fitme.bot.train_rendering import block_text
    from fitme.services.training import BlockView

    def view(*items: Prescription) -> str:
        block = Block(kind="single", items=list(items))
        shown = BlockView(session_id=1, index=0, total=1, workout_key="A", title="W", block=block)
        return block_text(shown, catalog=_CATALOG, lang="en")

    assert "Sets: 3 × 10 reps per side" in view(rx("dumbbell_row", 3, reps=(10, 10)))
    assert "Sets: 3 × 30 s per side" in view(rx("side_plank", 3, seconds=30))
    assert "per side" not in view(rx("dumbbell_bench_press", 3, reps=(10, 10)))
