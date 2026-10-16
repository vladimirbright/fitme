"""ADR 0011: stimulus-aware prescriptions, block roles, quality coverage (direct, supporting,
none), weekly distribution and superset rest semantics — fixed plans through the real catalog,
repair, review and rendering (no model is called).

Scenarios from the bug report: A (a farmer carry without load history), B (a medicine-ball
chest pass with a generic `rir`), C (a general boxing warm-up vs. a single-punch drill),
D (a footwork session), E (a two-day strength-and-conditioning plan), F (superset rests);
then plans that must keep passing (general strength, home, equipment-limited, conditioning)."""

from __future__ import annotations

import dataclasses

from test_plan_quality import plan, rules, rx, single, superset, workout

from fitme.bot.plan_rendering import render_plan_text
from fitme.catalog import load_catalog
from fitme.domain.catalog import Exercise, intensity_mode
from fitme.domain.enums import BlockRole, IntensityMode, Measure, Quality, Sport
from fitme.domain.models import Block, Load, Prescription, Workout
from fitme.llm.context import UserContext
from fitme.services import exercise_search, prescribing, programming
from fitme.services.measures import repair_measures
from fitme.services.plan_quality import (
    ReviewContext,
    block_roles,
    coverage,
    estimated_minutes,
    review,
)

_CATALOG = load_catalog()


def _exercise(exercise_id: str) -> Exercise:
    exercise = _CATALOG.by_id(exercise_id)
    assert exercise is not None, exercise_id
    return exercise


def role(kind: BlockRole, *items: Prescription) -> Block:
    return Block(kind="superset" if len(items) > 1 else "single", role=kind, items=list(items))


# --- Intensity by stimulus -------------------------------------------------------------------


def test_intensity_mode_follows_what_the_exercise_mainly_trains_and_how_it_is_measured() -> None:
    def mode(exercise_id: str, measure: Measure | None = None) -> IntensityMode:
        exercise = _exercise(exercise_id)
        return intensity_mode(exercise, measure or exercise.measures[0])

    assert mode("barbell_back_squat") == IntensityMode.EFFORT
    assert mode("dumbbell_lateral_raise") == IntensityMode.EFFORT
    assert mode("pushup") == IntensityMode.EFFORT  # bodyweight reps still have reserve
    assert mode("medicine_ball_chest_pass") == IntensityMode.SPEED
    assert mode("kettlebell_swing") == IntensityMode.SPEED
    assert mode("bodyweight_skater_hop") == IntensityMode.SPEED
    assert mode("dumbbell_farmer_carry") == IntensityMode.CARRY
    assert mode("battle_rope_alternating_waves") == IntensityMode.PACE
    assert mode("machine_rower") == IntensityMode.PACE
    assert mode("plank") == IntensityMode.NONE


def test_scenario_a_a_farmer_carry_without_history_gets_carry_calibration_not_2_kg() -> None:
    carry = rx("dumbbell_farmer_carry", 3, meters=40, rest=60, rir=3)
    week = plan([(1, "A")], workout("A", "Carry", single(carry)))

    fired = prescribing.repair_intensity(week, _CATALOG)

    assert carry.rir is None  # reps in reserve mean nothing for a carry
    assert [v.rule for v in fired] == [prescribing.RIR_DROPPED_RULE]
    assert carry.measure == Measure.DISTANCE and carry.meters == 40
    for lang, fragment in (
        ("en", "3 × 40 m @ calibration: a weight you can carry or hold for the whole set"),
        ("ru", "3 × 40 м @ калибровка: вес, который удержите весь подход"),
    ):
        text = render_plan_text(week, catalog=_CATALOG, lang=lang, title="Draft:")
        assert fragment in text
        assert "2 kg" not in text and "2 кг" not in text
    # The catalog start (2 kg each) still exists — it's just never shown as a weight.
    assert _exercise("dumbbell_farmer_carry").start == Load(kind="kg", kg=2.0)


