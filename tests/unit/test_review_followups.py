"""PR #10 review follow-ups (ADR 0016): iterative, relevance-ranked time-budget reduction
toward the session length itself; one definition of objective relevance shared by the review
and the trimmer; resistance-aware target progression; plan- and prescription-scoped load
progression. Fixed plans and logs through the real catalog; no model is called."""

from __future__ import annotations

import math

import pytest
from test_plan_quality import plan, rx
from test_training_management import _exercise, _session, role, workout

from fitme.catalog import load_catalog
from fitme.db.records import SessionOutcome
from fitme.domain.enums import BlockRole, CheckinAnswer, Quality
from fitme.domain.models import Load, Workout
from fitme.domain.results import ChangeTarget
from fitme.services import progression, session_time, time_budget
from fitme.services.loads import ExerciseHistory, next_load, scope_history
from fitme.services.plan_quality import Relevance, ReviewContext, relevance, review
from fitme.services.progression import TargetKind

_CATALOG = load_catalog()


def _minutes(session: Workout) -> float:
    return session_time.estimate(session, _CATALOG).minutes


def _sets(session: Workout, exercise_id: str) -> int | None:
    return next(
        (i.sets for b in session.blocks for i in b.items if i.exercise_id == exercise_id), None
    )


def _power_session(squat_sets: int = 6) -> Workout:
    """Power first: a throw (primary), then a heavy squat that supports it."""
    return workout(
        "A",
        "Power",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 4, reps=(4, 5))),
            role(BlockRole.STRENGTH, rx("barbell_back_squat", squat_sets, reps=(4, 6), rest=150)),
        ],
        [Quality.POWER],
    )


def _set_seconds(exercise_id: str, sets_rest: int, reps: int) -> float:
    item = rx(exercise_id, 1, reps=(reps, reps), rest=sets_rest)
    return session_time.set_work_seconds(item, _exercise(exercise_id)) + sets_rest


# --- 1. Iterative reduction --------------------------------------------------------------------


def test_six_sets_come_down_to_three_without_removing_the_exercise() -> None:
    session = _power_session(6)
    per_set = _set_seconds("barbell_back_squat", 150, 6) / 60
    target = math.ceil(_minutes(session) - 3 * per_set)  # under three sets' worth too long
    fired = time_budget.fit_workout(session, _CATALOG, target)
    assert _sets(session, "barbell_back_squat") == 3
    assert "barbell_back_squat 6 → 3 sets" in fired[0].detail
    assert "removed" not in fired[0].detail
    assert _sets(session, "medicine_ball_rotational_side_throw") == 4  # the primary keeps all
    assert fired[0].ok and _minutes(session) <= target


def test_competing_accessories_lose_sets_evenly_by_priority_and_never_below_two() -> None:
    session = workout(
        "A",
        "Strength",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            role(BlockRole.STRENGTH, rx("barbell_back_squat", 4, reps=(4, 6), rest=180)),
            role(BlockRole.ACCESSORY, rx("dumbbell_lateral_raise", 5, reps=(10, 12))),
            role(BlockRole.ACCESSORY, rx("dumbbell_biceps_curl", 5, reps=(10, 12))),
        ],
        [Quality.STRENGTH],
    )
    per_set = _set_seconds("dumbbell_lateral_raise", 90, 12) / 60
    time_budget.fit_workout(session, _CATALOG, math.ceil(_minutes(session) - 3.5 * per_set))
    raise_sets = _sets(session, "dumbbell_lateral_raise")
    curl_sets = _sets(session, "dumbbell_biceps_curl")
    # Both support strength equally: they shrink together, never below two, and the main
    # lift is untouched.
    assert raise_sets is not None and curl_sets is not None
    assert abs(raise_sets - curl_sets) <= 1 and min(raise_sets, curl_sets) >= 2
    assert _sets(session, "barbell_back_squat") == 4


def test_a_block_is_removed_only_once_volume_is_at_the_minimum() -> None:
    session = _power_session(6)
    fired = time_budget.fit_workout(session, _CATALOG, 10)
    detail = fired[0].detail
    assert detail.index("barbell_back_squat 6 → 2 sets") < detail.index(
        "removed barbell_back_squat"
    )
    assert _sets(session, "medicine_ball_rotational_side_throw") == 4


def test_a_target_out_of_reach_without_the_main_objective_is_reported_not_forced() -> None:
    session = workout(
        "A",
        "Power",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=600, rest=0)),
            role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 8, reps=(5, 5))),
            role(BlockRole.POWER, rx("medicine_ball_overhead_slam", 8, reps=(5, 5))),
        ],
        [Quality.POWER],
    )
    before = session.model_copy(deep=True)
    fired = time_budget.fit_workout(session, _CATALOG, 15)
    assert fired and not fired[0].ok
    assert "the warm-up and the main objective's work, which are kept" in fired[0].detail
    assert session == before  # nothing protected was cut to get closer


