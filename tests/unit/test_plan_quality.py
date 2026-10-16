"""ADR 0009: the deterministic programming review (`services.plan_quality`) and the three
regression scenarios — boxing support, hypertrophy, hotel — as fixed plans judged with the
real catalog, guards and review (no model is called)."""

from __future__ import annotations

from typing import Any

from fitme.catalog import load_catalog
from fitme.db.records import ExclusionRecord
from fitme.domain.catalog import LOCATION_DEFAULT_EQUIPMENT
from fitme.domain.enums import RED_FLAGS, Equipment, Experience, Location, Quality, Sport
from fitme.domain.models import Block, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import validate_plan
from fitme.services import exclusions
from fitme.services.exercise_search import requested_qualities
from fitme.services.measures import repair_measures
from fitme.services.plan_quality import ReviewContext, review

_CATALOG = load_catalog()
_NO_FLAGS = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]


def rx(exercise_id: str, sets: int = 3, **target: Any) -> Prescription:
    """One prescription; `target` is reps=(min, max) | seconds= | meters=, plus load/rest."""
    load = target.pop("load", {"kind": "calibration"})
    rest = target.pop("rest", 90)
    rir = target.pop("rir", None)
    body: dict[str, Any] = {
        "exercise_id": exercise_id,
        "sets": sets,
        "load": load,
        "rest_seconds": rest,
        "rir": rir,
    }
    if "seconds" in target:
        body |= {"measure": "time", "seconds": target["seconds"]}
    elif "meters" in target:
        body |= {"measure": "distance", "meters": target["meters"]}
    else:
        low, high = target.get("reps", (8, 12))
        body |= {"reps_min": low, "reps_max": high}
    return Prescription.model_validate(body)


def single(*items: Prescription) -> list[Block]:
    return [Block(kind="single", items=[item]) for item in items]


def superset(*items: Prescription) -> Block:
    return Block(kind="superset", items=list(items))


def workout(key: str, title: str, blocks: list[Block], qualities: list[Quality] = []) -> Workout:  # noqa: B006
    return Workout(key=key, title=title, blocks=blocks, qualities=qualities)


def plan(days: list[tuple[int, str]], *workouts: Workout) -> Plan:
    return Plan(
        name="Plan",
        schedule=[ScheduledDay(weekday=day, workout_key=key) for day, key in days],
        workouts=list(workouts),
    )


def rules(findings: list[Any]) -> list[str]:
    return [finding.rule for finding in findings]


# --- Single rules ---------------------------------------------------------------------------


def test_heavy_lifting_needs_a_warm_up_first() -> None:
    cold = plan([(0, "A")], workout("A", "Lower", single(rx("barbell_deadlift", reps=(3, 5)))))
    assert "quality.warm_up" in rules(review(cold, ReviewContext(_CATALOG)))
    warm = plan(
        [(0, "A")],
        workout(
            "A",
            "Lower",
            single(rx("mobility_cat_cow", 1), rx("barbell_deadlift", reps=(3, 5))),
        ),
    )
    assert "quality.warm_up" not in rules(review(warm, ReviewContext(_CATALOG)))


def test_power_work_after_heavy_strength_work_is_reported() -> None:
    late = plan(
        [(0, "A")],
        workout(
            "A",
            "Power",
            single(
                rx("mobility_cat_cow", 1),
                rx("barbell_deadlift", reps=(3, 5)),
                rx("medicine_ball_overhead_slam", reps=(5, 5)),
            ),
        ),
    )
    assert "quality.order" in rules(review(late, ReviewContext(_CATALOG)))


def test_supersets_need_a_reason_matching_sets_and_short_transitions() -> None:
    bad = plan(
        [(0, "A")],
        workout(
            "A",
            "Upper",
            [
                *single(rx("mobility_cat_cow", 1)),
                superset(
                    rx("barbell_bench_press", 4, reps=(4, 6), rest=120),
                    rx("barbell_bent_over_row", 3, reps=(4, 6), rest=120),
                ),
            ],
        ),
    )
    details = [
        f.detail for f in review(bad, ReviewContext(_CATALOG)) if f.rule == "quality.superset"
    ]
    assert any("two heavy lifts" in d for d in details)
    assert any("same sets" in d for d in details)
    assert any("transition" in d for d in details)
    good = plan(
        [(0, "A")],
        workout(
            "A",
            "Upper",
            [
                *single(rx("mobility_cat_cow", 1)),
                superset(
                    rx("dumbbell_bench_press", 3, reps=(8, 10), rest=20),
                    rx("dumbbell_row", 3, reps=(8, 10), rest=90),
                ),
            ],
        ),
    )
    assert "quality.superset" not in rules(review(good, ReviewContext(_CATALOG)))