def test_scenario_b_a_chest_pass_is_regulated_by_speed_not_reps_in_reserve() -> None:
    chest_pass = rx("medicine_ball_chest_pass", 4, reps=(5, 6), rir=3, rest=90)
    squat = rx("barbell_back_squat", 4, reps=(4, 6))
    week = plan([(1, "A")], workout("A", "Power", single(chest_pass, squat)))

    prescribing.repair_intensity(week, _CATALOG)

    assert chest_pass.rir is None
    assert squat.rir == prescribing.DEFAULT_CALIBRATION_RIR  # strength work keeps it
    text = render_plan_text(week, catalog=_CATALOG, lang="en", title="Draft:")
    line = next(line for line in text.splitlines() if "Medicine ball chest pass" in line)
    assert "4 × 5–6 @ calibration: a load you can move fast with clean technique" in line
    assert "reps in reserve" not in line
    # A plan saved before ADR 0011 with an rir on the throw doesn't show it either.
    saved = rx("medicine_ball_chest_pass", 4, reps=(5, 6), rir=3, load={"kind": "kg", "kg": 4})
    label = prescribing.prescription_load_label(saved, _exercise("medicine_ball_chest_pass"), "en")
    assert label == "4 kg, every rep fast; end the set when speed or technique drops"


def test_power_work_in_long_sets_or_with_almost_no_rest_is_reported() -> None:
    def power_day(item: Prescription) -> list[str]:
        week = plan(
            [(0, "A")],
            workout(
                "A",
                "Power",
                single(rx("jump_rope_skipping", 2, seconds=120, rest=30), item),
                [Quality.POWER],
            ),
        )
        return [f.detail for f in review(week, ReviewContext(_CATALOG)) if "prescription" in f.rule]

    assert power_day(rx("medicine_ball_chest_pass", 4, reps=(5, 6), rest=90)) == []
    long_sets = power_day(rx("medicine_ball_chest_pass", 3, reps=(12, 15), rest=90))
    assert len(long_sets) == 1 and "fewer reps" in long_sets[0]
    no_rest = power_day(rx("medicine_ball_chest_pass", 4, reps=(5, 5), rest=20))
    assert len(no_rest) == 1 and "at least 45 s" in no_rest[0]
    # A slam is power *and* conditioning: a longer set is a legitimate choice.
    assert power_day(rx("medicine_ball_overhead_slam", 3, reps=(10, 12), rest=60)) == []


# --- Scenario C: a general warm-up, not a single-punch drill ---------------------------------


def test_scenario_c_a_technique_drill_is_not_a_warm_up_and_retrieval_knows_it() -> None:
    hook = _exercise("bodyweight_shadow_boxing_hook")
    shadow = _exercise("bodyweight_shadow_boxing")
    assert hook.specific and not shadow.specific
    assert exercise_search.is_general_warm_up(shadow)
    assert exercise_search.is_general_warm_up(_exercise("jump_rope_skipping"))
    assert not exercise_search.is_general_warm_up(hook)

    def session(warm_up: str) -> Workout:
        return workout(
            "A",
            "Power and strength",
            [
                role(BlockRole.WARM_UP, rx(warm_up, 2, seconds=120, rest=30)),
                role(BlockRole.POWER, rx("medicine_ball_chest_pass", 4, reps=(5, 5))),
                role(BlockRole.STRENGTH, rx("barbell_bench_press", 4, reps=(4, 6))),
            ],
            [Quality.POWER, Quality.STRENGTH],
        )

    drill = review(plan([(0, "A")], session(hook.id)), ReviewContext(_CATALOG))
    # The drill isn't a warm-up, so the heavy bench press has none either.
    assert sorted(f.rule for f in drill) == ["quality.role", "quality.warm_up"]
    assert any("narrow technique drill" in f.detail for f in drill)
    assert review(plan([(0, "A")], session(shadow.id)), ReviewContext(_CATALOG)) == []
    # Plans saved before roles: the leading warm-up-like blocks are inferred as the warm-up.
    old = workout(
        "A",
        "Boxing",
        single(
            rx("jump_rope_skipping", 2, seconds=120),
            rx(hook.id, 2, seconds=120),
            rx("medicine_ball_chest_pass", 4, reps=(5, 5)),
        ),
    )
    assert block_roles(old, _CATALOG) == [BlockRole.WARM_UP, BlockRole.WARM_UP, BlockRole.POWER]
    assert "quality.role" in rules(review(plan([(0, "A")], old), ReviewContext(_CATALOG)))