# --- 5. The target is the session length -------------------------------------------------------


@pytest.mark.parametrize("target", [30, 45, 60])
def test_the_requested_length_is_the_target_not_a_limit_with_slack(target: int) -> None:
    session = workout(
        "A",
        "Strength",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            role(BlockRole.STRENGTH, rx("barbell_back_squat", 5, reps=(4, 6), rest=180)),
            role(BlockRole.STRENGTH, rx("barbell_bench_press", 5, reps=(4, 6), rest=180)),
            role(BlockRole.ACCESSORY, rx("dumbbell_lateral_raise", 5, reps=(10, 12))),
            role(BlockRole.ACCESSORY, rx("dumbbell_biceps_curl", 5, reps=(10, 12))),
            role(BlockRole.CORE, rx("plank", 5, seconds=45, rest=45)),
        ],
        [Quality.STRENGTH],
    )
    assert _minutes(session) > target
    fired = time_budget.fit_workout(session, _CATALOG, target)
    if fired[0].ok:
        assert _minutes(session) <= target
    else:  # only the warm-up and the main objective's lifts are left, at full volume
        kept = {i.exercise_id for b in session.blocks for i in b.items}
        assert kept == {"jump_rope_skipping", "barbell_back_squat", "barbell_bench_press"}
        assert _sets(session, "barbell_back_squat") == 5


def test_a_workout_slightly_over_loses_a_little_and_one_far_over_loses_more() -> None:
    def session() -> Workout:
        return _power_session(6)

    length = _minutes(session())
    slightly, far = session(), session()
    time_budget.fit_workout(slightly, _CATALOG, int(length) - 1)
    time_budget.fit_workout(far, _CATALOG, int(length) - 10)
    slight_sets, far_sets = _sets(slightly, "barbell_back_squat"), _sets(far, "barbell_back_squat")
    assert slight_sets is not None and slight_sets >= 5
    assert far_sets is None or far_sets < slight_sets


def test_the_review_warns_only_beyond_the_estimates_uncertainty() -> None:
    session = _power_session(6)
    length = _minutes(session)
    week = plan([(0, "A")], session)
    within = review(week, ReviewContext(_CATALOG, session_minutes=int(length / 1.1)))
    beyond = review(week, ReviewContext(_CATALOG, session_minutes=int(length / 1.3)))
    assert all(f.rule != "quality.duration" for f in within)
    assert any(f.rule == "quality.duration" for f in beyond)


# --- 2. One relevance for the review and the trimmer -------------------------------------------


def test_relevance_comes_from_catalog_qualities_and_their_support_relations() -> None:
    power = [Quality.POWER, Quality.CONDITIONING]
    throw = _exercise("medicine_ball_rotational_side_throw")
    squat = _exercise("barbell_back_squat")
    slam = _exercise("medicine_ball_overhead_slam")
    raise_ = _exercise("dumbbell_lateral_raise")
    rope = _exercise("battle_rope_alternating_waves")
    assert relevance(throw, power) == Relevance.PRIMARY
    assert relevance(slam, power) == Relevance.PRIMARY
    assert relevance(rope, power) == Relevance.SECONDARY
    assert relevance(squat, power) == Relevance.SUPPORTING  # strength supports power
    assert relevance(raise_, power) == Relevance.NONE
    assert relevance(raise_, [Quality.STRENGTH]) == Relevance.SUPPORTING


def test_the_trimmer_removes_what_the_review_calls_off_objective_and_keeps_support() -> None:
    session = workout(
        "A",
        "Power",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0)),
            role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 4, reps=(4, 5))),
            role(BlockRole.STRENGTH, rx("barbell_back_squat", 3, reps=(4, 6), rest=150)),
            role(BlockRole.ACCESSORY, rx("dumbbell_lateral_raise", 3, reps=(10, 12))),
            role(BlockRole.ACCESSORY, rx("dumbbell_biceps_curl", 3, reps=(10, 12))),
        ],
        [Quality.POWER],
    )
    flagged = next(
        f.detail
        for f in review(plan([(0, "A")], session), ReviewContext(_CATALOG))
        if f.rule == "quality.off_objective"
    )
    assert "dumbbell_lateral_raise" in flagged and "dumbbell_biceps_curl" in flagged
    assert "barbell_back_squat" not in flagged  # supporting work isn't off-objective
    target = _minutes(session) - 4
    fired = time_budget.fit_workout(session, _CATALOG, int(target))
    removed = [part for part in fired[0].detail.split("; ") if part.startswith("removed")]
    assert removed and all(
        name in ("removed dumbbell_lateral_raise", "removed dumbbell_biceps_curl")
        for name in removed
    )
    assert _sets(session, "barbell_back_squat") == 3  # the supporting lift kept its volume