def test_a_stated_goal_needs_an_exercise_that_directly_trains_it() -> None:
    """ "Foot speed" with only presses and pulls: the observed mismatch."""
    titled = plan(
        [(0, "A")],
        workout(
            "A",
            "Press + foot speed",
            single(rx("dumbbell_bench_press"), rx("cable_neutral_grip_pulldown")),
            [Quality.STRENGTH, Quality.AGILITY],
        ),
    )
    findings = review(titled, ReviewContext(_CATALOG))
    assert [f.detail for f in findings if f.rule == "quality.goal_coverage"] == [
        "workout A is for agility, but none of its exercises directly trains agility"
    ]


def test_a_request_naming_goals_needs_every_workout_to_state_them() -> None:
    bare = plan([(0, "A")], workout("A", "Full body", single(rx("pushup"))))
    asked = ReviewContext(_CATALOG, requested=[Quality.STRENGTH], require_declared=True)
    assert "quality.declared" in rules(review(bare, asked))
    assert "quality.declared" not in rules(review(bare, ReviewContext(_CATALOG)))


def test_heavy_lower_body_days_back_to_back_are_reported() -> None:
    lower = workout(
        "A", "Lower", single(rx("mobility_cat_cow", 1), rx("barbell_deadlift", reps=(3, 5)))
    )
    assert "quality.consecutive_heavy" in rules(
        review(plan([(0, "A"), (1, "A")], lower), ReviewContext(_CATALOG))
    )
    assert "quality.consecutive_heavy" in rules(  # Sunday then Monday
        review(plan([(2, "A"), (6, "A"), (0, "A")], lower), ReviewContext(_CATALOG))
    )
    assert "quality.consecutive_heavy" not in rules(
        review(plan([(0, "A"), (3, "A")], lower), ReviewContext(_CATALOG))
    )


def test_a_sport_lowers_the_weekly_shoulder_limit() -> None:
    upper = workout(
        "A",
        "Upper",
        single(
            rx("dumbbell_bench_press", 4),
            rx("dumbbell_seated_shoulder_press", 4),
            rx("dumbbell_row", 4),
        ),
    )
    week = plan([(0, "A"), (2, "A")], upper)  # 24 shoulder sets a week
    assert "quality.shoulder_volume" not in rules(review(week, ReviewContext(_CATALOG)))
    boxing = ReviewContext(
        _CATALOG, sport=Sport.BOXING_MARTIAL_ARTS, sport_sessions_per_week=5
    )  # 36 - 15 = 21
    assert "quality.shoulder_volume" in rules(review(week, boxing))


# --- Scenario A: strength and conditioning supporting boxing --------------------------------
# 3 sessions a week, intermediate, full gym, avoid squats and lunges, boxing 3x a week.

_BOXING_REQUEST = (
    "Поддерживающая тренировка для бокса: сила удара, выносливость, скорость перемещения"
)
_BOXING_EXCLUDED = exclusions.resolve(
    [
        ExclusionRecord(id=1, user_id=1, kind="pattern", value="squat", created_at=""),
    ],
    _CATALOG,
)


def _boxing_context() -> ReviewContext:
    return ReviewContext(
        _CATALOG,
        requested=requested_qualities(_BOXING_REQUEST, sport=Sport.BOXING_MARTIAL_ARTS),
        sport=Sport.BOXING_MARTIAL_ARTS,
        sport_sessions_per_week=3,
        require_declared=True,
    )


def _gym_ctx(excluded: frozenset[str] = frozenset()) -> GuardContext:
    return GuardContext(
        catalog=_CATALOG,
        flags=_NO_FLAGS,
        equipment=LOCATION_DEFAULT_EQUIPMENT[Location.PUBLIC_GYM],
        location=Location.PUBLIC_GYM,
        sessions_per_week=3,
        experience=Experience.Y2_TO_5Y,  # "intermediate": advanced moves allowed
        excluded_exercise_ids=excluded,
    )