# --- Scenario D: a footwork session ----------------------------------------------------------


def test_direct_supporting_and_unrelated_coverage() -> None:
    squat, quick_feet = _exercise("barbell_back_squat"), _exercise("bodyweight_quick_feet")
    throw = _exercise("medicine_ball_chest_pass")
    assert coverage(Quality.AGILITY, [quick_feet]) == (1, 0)  # direct
    assert coverage(Quality.POWER, [squat]) == (0, 1)  # a strength base supports power
    assert coverage(Quality.AGILITY, [squat]) == (0, 0)  # unrelated
    assert coverage(Quality.AGILITY, [throw]) == (0, 1)  # power supports quick feet


def test_scenario_d_a_footwork_session_needs_footwork_not_one_drill_among_lifts() -> None:
    weak = workout(
        "A",
        "Agility and footwork",
        single(
            rx("barbell_bench_press", 3, reps=(6, 8)),
            rx("cable_neutral_grip_pulldown", 3),
            rx("bodyweight_quick_feet", 4, seconds=20, rest=60),
            rx("barbell_romanian_deadlift", 3, reps=(8, 10)),
        ),
        [Quality.AGILITY],
    )
    findings = review(plan([(0, "A")], weak), ReviewContext(_CATALOG))
    assert "quality.weak_coverage" in rules(findings)
    footwork = workout(
        "A",
        "Agility and footwork",
        [
            role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
            role(BlockRole.POWER, rx("stance_footwork_drill", 4, seconds=60, rest=60)),
            role(BlockRole.POWER, rx("bodyweight_lateral_shuffle", 4, seconds=20, rest=60)),
            role(BlockRole.POWER, rx("shuttle_run", 4, seconds=15, rest=90)),
            role(BlockRole.STRENGTH, rx("trap_bar_deadlift", 3, reps=(5, 5))),
        ],
        [Quality.AGILITY, Quality.STRENGTH],
    )
    assert review(plan([(0, "A")], footwork), ReviewContext(_CATALOG)) == []


def test_a_quality_only_the_warm_up_touches_is_not_covered() -> None:
    week = plan(
        [(0, "A")],
        workout(
            "A",
            "Strength and footwork",
            [
                role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
                role(BlockRole.STRENGTH, rx("barbell_back_squat", 4, reps=(4, 6))),
                role(BlockRole.STRENGTH, rx("dumbbell_row", 3, reps=(8, 10))),
            ],
            [Quality.STRENGTH, Quality.AGILITY],
        ),
    )
    details = [f.detail for f in review(week, ReviewContext(_CATALOG))]
    assert any("is for agility" in d and "only its warm-up" in d for d in details)


# --- Roles and order -------------------------------------------------------------------------


def test_roles_must_fit_their_exercises_and_follow_the_session_order() -> None:
    def findings(*blocks: Block) -> list[str]:
        week = plan([(0, "A")], workout("A", "Session", list(blocks)))
        return [f"{f.rule}: {f.detail}" for f in review(week, ReviewContext(_CATALOG))]

    warm = role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30))
    mismatch = findings(warm, role(BlockRole.POWER, rx("dumbbell_lateral_raise", 3)))
    assert any(f.startswith("quality.role") and "dumbbell_lateral_raise" in f for f in mismatch)
    after_conditioning = findings(
        warm,
        role(BlockRole.CONDITIONING, rx("battle_rope_alternating_waves", 4, seconds=30)),
        role(BlockRole.POWER, rx("medicine_ball_chest_pass", 4, reps=(5, 5))),
    )
    assert any(f.startswith("quality.order") and "comes after" in f for f in after_conditioning)
    late_warm_up = findings(
        role(BlockRole.STRENGTH, rx("dumbbell_row", 3, reps=(8, 10))),
        role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
    )
    assert any(f.startswith("quality.order") and "after the main work" in f for f in late_warm_up)
    # A carry is fine as conditioning; a heavy squat isn't a warm-up.
    carry = role(BlockRole.CONDITIONING, rx("dumbbell_farmer_carry", 3, meters=40, rest=60))
    assert not [f for f in findings(warm, carry) if f.startswith("quality.role")]
    heavy_warm_up = findings(role(BlockRole.WARM_UP, rx("barbell_back_squat", 3, reps=(3, 5))))
    assert any(f.startswith("quality.role") and "heavy" in f for f in heavy_warm_up)