# --- 4. Resistance-aware target progression ----------------------------------------------------


_CARRY = rx("dumbbell_farmer_carry", 3, meters=40, rest=60)


def _carry_target(*sessions: list) -> progression.TargetDecision:  # type: ignore[type-arg]
    return progression.next_target(_CARRY, _exercise("dumbbell_farmer_carry"), list(sessions))


def test_a_carry_at_the_same_load_twice_earns_more_distance() -> None:
    decision = _carry_target(
        _session(_CARRY, 40, 40, 40, kg=24.0), _session(_CARRY, 40, 40, 40, kg=24.0)
    )
    assert decision.change == ChangeTarget(
        exercise_id="dumbbell_farmer_carry", from_target=40, meters=45
    )


@pytest.mark.parametrize(("older", "newer"), [(12.0, 24.0), (24.0, 12.0)])
def test_a_carry_whose_load_changed_is_not_two_sessions_of_evidence(
    older: float, newer: float
) -> None:
    """12 kg then 24 kg per hand over 40 m is a load progression; 24 then 12 is a lighter
    session. Neither earns more distance at the newer load yet."""
    decision = _carry_target(
        _session(_CARRY, 40, 40, 40, kg=newer), _session(_CARRY, 40, 40, 40, kg=older)
    )
    assert decision.kind == TargetKind.HOLD and decision.change is None
    assert f"the load changed ({older:g} -> {newer:g} kg)" in decision.reason


def test_a_carry_without_a_logged_load_is_no_evidence() -> None:
    decision = _carry_target(_session(_CARRY, 40, 40, 40, kg=24.0), _session(_CARRY, 40, 40, 40))
    assert decision.change is None
    mixed_loads = [*_session(_CARRY, 40, 40, kg=24.0), *_session(_CARRY, 40, kg=20.0)]
    assert _carry_target(_session(_CARRY, 40, 40, 40, kg=24.0), mixed_loads).change is None


def test_a_loaded_hold_by_time_follows_the_same_rule() -> None:
    hold = rx("dumbbell_farmer_carry", 3, seconds=40, rest=60)
    exercise = _exercise("dumbbell_farmer_carry")
    same = progression.next_target(hold, exercise, [_session(hold, 40, 40, 40, kg=20.0)] * 2)
    assert same.change == ChangeTarget(
        exercise_id="dumbbell_farmer_carry", from_target=40, seconds=45
    )
    heavier = progression.next_target(
        hold, exercise, [_session(hold, 40, 40, 40, kg=22.0), _session(hold, 40, 40, 40, kg=20.0)]
    )
    assert heavier.change is None


def test_a_carry_prescribed_at_another_kg_load_is_another_prescription() -> None:
    """Two variables at once: the plan now prescribes 24 kg, the sessions were at 20 kg."""
    current = rx("dumbbell_farmer_carry", 3, meters=40, rest=60, load={"kind": "kg", "kg": 24.0})
    older = rx("dumbbell_farmer_carry", 3, meters=40, rest=60, load={"kind": "kg", "kg": 20.0})
    decision = progression.next_target(
        current, _exercise("dumbbell_farmer_carry"), [_session(older, 40, 40, 40, kg=20.0)] * 2
    )
    assert decision.change is None


def test_bodyweight_holds_and_intervals_need_no_resistance() -> None:
    plank = rx("plank", 3, seconds=40, rest=45)
    rope = rx("jump_rope_skipping", 4, seconds=60, rest=60)
    assert progression.next_target(
        plank, _exercise("plank"), [_session(plank, 40, 40, 40)] * 2
    ).change == (ChangeTarget(exercise_id="plank", from_target=40, seconds=45))
    rope_change = progression.next_target(
        rope, _exercise("jump_rope_skipping"), [_session(rope, *[60] * 4)] * 2
    ).change
    assert rope_change == ChangeTarget(exercise_id="jump_rope_skipping", from_target=60, seconds=65)


# --- 6. Load progression is scoped to the plan and the prescription ---------------------------


_BENCH = _exercise("barbell_bench_press")


def _bench_outcome(*, plan_id: int | None, sets: int, reps: tuple[int, int]) -> SessionOutcome:
    return SessionOutcome(
        load_kg=60.0,
        planned_load_kg=60.0,
        hit_reps_max=True,
        below_reps_min=False,
        plan_id=plan_id,
        planned_reps_min=reps[0],
        planned_reps_max=reps[1],
        planned_sets=sets,
    )


