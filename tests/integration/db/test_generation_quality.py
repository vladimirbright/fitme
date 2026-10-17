"""ADR 0011 through the real `plan_generate` pipeline (a real migrated DB, the real catalog,
retrieval, guards, repair and review; a `FunctionModel` instead of a provider): what the model
is given before it picks exercises, and what happens to the loads and intensities it returns.

Scenario A (a farmer carry with no history) and B (a medicine-ball chest pass with a generic
`rir`) from the bug report, end to end."""

from __future__ import annotations

import json

import pytest
from test_planning_service import FakeLlm, _settings

from fitme import clock
from fitme.bot.plan_rendering import render_plan_text
from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, BlockRole, Measure, Quality
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.guards.plan import validate_plan
from fitme.services import planning, session_time, time_budget

_GYM = [
    "dumbbells", "barbell", "rack", "bench", "pull_up_bar", "kettlebell", "resistance_bands",
    "machine", "cable", "medicine_ball", "plyo_box", "jump_rope",
]  # fmt: skip


async def _seed_boxer(db: Database, user_id: int) -> None:
    """Two gym sessions a week next to three boxing sessions; no exclusions, no history."""
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="2y_5y",
            barbell_experience="yes",
            preferences=["weight_training", "conditioning"],
            location="public_gym",
            equipment=_GYM,
            sessions_per_week=2,
            session_minutes=60,
            focus="strength",
            completed_at=clock.now(),
            sport="boxing_martial_arts",
            sport_sessions_per_week=3,
        )
        for flag in [*RED_FLAGS, *AREA_FLAGS]:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


def _rx(exercise_id: str, sets: int, **fields: object) -> Prescription:
    body: dict[str, object] = {
        "exercise_id": exercise_id,
        "sets": sets,
        "load": {"kind": "calibration"},
        "rest_seconds": 90,
        **fields,
    }
    if "measure" not in body:
        body |= {"reps_min": 5, "reps_max": 6}
    return Prescription.model_validate(body)


def _model_plan() -> Plan:
    """What a model might return: a farmer carry with an invented 2 kg, and a chest pass
    and a carry with the strength-work `rir` 3."""
    return Plan(
        name="Boxing support",
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                qualities=[Quality.POWER, Quality.STRENGTH],
                title="Power and strength",
                blocks=[
                    Block(
                        kind="single",
                        role=BlockRole.WARM_UP,
                        items=[
                            _rx(
                                "jump_rope_skipping",
                                2,
                                measure="time",
                                seconds=120,
                                rest_seconds=30,
                                load={"kind": "bodyweight"},
                            )
                        ],
                    ),
                    Block(
                        kind="single",
                        role=BlockRole.POWER,
                        items=[_rx("medicine_ball_chest_pass", 4, rir=3)],
                    ),
                    Block(
                        kind="single",
                        role=BlockRole.STRENGTH,
                        items=[_rx("trap_bar_deadlift", 4, rir=2)],
                    ),
                    Block(
                        kind="single",
                        role=BlockRole.CONDITIONING,
                        items=[
                            _rx(
                                "dumbbell_farmer_carry",
                                3,
                                measure="distance",
                                meters=40,
                                load={"kind": "kg", "kg": 2.0},
                                rir=3,
                            )
                        ],
                    ),
                ],
            )
        ],
    )


async def test_the_model_gets_the_weekly_frame_and_stimulus_facts_before_exercises(
    db: Database, user_id: int
) -> None:
    await _seed_boxer(db, user_id)
    llm = FakeLlm([_model_plan(), _model_plan()])

    await planning.propose_new_plan(
        db, llm.runtime(), user_id, guidance="Поддержка бокса: взрывная мощность и работа ног"
    )

    payload = json.loads(llm.prompts[0])
    brief = payload["programming_brief"]
    assert brief["sessions_per_week"] == 2
    assert brief["structure"] == "full_body_varied_emphasis"  # two sessions: no split
    assert brief["fatigue_budget"] == "moderate"  # three boxing sessions a week
    assert brief["supporting_sport"] == {"sport": "boxing_martial_arts", "sessions_per_week": 3}
    assert brief["main_qualities"] == ["power", "agility"]  # what the request names first
    assert brief["session_order"][:3] == ["warm_up", "power", "strength"]
    facts = {fact["id"]: fact for fact in payload["exercise_facts"]}
    # Scenario C/D: general shadow boxing and real footwork drills are in the pool; a narrow
    # drill, if pooled, says it is one.
    assert "bodyweight_shadow_boxing" in facts
    footwork = {"stance_footwork_drill", "bodyweight_lateral_shuffle", "shuttle_run", "carioca"}
    assert len(footwork & set(facts)) >= 2
    if "bodyweight_shadow_boxing_hook" in facts:
        assert facts["bodyweight_shadow_boxing_hook"]["specific"] is True
    fast = [f for f in facts.values() if f["qualities"][0] in ("power", "rotational_power")]
    assert fast and all(fact["intensity"] == "speed" for fact in fast)
    # Objectives and roles come before exercises in the output schema the model fills.
    workout_fields = list(Workout.model_fields)
    assert workout_fields.index("qualities") < workout_fields.index("blocks")
    assert list(Block.model_fields).index("role") < list(Block.model_fields).index("items")