# --- Scenario E: a two-day strength-and-conditioning plan ------------------------------------


def _boxing_two_days() -> ReviewContext:
    return ReviewContext(
        _CATALOG,
        requested=[Quality.POWER, Quality.AGILITY, Quality.STRENGTH, Quality.CONDITIONING],
        sport=Sport.BOXING_MARTIAL_ARTS,
        sport_sessions_per_week=3,
        session_minutes=60,
    )


def test_scenario_e_the_observed_two_day_plan_is_a_split_with_stacked_lower_body_work() -> None:
    observed = plan(
        [(1, "A"), (4, "B")],
        workout(
            "A",
            "Power, upper body, conditioning",
            single(
                rx("jump_rope_skipping", 2, seconds=120, rest=30),
                rx("bodyweight_shadow_boxing_hook", 2, seconds=120, rest=30),
                rx("medicine_ball_chest_pass", 4, reps=(5, 6)),
                rx("cable_wood_chop", 3, reps=(8, 10)),
                rx("barbell_bench_press", 4, reps=(5, 6)),
            )
            + [
                superset(
                    rx("dumbbell_row", 3, reps=(8, 10), rest=20),
                    rx("plank", 3, seconds=40, rest=75),
                ),
                *single(rx("battle_rope_alternating_waves", 4, seconds=30, rest=60)),
            ],
            [Quality.POWER, Quality.STRENGTH, Quality.CONDITIONING],
        ),
        workout(
            "B",
            "Leg strength, foot speed, endurance",
            single(
                rx("jump_rope_skipping", 2, seconds=120, rest=30),
                rx("bodyweight_shadow_boxing_hook", 2, seconds=120, rest=30),
                rx("bodyweight_quick_feet", 4, seconds=20, rest=40),
                rx("kettlebell_swing", 4, reps=(12, 15), rest=60),
                rx("barbell_back_squat", 4, reps=(4, 6), rest=150),
                rx("barbell_romanian_deadlift", 3, reps=(6, 8), rest=120),
                rx("cable_neutral_grip_pulldown", 3, reps=(8, 10)),
                rx("dumbbell_farmer_carry", 3, meters=40, rest=60),
            ),
            [Quality.STRENGTH, Quality.AGILITY, Quality.CONDITIONING],
        ),
    )
    found = review(observed, _boxing_two_days())
    details = [f.detail for f in found if f.rule == "quality.split"]
    assert any(d.startswith("lower body is trained only in workout B") for d in details)
    assert any(d.startswith("pressing is trained only in workout A") for d in details)
    assert sum(f.rule == "quality.role" for f in found) == 2  # the hook drill as warm-up
    # With a split the user asked for, the same plan is no longer reported as one.
    asked = dataclasses.replace(_boxing_two_days(), split_wanted=True)
    assert "quality.split" not in rules(review(observed, asked))