def _engine(
    outcome: SessionOutcome, *, plan_id: int | None, sets: int, reps: tuple[int, int]
) -> Load:
    history, higher = scope_history(
        ExerciseHistory(history_max_kg=60.0, sessions=[outcome]),
        plan_id=plan_id,
        sets=sets,
        reps_min=reps[0],
        reps_max=reps[1],
    )
    if higher:
        return Load(kind="calibration")
    return next_load(
        _BENCH,
        history,
        {"shoulder": CheckinAnswer.FINE, "elbow_wrist": CheckinAnswer.FINE},
        [],
        2.5,
        flagged_areas=frozenset(),
    ).load


def test_a_success_in_the_same_plan_and_prescription_earns_the_increase() -> None:
    done = _bench_outcome(plan_id=1, sets=4, reps=(4, 6))
    assert _engine(done, plan_id=1, sets=4, reps=(4, 6)) == Load(kind="kg", kg=62.5)


def test_another_plans_success_informs_the_load_but_earns_nothing() -> None:
    done = _bench_outcome(plan_id=1, sets=4, reps=(4, 6))
    # Program B, same 4 x 4-6: start at the known 60 kg, no increase on Program A's streak.
    assert _engine(done, plan_id=2, sets=4, reps=(4, 6)) == Load(kind="kg", kg=60.0)
    # A new plan not saved yet (generation): the same.
    assert _engine(done, plan_id=None, sets=4, reps=(4, 6)) == Load(kind="kg", kg=60.0)


def test_another_set_count_holds_and_a_higher_rep_range_calibrates() -> None:
    done = _bench_outcome(plan_id=1, sets=4, reps=(4, 6))
    assert _engine(done, plan_id=1, sets=3, reps=(4, 6)) == Load(kind="kg", kg=60.0)
    # Program B, 3 x 10: a 6-rep load is not a sound start for 10 reps.
    assert _engine(done, plan_id=2, sets=3, reps=(8, 10)) == Load(kind="calibration")
    # Fewer reps than the load was used for: hold it, no increase.
    assert _engine(done, plan_id=1, sets=4, reps=(3, 5)) == Load(kind="kg", kg=60.0)


def test_a_failure_elsewhere_never_triggers_a_decrease_here() -> None:
    failed = SessionOutcome(
        load_kg=60.0,
        planned_load_kg=60.0,
        hit_reps_max=False,
        below_reps_min=True,
        plan_id=1,
        planned_reps_min=4,
        planned_reps_max=6,
        planned_sets=4,
    )
    history, _ = scope_history(
        ExerciseHistory(history_max_kg=60.0, sessions=[failed, failed]),
        plan_id=2,
        sets=4,
        reps_min=4,
        reps_max=6,
    )
    decision = next_load(_BENCH, history, {}, [], 2.5, flagged_areas=frozenset())
    assert decision.load == Load(kind="kg", kg=60.0)


# --- An unreachable target is a warning, as the plan is now ------------------------------------


def test_the_duration_warning_says_whether_the_main_work_is_all_that_is_left() -> None:
    from fitme.bot.plan_rendering import render_plan_text

    kept = workout(
        "A",
        "Power",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=600, rest=0)),
            role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 10, reps=(5, 5))),
        ],
        [Quality.POWER],
    )
    shortenable = _power_session(6)  # its supporting squat could still lose sets
    fits = workout(
        "C",
        "Short",
        [role(BlockRole.WARM_UP, rx("jump_rope_skipping", 1, seconds=300, rest=0))],
    )
    target = 15
    first = time_budget.duration_warning(kept, _CATALOG, target)
    second = time_budget.duration_warning(shortenable, _CATALOG, target)
    assert first is not None and first.main_work_kept
    assert second is not None and not second.main_work_kept
    assert time_budget.duration_warning(fits, _CATALOG, target) is None
    assert time_budget.duration_warning(kept, _CATALOG, None) is None  # no session length
    # Pure: the plan shown is the plan judged; the trial run changed nothing.
    assert _sets(shortenable, "barbell_back_squat") == 6
    shortenable = shortenable.model_copy(update={"key": "B"})
    text = render_plan_text(
        plan([(0, "A"), (2, "B"), (4, "C")], kept, shortenable, fits),
        catalog=_CATALOG,
        lang="en",
        title="Plan:",
        session_minutes=target,
    )
    assert text.count("⚠") == 2
    assert "so it is kept." in text and "Ask for a shorter version" in text
    assert first.estimated_minutes == session_time.display_minutes(kept, _CATALOG)
    ru = render_plan_text(
        plan([(0, "A")], kept), catalog=_CATALOG, lang="ru", title="План:", session_minutes=target
    )
    assert f"Запрошено: {target} мин. По оценке: около {first.estimated_minutes} мин." in ru