async def test_no_invented_carry_weight_and_no_rir_on_power_or_carry_work(
    db: Database, user_id: int
) -> None:
    await _seed_boxer(db, user_id)
    llm = FakeLlm([_model_plan(), _model_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.plan is not None
    items = {
        item.exercise_id: item
        for workout in result.plan.workouts
        for block in workout.blocks
        for item in block.items
    }
    carry = items["dumbbell_farmer_carry"]
    # Scenario A: no history, so the invented 2 kg is replaced by a calibration load; the
    # distance stays the prescription, and reps in reserve don't apply to a carry.
    assert carry.load == Load(kind="calibration")
    assert carry.measure == Measure.DISTANCE and carry.meters == 40
    assert carry.rir is None
    # Scenario B: an explosive throw is regulated by speed, not reps in reserve.
    assert items["medicine_ball_chest_pass"].rir is None
    # Strength work keeps its reps-in-reserve target.
    assert items["trap_bar_deadlift"].rir == 2
    dropped = [v.detail for v in result.guards_fired if v.rule == "intensity.rir_dropped"]
    assert any(detail.startswith("medicine_ball_chest_pass") for detail in dropped)

    text = render_plan_text(result.plan, catalog=load_catalog(), lang="en", title="Draft:")
    carry_line = next(line for line in text.splitlines() if "Dumbbell farmer carry" in line)
    assert "3 × 40 m @ calibration: a weight you can carry or hold for the whole set" in carry_line
    assert "2 kg" not in carry_line and "reps in reserve" not in carry_line
    pass_line = next(line for line in text.splitlines() if "Medicine ball chest pass" in line)
    assert "a load you can move fast with clean technique" in pass_line
    assert "reps in reserve" not in pass_line
    assert "Power and speed:" in text and "Warm-up:" in text  # block role headings


async def test_a_draft_chosen_from_an_earlier_attempt_is_still_the_one_confirm_saves(
    db: Database, user_id: int
) -> None:
    """Bug: the repair attempt had as many findings as the first, so best-of showed the first
    attempt's draft, while the newer repair decision made that draft stale on its own Confirm."""
    await _seed_boxer(db, user_id)
    llm = FakeLlm([_model_plan(), _model_plan()])

    result = await planning.propose_new_plan(
        db, llm.runtime(), user_id, guidance="Поддержка бокса: взрывная мощность и работа ног"
    )

    assert result.plan is not None and len(llm.prompts) == 2  # findings -> one repair round
    assert await planning.current_draft_id(db, user_id) == result.decision_id
    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED


def _long_plan() -> Plan:
    """What a model might return for a 60 min session: about 75 min of work, with two
    accessories that serve neither of the session's objectives."""

    def single(role: BlockRole, item: Prescription) -> Block:
        return Block(kind="single", role=role, items=[item])

    return Plan(
        name="Boxing support",
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                qualities=[Quality.AGILITY, Quality.CONDITIONING],
                title="Footwork",
                blocks=[
                    single(
                        BlockRole.WARM_UP,
                        _rx("jump_rope_skipping", 2, measure="time", seconds=120, rest_seconds=30),
                    ),
                    single(
                        BlockRole.POWER,
                        _rx("stance_footwork_drill", 6, measure="time", seconds=30),
                    ),
                    single(BlockRole.POWER, _rx("shuttle_run", 6, measure="distance", meters=40)),
                    single(
                        BlockRole.STRENGTH,
                        _rx("barbell_deadlift", 5, rir=2, rest_seconds=180),
                    ),
                    single(
                        BlockRole.ACCESSORY,
                        _rx(
                            "dumbbell_seated_shoulder_press",
                            4,
                            reps_min=8,
                            reps_max=10,
                            rest_seconds=120,
                        ),
                    ),
                    single(
                        BlockRole.ACCESSORY,
                        _rx("machine_lat_pulldown", 4, reps_min=8, reps_max=10, rest_seconds=120),
                    ),
                    single(
                        BlockRole.CONDITIONING,
                        _rx("battle_rope_alternating_waves", 8, measure="time", seconds=30),
                    ),
                ],
            )
        ],
    )