def _coherent_two_days() -> list[Workout]:
    warm_up = role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30))
    return [
        workout(
            "A",
            "Power and lower-body strength",
            [
                warm_up,
                role(BlockRole.POWER, rx("medicine_ball_rotational_side_throw", 4, reps=(4, 5))),
                role(BlockRole.POWER, rx("bodyweight_skater_hop", 3, reps=(5, 6))),
                role(BlockRole.STRENGTH, rx("trap_bar_deadlift", 4, reps=(4, 6), rest=150)),
                role(
                    BlockRole.ACCESSORY,
                    rx("dumbbell_bench_press", 3, reps=(8, 10), rest=20),
                    rx("dumbbell_row", 3, reps=(8, 10), rest=90),
                ),
                role(BlockRole.CORE, rx("side_plank", 3, seconds=30, rest=45)),
            ],
            [Quality.POWER, Quality.STRENGTH, Quality.CORE_STABILITY],
        ),
        workout(
            "B",
            "Footwork and upper-body strength",
            [
                warm_up,
                role(BlockRole.POWER, rx("stance_footwork_drill", 4, seconds=60, rest=60)),
                role(BlockRole.POWER, rx("bodyweight_lateral_shuffle", 4, seconds=20, rest=60)),
                role(BlockRole.STRENGTH, rx("barbell_bench_press", 4, reps=(4, 6), rest=150)),
                role(
                    BlockRole.STRENGTH, rx("pull_up", 3, reps=(5, 8), load={"kind": "bodyweight"})
                ),
                role(BlockRole.ACCESSORY, rx("dumbbell_bulgarian_split_squat", 3, reps=(8, 10))),
                role(BlockRole.CONDITIONING, rx("dumbbell_farmer_carry", 3, meters=40, rest=60)),
            ],
            [Quality.AGILITY, Quality.STRENGTH, Quality.CONDITIONING],
        ),
    ]


def test_scenario_e_a_two_day_full_body_plan_with_different_emphases_passes() -> None:
    week = plan([(1, "A"), (4, "B")], *_coherent_two_days())
    assert repair_measures(week, _CATALOG) == []
    assert review(week, _boxing_two_days()) == []
    assert all(estimated_minutes(w) <= 60 * 1.25 for w in week.workouts)


def test_a_session_far_longer_than_the_session_length_is_reported() -> None:
    long = workout(
        "A",
        "Everything",
        [single(rx("jump_rope_skipping", 2, seconds=120, rest=30))[0]]
        + single(*[rx("dumbbell_row", 5, reps=(10, 12), rest=180) for _ in range(6)]),
    )
    ctx = ReviewContext(_CATALOG, session_minutes=45)
    assert "quality.duration" in rules(review(plan([(0, "A")], long), ctx))


# --- Scenario F: superset rests --------------------------------------------------------------


def test_scenario_f_a_superset_says_its_round_rest_once_and_each_transition() -> None:
    week = plan(
        [(0, "A")],
        workout(
            "A",
            "Upper",
            [
                superset(
                    rx("dumbbell_row", 3, reps=(8, 10), rest=20),
                    rx("plank", 3, seconds=40, rest=75, load={"kind": "bodyweight"}),
                )
            ],
        ),
    )
    text = render_plan_text(week, catalog=_CATALOG, lang="en", title="Draft:")
    assert "Superset, in this order, 3 rounds; rest 1 min 15 s after each round:" in text
    assert text.count("1 min 15 s") == 1  # the round rest, once
    row = next(line for line in text.splitlines() if "Dumbbell row" in line)
    plank = next(line for line in text.splitlines() if "Plank" in line)
    assert row.endswith("then 20 s to the next exercise")
    assert "rest" not in plank
    assert text.index("Dumbbell row") < text.index("Plank")  # execution order
    ru = render_plan_text(week, catalog=_CATALOG, lang="ru", title="Черновик:")
    assert ru.count("1 мин 15 с") == 1 and "затем 20 с — и к следующему упражнению" in ru


# --- Plans that must keep passing ------------------------------------------------------------


def test_general_strength_full_body_three_days_passes() -> None:
    def day(key: str, main: str, pull: str) -> Workout:
        return workout(
            key,
            "Full body",
            [
                role(BlockRole.WARM_UP, rx("mobility_cat_cow", 1, seconds=60, rest=0)),
                role(BlockRole.STRENGTH, rx(main, 4, reps=(4, 6), rest=150)),
                role(BlockRole.STRENGTH, rx("barbell_bench_press", 3, reps=(5, 8), rest=120)),
                role(BlockRole.ACCESSORY, rx(pull, 3, reps=(8, 10))),
                role(BlockRole.CORE, rx("plank", 3, seconds=40, rest=45)),
            ],
            [Quality.STRENGTH],
        )

    week = plan(
        [(0, "A"), (2, "B"), (4, "A")],
        day("A", "barbell_back_squat", "dumbbell_row"),
        day("B", "trap_bar_deadlift", "cable_neutral_grip_pulldown"),
    )
    assert review(week, ReviewContext(_CATALOG, requested=[Quality.STRENGTH])) == []