def _observed_boxing_plan() -> Plan:
    """The generated plan from the bug report, as far as the catalog reproduces it."""
    return plan(
        [(0, "A"), (2, "B"), (4, "C")],
        workout(
            "A",
            "Legs + explosive",
            [
                *single(
                    rx("mobility_cat_cow", 1),
                    rx("battle_rope_alternating_waves", 4, reps=(1, 1)),
                    rx("barbell_back_squat", 4, reps=(4, 6)),
                ),
                superset(rx("dumbbell_bench_press", 4), rx("cable_neutral_grip_pulldown", 4)),
                *single(rx("cable_wood_chop")),
            ],
        ),
        workout(
            "B",
            "Pull + shoulders",
            [
                *single(
                    rx("ankle_circles", 1),
                    rx("kettlebell_swing", 5, reps=(12, 15)),
                    rx("barbell_romanian_deadlift", 4, reps=(6, 8)),
                ),
                superset(rx("barbell_bent_over_row", 4), rx("dumbbell_seated_shoulder_press", 4)),
                superset(rx("dumbbell_lateral_raise", 3), rx("plank", 3, reps=(1, 1))),
            ],
        ),
        workout(
            "C",
            "Press + foot speed",
            [
                *single(
                    rx("mobility_cat_cow", 1),
                    rx("battle_rope_alternating_waves", 5, reps=(1, 1)),
                    rx("barbell_bench_press", 4, reps=(4, 6)),
                ),
                superset(rx("dumbbell_goblet_squat"), rx("cable_neutral_grip_pulldown")),
                superset(rx("cable_wood_chop"), rx("dumbbell_farmer_carry", reps=(1, 1))),
            ],
        ),
    )


def _good_boxing_plan() -> Plan:
    def warm_up() -> list[Block]:
        return single(
            rx("jump_rope_skipping", 2, seconds=120, rest=30), rx("mobility_thoracic_rotation", 1)
        )

    return plan(
        [(0, "A"), (2, "B"), (4, "C")],
        workout(
            "A",
            "Power + strength",
            [
                *warm_up(),
                *single(
                    rx("medicine_ball_three_point_chest_throw", 4, reps=(5, 5)),
                    rx("trap_bar_deadlift", 4, reps=(4, 6), rir=2),
                    rx("dumbbell_row", 3, reps=(8, 10)),
                ),
                superset(
                    rx("band_pallof_press", 3, reps=(10, 12), rest=20),
                    rx("side_plank", 3, seconds=30, rest=60),
                ),
            ],
            [Quality.POWER, Quality.STRENGTH, Quality.CORE_STABILITY],
        ),
        workout(
            "B",
            "Footwork + conditioning",
            [
                *warm_up(),
                *single(
                    rx("bodyweight_skater_hop", 4, reps=(6, 8)),
                    rx("bodyweight_shadow_boxing", 4, seconds=120, rest=60),
                    rx("dumbbell_farmer_carry", 3, meters=30, rest=60),
                    rx("plank", 3, seconds=40, rest=45),
                ),
            ],
            [Quality.AGILITY, Quality.CONDITIONING, Quality.CORE_STABILITY],
        ),
        workout(
            "C",
            "Rotation + strength",
            [
                *warm_up(),
                *single(
                    rx("cable_wood_chop", 4, reps=(6, 8)),
                    rx("medicine_ball_overhead_slam", 3, reps=(6, 8)),
                    rx("barbell_hip_thrust", 4, reps=(6, 8)),
                    rx("pull_up", 3, reps=(5, 8), load={"kind": "bodyweight"}),
                    rx("dumbbell_bench_press", 3, reps=(8, 10)),
                ),
            ],
            [Quality.ROTATIONAL_POWER, Quality.POWER, Quality.STRENGTH],
        ),
    )


def test_scenario_a_the_observed_boxing_plan_is_caught() -> None:
    observed = _observed_boxing_plan()
    measure_fixes = repair_measures(observed, _CATALOG)
    # "Battle ropes 4 × 1", "Plank 3 × 1", "Farmer carry 3 × 1" are moved to time/distance.
    assert sum(f.rule == "plan.measure_fit" for f in measure_fixes) == 4
    # Squat variations violate the hard "no squats and lunges" exclusion.
    excluded = [
        v for v in validate_plan(observed, _gym_ctx(_BOXING_EXCLUDED)) if v.rule == "plan.excluded"
    ]
    assert {v.detail.split(":")[0] for v in excluded} == {
        "barbell_back_squat",
        "dumbbell_goblet_squat",
    }
    found = rules(review(observed, _boxing_context()))
    assert "quality.request_coverage" in found  # no footwork drill anywhere
    assert "quality.declared" in found  # titles say "foot speed", nothing states it
    assert "quality.shoulder_volume" in found  # presses + raises + ropes on top of boxing
    assert "quality.superset" in found  # a 90 s "transition" inside each superset