async def test_a_generated_workout_over_the_session_length_is_shortened_by_priority(
    db: Database, user_id: int
) -> None:
    """ADR 0016: the app estimates the generated workout and shortens it before the review
    and the draft; the draft the user sees is the shortened one, the change is logged, and
    the estimate is shown as an estimate."""
    await _seed_boxer(db, user_id)
    llm = FakeLlm([_long_plan(), _long_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.plan is not None
    catalog = load_catalog()
    workout = result.plan.workouts[0]
    assert session_time.estimate(workout, catalog).minutes <= 60  # the target itself
    kept = [item.exercise_id for block in workout.blocks for item in block.items]
    assert kept[:3] == ["jump_rope_skipping", "stance_footwork_drill", "shuttle_run"]
    assert "machine_lat_pulldown" not in kept
    budget = [v for v in result.guards_fired if v.rule == "plan.time_budget"]
    assert budget and budget[0].ok and "removed machine_lat_pulldown" in budget[0].detail
    payload = json.loads(llm.prompts[0])
    assert payload["programming_brief"]["time_budget"]["target_minutes"] == 60
    assert len(payload["programming_brief"]["session_objectives"]) == 2  # one per session
    text = render_plan_text(result.plan, catalog=catalog, lang="en", title="Draft:")
    assert "(estimated from the sets and rests, not measured)" in text


# --- ADR 0016 follow-up: the plan judged is the plan as shortened -----------------------------


def _two_long_sessions() -> Plan:
    """Two over-long workouts; B's only conditioning work is an accessory-ranked block."""
    first = _long_plan().workouts[0]
    second = first.model_copy(deep=True, update={"key": "B"})
    return Plan(
        name="Boxing support",
        schedule=[
            ScheduledDay(weekday=1, workout_key="A"),
            ScheduledDay(weekday=4, workout_key="B"),
        ],
        workouts=[first, second],
    )


async def _draft_decision_plan(db: Database, user_id: int, decision_id: int) -> Plan:
    async with db.read() as conn:
        decisions = {d.id: d for d in await list_decisions_for_user(conn, user_id)}
    proposal = decisions[decision_id].proposal
    assert proposal is not None
    return Plan.model_validate(proposal["plan"])


async def test_every_shortened_session_is_judged_again_and_the_draft_is_what_was_judged(
    db: Database, user_id: int
) -> None:
    await _seed_boxer(db, user_id)
    llm = FakeLlm([_two_long_sessions(), _two_long_sessions()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.plan is not None
    budget = [v for v in result.guards_fired if v.rule == "plan.time_budget"]
    assert [v.detail.split(":")[0] for v in budget] == ["workout A", "workout B"]
    # The stored draft is the shortened plan, and the guards pass on exactly it.
    stored = await _draft_decision_plan(db, user_id, result.decision_id)
    assert stored == result.plan
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    inputs = planning.build_inputs(load_catalog(), snapshot, _settings(), user_id)
    assert all(v.ok for v in validate_plan(stored, inputs.ctx))
    # The review ran on the shortened plan: nothing it says names a removed exercise.
    findings = [v.detail for v in result.guards_fired if v.rule.startswith("quality.")]
    assert not any("machine_lat_pulldown" in detail for detail in findings)


async def test_a_shortened_plan_that_fails_the_guards_is_not_used(
    db: Database, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never expected from trimming, which only removes sets and blocks — but if the plan as
    changed fails the guards, the plan that passed them is the draft, with a note."""
    await _seed_boxer(db, user_id)

    def broken_fit(plan: Plan, catalog: object, minutes: int) -> list[GuardVerdict]:
        plan.workouts[0].blocks[1].items[0].exercise_id = "not_a_catalog_exercise"
        return [GuardVerdict(rule="plan.time_budget", ok=True, detail="workout A: changed")]

    monkeypatch.setattr(time_budget, "fit_plan", broken_fit)
    llm = FakeLlm([_long_plan(), _long_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.plan is not None
    ids = [i.exercise_id for b in result.plan.workouts[0].blocks for i in b.items]
    assert "not_a_catalog_exercise" not in ids and "machine_lat_pulldown" in ids
    notes = [v.detail for v in result.guards_fired if v.rule == "plan.time_budget"]
    assert any(
        note.startswith("shortened plan failed the guards and was not used") for note in notes
    )