def test_general_conditioning_and_an_equipment_free_home_plan_pass() -> None:
    circuit = workout(
        "A",
        "Conditioning",
        [
            role(BlockRole.WARM_UP, rx("march_in_place", 1, seconds=180, rest=0)),
            role(
                BlockRole.CONDITIONING,
                rx("bodyweight_jumping_jacks", 4, seconds=40, rest=15),
                rx("mountain_climbers", 4, seconds=30, rest=15),
                rx("bodyweight_burpee", 4, seconds=30, rest=90),
            ),
            role(BlockRole.STRENGTH, rx("pushup", 3, reps=(8, 12), load={"kind": "bodyweight"})),
            role(
                BlockRole.STRENGTH,
                rx("bodyweight_split_squat", 3, reps=(10, 12), load={"kind": "bodyweight"}),
            ),
            role(BlockRole.CORE, rx("plank", 3, seconds=40, rest=45)),
        ],
        [Quality.CONDITIONING, Quality.MUSCULAR_ENDURANCE],
    )
    week = plan([(0, "A"), (3, "A")], circuit)
    ctx = ReviewContext(
        _CATALOG, requested=[Quality.CONDITIONING], session_minutes=40, require_declared=True
    )
    assert review(week, ctx) == []
    assert all(
        not _exercise(item.exercise_id).equipment for b in circuit.blocks for item in b.items
    )


# --- The weekly frame ------------------------------------------------------------------------


def _context(**fields: object) -> UserContext:
    base: dict[str, object] = {
        "user_id": 1,
        "language": "en",
        "age_bucket": "30_39",
        "weight_bucket": "80_89",
        "experience": "2y_5y",
        "barbell_experience": "yes",
        "preferences": [],
        "focus": "strength",
        "location": "public_gym",
        "equipment": [],
        "sessions_per_week": 2,
        "session_minutes": 60,
        "flags": [],
        "allowed_exercise_ids": [],
        "history": [],
    }
    return UserContext.model_validate({**base, **fields})


def test_the_programming_brief_sets_the_frame_from_the_profile() -> None:
    boxer = _context(sport="boxing_martial_arts", sport_sessions_per_week=4)
    qualities = [Quality.POWER, Quality.AGILITY, Quality.CONDITIONING]
    brief = programming.brief(boxer, qualities, split_wanted=False)
    assert brief["structure"] == "full_body_varied_emphasis"
    assert brief["main_qualities"] == ["power", "agility"]
    assert brief["secondary_qualities"] == ["conditioning"]
    assert brief["fatigue_budget"] == "low"
    assert brief["working_blocks_per_session"] == [4, 6]
    assert programming.working_blocks(30) == (2, 3)
    split = _context(preferences=["split"], sessions_per_week=4)
    assert programming.split_wanted(split, None)
    assert programming.split_wanted(_context(), "сплит верх/низ")
    assert programming.brief(split, [], split_wanted=True)["structure"] == "split_as_requested"
    assert programming.brief(_context(sessions_per_week=5), [], split_wanted=False)[
        "structure"
    ] == ("full_body_or_upper_lower")