def test_scenario_a_a_coherent_boxing_plan_passes() -> None:
    good = _good_boxing_plan()
    assert repair_measures(good, _CATALOG) == []
    failures = [v for v in validate_plan(good, _gym_ctx(_BOXING_EXCLUDED)) if not v.ok]
    assert failures == []
    assert review(good, _boxing_context()) == []


# --- Scenario B: hypertrophy, 4 sessions, full gym -------------------------------------------


def test_scenario_b_an_upper_lower_hypertrophy_split_passes() -> None:
    def upper(key: str) -> Workout:
        return workout(
            key,
            "Upper",
            [
                *single(rx("mobility_thoracic_rotation", 1), rx("dumbbell_bench_press", 3)),
                superset(rx("dumbbell_row", 3, rest=20), rx("cable_neutral_grip_pulldown", 3)),
                superset(rx("dumbbell_lateral_raise", 3, rest=20), rx("dumbbell_incline_curl", 3)),
            ],
            [Quality.HYPERTROPHY],
        )

    def lower(key: str) -> Workout:
        return workout(
            key,
            "Lower",
            single(
                rx("mobility_hip_flexor_stretch", 1, seconds=40),
                rx("machine_leg_press", 4, reps=(8, 12)),
                rx("barbell_romanian_deadlift", 3, reps=(8, 10)),
                rx("machine_lying_leg_curl", 3, reps=(10, 15)),
                rx("machine_seated_calf_raise", 3, reps=(12, 15)),
            ),
            [Quality.HYPERTROPHY],
        )

    split = plan(
        [(0, "A"), (1, "B"), (3, "C"), (4, "D")], upper("A"), lower("B"), upper("C"), lower("D")
    )
    ctx = ReviewContext(
        _CATALOG, requested=requested_qualities("muscle growth"), require_declared=True
    )
    assert review(split, ctx) == []
    assert not [v for v in validate_plan(split, _gym_ctx()) if not v.ok]
    assert all(
        item.reps_min is not None
        for w in split.workouts
        for b in w.blocks
        for item in b.items
        if item.exercise_id != "mobility_hip_flexor_stretch"
    )


# --- Scenario C: hotel, bodyweight and bands -------------------------------------------------


def test_scenario_c_a_hotel_plan_uses_no_barbell_or_machine_and_times_its_holds() -> None:
    hotel = GuardContext(
        catalog=_CATALOG,
        flags=_NO_FLAGS,
        equipment=frozenset({Equipment.RESISTANCE_BANDS}),
        location=Location.HOME_EQUIPMENT,
        sessions_per_week=3,
        experience=Experience.M6_TO_2Y,
    )
    full_body = workout(
        "A",
        "Full body",
        [
            *single(
                rx("bodyweight_jumping_jacks", 2, seconds=60, rest=30),
                rx("bodyweight_split_squat", 3, reps=(10, 12), load={"kind": "bodyweight"}),
                rx("incline_push_up", 3, reps=(10, 15), load={"kind": "bodyweight"}),
            ),
            superset(
                rx("band_pull_apart", 3, reps=(15, 20), rest=20),
                rx("bodyweight_glute_bridge", 3, reps=(15, 20), load={"kind": "bodyweight"}),
            ),
            *single(rx("plank", 3, seconds=40, load={"kind": "bodyweight"})),
        ],
        [Quality.STRENGTH, Quality.MUSCULAR_ENDURANCE],
    )
    week = plan([(0, "A"), (2, "A"), (4, "A")], full_body)
    assert not [v for v in validate_plan(week, hotel) if not v.ok]
    assert review(week, ReviewContext(_CATALOG)) == []
    with_barbell = plan(
        [(0, "A")], workout("A", "Full body", single(rx("barbell_bench_press", reps=(8, 10))))
    )
    assert any(
        v.rule == "plan.equipment_fit" and not v.ok for v in validate_plan(with_barbell, hotel)
    )