def test_the_speed_cue_fits_the_block_and_the_measure() -> None:
    shadow = _exercise("bodyweight_shadow_boxing")
    rounds = rx(shadow.id, 2, seconds=120, rest=60, load={"kind": "bodyweight"})
    hops = rx("bodyweight_skater_hop", 3, reps=(5, 6), load={"kind": "bodyweight"})

    def label(item: Prescription, block_role: BlockRole | None) -> str:
        exercise = _exercise(item.exercise_id)
        return prescribing.prescription_load_label(item, exercise, "en", role=block_role)

    # Shadow boxing as the warm-up or as conditioning is easy or capacity work: no cue.
    assert label(rounds, BlockRole.WARM_UP) == "bodyweight"
    assert label(rounds, BlockRole.CONDITIONING) == "bodyweight"
    assert label(rounds, BlockRole.POWER) == (
        "bodyweight, fast and sharp throughout; end the set when speed or technique drops"
    )
    assert label(hops, BlockRole.POWER) == (
        "bodyweight, every rep fast; end the set when speed or technique drops"
    )
    week = plan([(0, "A")], workout("A", "Boxing", [role(BlockRole.WARM_UP, rounds)]))
    assert "speed" not in render_plan_text(week, catalog=_CATALOG, lang="en", title="Draft:")


# --- Review follow-ups -----------------------------------------------------------------------


def test_a_negated_split_request_is_not_a_split_request() -> None:
    for request in ("no split, full body please", "без сплита", "не сплит, а фулбоди"):
        assert not programming.split_wanted(_context(), request), request
    for request in ("upper/lower split", "сплит верх/низ"):
        assert programming.split_wanted(_context(), request), request


def test_a_block_called_warm_up_must_hold_warm_up_work() -> None:
    week = plan(
        [(0, "A")],
        workout(
            "A",
            "Lower",
            [
                role(BlockRole.WARM_UP, rx("dumbbell_lateral_raise", 2, reps=(12, 15))),
                role(BlockRole.STRENGTH, rx("barbell_back_squat", 4, reps=(4, 6))),
            ],
        ),
    )
    details = {f.rule: f.detail for f in review(week, ReviewContext(_CATALOG))}
    assert "quality.warm_up" in details  # the heavy squat still has no warm-up
    assert "not general preparation" in details["quality.role"]


def test_fast_work_after_strength_is_reported_whatever_its_block_is_called() -> None:
    week = plan(
        [(0, "A")],
        workout(
            "A",
            "Lower",
            [
                role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
                role(BlockRole.STRENGTH, rx("barbell_back_squat", 4, reps=(4, 6))),
                role(BlockRole.ACCESSORY, rx("medicine_ball_chest_pass", 4, reps=(5, 5))),
            ],
        ),
    )
    order = [f.detail for f in review(week, ReviewContext(_CATALOG)) if f.rule == "quality.order"]
    assert order == [
        "workout A: medicine_ball_chest_pass (power/speed) comes after heavy barbell_back_squat; "
        "put fast work before strength and conditioning work, while fresh"
    ]
    # Fast exercises done for capacity at the end are fine.
    finisher = plan(
        [(0, "A")],
        workout(
            "A",
            "Lower",
            [
                role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
                role(BlockRole.STRENGTH, rx("barbell_back_squat", 4, reps=(4, 6))),
                role(BlockRole.CONDITIONING, rx("kettlebell_swing", 4, reps=(12, 15), rest=60)),
            ],
        ),
    )
    assert "quality.order" not in rules(review(finisher, ReviewContext(_CATALOG)))


def test_speed_work_in_a_superset_needs_recovery_across_the_round() -> None:
    def findings(*items: Prescription) -> list[str]:
        week = plan(
            [(0, "A")],
            workout(
                "A",
                "Power",
                [
                    role(BlockRole.WARM_UP, rx("jump_rope_skipping", 2, seconds=120, rest=30)),
                    role(BlockRole.POWER, *items),
                ],
            ),
        )
        ctx = ReviewContext(_CATALOG)
        return [f.detail for f in review(week, ctx) if f.rule == "quality.prescription"]

    throw = rx("medicine_ball_chest_pass", 3, reps=(5, 5), rest=0)
    no_rest = findings(throw, rx("side_plank", 3, seconds=20, rest=0, load={"kind": "bodyweight"}))
    assert len(no_rest) == 1 and "20 s recovery per superset round" in no_rest[0]
    rested = findings(
        rx("medicine_ball_chest_pass", 3, reps=(5, 5), rest=15),
        rx("side_plank", 3, seconds=20, rest=60, load={"kind": "bodyweight"}),
    )
    assert rested == []
