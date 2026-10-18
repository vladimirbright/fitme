"""A plan with a non-catalog activity item (`activity:<slug>`) and its own location
(`Plan.location`) through the services that judge, review, time and save plans (A§4.5),
against a real migrated temp DB. Drafts the user stages go through
`planning.record_edit_draft`; the legacy agents (a `FunctionModel` `plan_generate` /
`plan_revise`) may not add an activity item or set a plan location (`AgentScope`)."""

from __future__ import annotations

import json

from fitme import clock
from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.plans import list_plan_versions
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, Location, Measure
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.services import (
    measures,
    plan_edit,
    plan_quality,
    planning,
    prescribing,
    session_time,
    time_budget,
)
from tests.integration.db.test_planning_service import FakeLlm, _settings, decisions

_ACTIVITY = "activity:bjj_class"


async def seed_gym_profile(db: Database, user_id: int) -> None:
    """A complete public-gym profile, every screening flag answered "no"."""
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="6m_2y",
            barbell_experience="some",
            preferences=["weight_training"],
            location="public_gym",
            equipment=[],
            sessions_per_week=2,
            session_minutes=90,
            focus="general_fitness",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS | AREA_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


def timed(exercise_id: str, seconds: int, *, name: str | None = None) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        activity_name=name,
        sets=1,
        measure=Measure.TIME,
        seconds=seconds,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
        note="Class at the club" if name else None,
    )


def outdoor_plan(*, location: Location | None = Location.OUTDOOR) -> Plan:
    """A: an outdoor interval jog, an easy jog, then a 90-minute BJJ class (Tuesday)."""
    return Plan(
        name="Run and BJJ",
        location=location,
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Run, then class",
                blocks=[
                    Block(kind="single", items=[timed("outdoor_interval_jog", 900)]),
                    Block(kind="single", items=[timed("easy_jog", 1200)]),
                    Block(kind="single", items=[timed(_ACTIVITY, 5400, name="BJJ class")]),
                ],
            )
        ],
    )


async def _inputs(db: Database, user_id: int) -> planning.Inputs:
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id, new_plan=True)
    return planning.build_inputs(load_catalog(), snapshot, _settings(), user_id)


async def test_a_gym_user_confirms_an_outdoor_plan_with_an_activity(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    edit = await planning.record_edit_draft(
        db, _settings(), user_id, plan=outdoor_plan(), plan_id=None, user_report={"test": True}
    )
    assert edit.round is not None, edit.errors
    confirmed = await planning.confirm_plan(db, _settings(), user_id, edit.round.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    assert confirmed.plan_id is not None

    async with db.read() as conn:
        versions = await list_plan_versions(conn, confirmed.plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert len(versions) == 1
    saved = Plan.model_validate(versions[0].body)
    assert saved.location == Location.OUTDOOR
    activity = saved.workouts[0].blocks[2].items[0]
    assert activity.exercise_id == _ACTIVITY
    assert activity.activity_name == "BJJ class"
    assert activity.seconds == 5400
    confirm = next(d for d in decisions if d.kind == "plan_confirm")
    assert confirm.load_changes == []


async def test_the_same_plan_without_its_location_is_refused_for_a_gym_user(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    edit = await planning.record_edit_draft(
        db,
        _settings(),
        user_id,
        plan=outdoor_plan(location=None),
        plan_id=None,
        user_report={"test": True},
    )
    assert edit.round is None
    assert any("outdoor_interval_jog: not available at public_gym" in e for e in edit.errors)


async def test_judging_repairs_nothing_on_an_activity_item(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    inputs = await _inputs(db, user_id)
    plan = outdoor_plan()
    assert measures.repair_measures(plan, inputs.catalog) == []
    assert prescribing.repair_intensity(plan, inputs.catalog) == []
    judgement = planning.judge(plan, inputs)
    assert judgement.ok, judgement.failures
    assert plan == outdoor_plan()
    assert any(v.rule == "plan.activity" for v in judgement.verdicts)


async def test_review_timing_and_web_edit_checks_tolerate_an_activity(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    inputs = await _inputs(db, user_id)
    catalog = inputs.catalog
    plan = outdoor_plan()

    findings = plan_quality.review(
        plan, plan_quality.ReviewContext(catalog=catalog, session_minutes=90)
    )
    assert all(not f.detail.startswith(_ACTIVITY) for f in findings if not f.ok)
    workout = plan.workouts[0]
    estimate = session_time.estimate(workout, catalog)
    assert estimate.minutes >= 90 + 15 + 20  # the class counts for its own length
    assert plan_quality.weekly_load(plan, catalog)
    assert time_budget.fit_plan(plan, catalog, 180) == []
    # The web editor's checks use the plan's location too.
    assert plan_edit.blocking_errors(plan, inputs.ctx) == []
    assert plan_edit.load_cap_warnings(plan, inputs.ctx) == {}
    profile_located = outdoor_plan(location=None)
    assert any(
        "outdoor_interval_jog" in e for e in plan_edit.blocking_errors(profile_located, inputs.ctx)
    )


# Legacy agents (AgentScope): no new activity items, no plan location


def _jog_and_class(*, location: Location | None = None, activity: bool = True) -> Plan:
    blocks = [Block(kind="single", items=[timed("easy_jog", 1200)])]
    if activity:
        blocks.append(Block(kind="single", items=[timed(_ACTIVITY, 3600, name="BJJ class")]))
    return Plan(
        name="Jog and class",
        location=location,
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[Workout(key="A", title="Jog", blocks=blocks)],
    )


def _failed_rule(decision: object, rule: str) -> bool:
    fired = getattr(decision, "guards_fired", None) or []
    return any(g["rule"] == rule and not g["ok"] for g in fired)


async def test_plan_generate_cannot_output_an_activity_item(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    llm = FakeLlm([_jog_and_class(), _jog_and_class()])
    result = await planning.propose_new_plan(db, llm.runtime(), user_id)
    assert result.refusal is not None and llm.calls == 2
    rounds = [d for d in await decisions(db, user_id) if d.kind == "plan_generate"]
    assert len(rounds) == 2
    assert all(_failed_rule(d, "plan.activity_not_allowed") for d in rounds)


async def test_plan_generate_cannot_set_a_plan_location(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    outdoor = _jog_and_class(location=Location.OUTDOOR, activity=False)
    llm = FakeLlm([outdoor, outdoor])
    result = await planning.propose_new_plan(db, llm.runtime(), user_id)
    assert result.refusal is not None and llm.calls == 2
    rounds = [d for d in await decisions(db, user_id) if d.kind == "plan_generate"]
    assert len(rounds) == 2
    assert all(_failed_rule(d, "plan.location_not_allowed") for d in rounds)


async def test_the_same_plan_staged_by_the_user_is_accepted(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    edit = await planning.record_edit_draft(
        db,
        _settings(),
        user_id,
        plan=_jog_and_class(location=Location.OUTDOOR),
        plan_id=None,
        user_report={"test": True},
    )
    assert edit.round is not None, edit.errors


async def _saved_outdoor_plan(db: Database, user_id: int) -> int:
    edit = await planning.record_edit_draft(
        db, _settings(), user_id, plan=outdoor_plan(), plan_id=None, user_report={"test": True}
    )
    assert edit.round is not None
    confirmed = await planning.confirm_plan(db, _settings(), user_id, edit.round.decision_id)
    assert confirmed.plan_id is not None
    return confirmed.plan_id


async def test_a_revision_keeps_the_base_location_and_its_activities(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    plan_id = await _saved_outdoor_plan(db, user_id)
    revised = outdoor_plan(location=None)  # the model leaves the location out
    revised.workouts[0].blocks[1].items[0].seconds = 1500
    llm = FakeLlm([revised])
    result = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "a longer easy jog"
    )
    assert result.plan is not None, result.refusal
    assert result.plan.location == Location.OUTDOOR
    assert result.plan.workouts[0].blocks[2].items[0].exercise_id == _ACTIVITY


async def test_a_revision_cannot_add_an_activity(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    plan_id = await _saved_outdoor_plan(db, user_id)
    added = outdoor_plan()
    added.workouts[0].blocks.append(
        Block(kind="single", items=[timed("activity:yoga_class", 3600, name="Yoga")])
    )
    llm = FakeLlm([added, added, added])
    result = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "add yoga"
    )
    assert result.refusal is not None
    rounds = [
        d for d in await decisions(db, user_id) if d.kind == "plan_revise" and d.model is not None
    ]
    assert rounds and all(_failed_rule(d, "plan.activity_not_allowed") for d in rounds)


async def test_session_adjust_scope_keeps_the_workouts_own_activities_only(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    inputs = await _inputs(db, user_id)
    workout = _jog_and_class().workouts[0]
    own = planning.AgentScope.of_workout(workout, location=None)
    assert planning.judge_workout(workout.model_copy(deep=True), inputs, scope=own).ok
    refused = planning.judge_workout(workout, inputs, scope=planning.AgentScope())
    assert any(v.rule == "plan.activity_not_allowed" for v in refused.failures)


def _renamed(plan: Plan) -> Plan:
    plan.workouts[0].blocks[2].items[0].activity_name = "Hill repeats"
    return plan


def _duplicated(plan: Plan) -> Plan:
    plan.workouts[0].blocks.append(plan.workouts[0].blocks[2].model_copy(deep=True))
    return plan


def _lengthened(plan: Plan) -> Plan:
    item = plan.workouts[0].blocks[2].items[0]
    item.sets, item.seconds = 10, 10800
    return plan


async def test_a_revision_cannot_rename_duplicate_or_lengthen_an_activity(
    db: Database, user_id: int
) -> None:
    """The scope carries each activity's name, count and total time from the base: a
    revision keeps `activity:bjj_class` only as the BJJ class it was."""
    await seed_gym_profile(db, user_id)
    plan_id = await _saved_outdoor_plan(db, user_id)
    for change in (_renamed, _duplicated, _lengthened):
        bad = change(outdoor_plan())
        llm = FakeLlm([bad, bad, bad])
        result = await planning.revise_plan(
            db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "change the class"
        )
        assert result.refusal is not None, change.__name__
    rounds = [
        d for d in await decisions(db, user_id) if d.kind == "plan_revise" and d.model is not None
    ]
    assert rounds and all(_failed_rule(d, "plan.activity_not_allowed") for d in rounds)


async def test_session_adjust_scope_refuses_a_renamed_duplicated_or_longer_activity(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    inputs = await _inputs(db, user_id)
    base = outdoor_plan()
    scope = planning.AgentScope.of_workout(base.workouts[0], location=base.location)
    kept = outdoor_plan().workouts[0]
    assert planning.judge_workout(kept, inputs, location=base.location, scope=scope).ok
    for change in (_renamed, _duplicated, _lengthened):
        workout = change(outdoor_plan()).workouts[0]
        judgement = planning.judge_workout(workout, inputs, location=base.location, scope=scope)
        assert any(v.rule == "plan.activity_not_allowed" for v in judgement.failures), (
            change.__name__
        )


async def test_the_generator_never_sees_an_activity_id_of_another_plan(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    await _saved_outdoor_plan(db, user_id)
    summary = await planning.existing_plans_summary(db, user_id)
    ids = [i for w in json.loads(json.dumps(summary))[0]["workouts"] for i in w["exercise_ids"]]
    assert ids == ["outdoor_interval_jog", "easy_jog"]


# Wording, time budget


async def test_a_forbidden_term_in_an_activity_name_is_replaced(db: Database, user_id: int) -> None:
    await seed_gym_profile(db, user_id)
    inputs = await _inputs(db, user_id)
    plan = _jog_and_class()
    plan.workouts[0].blocks[1].items[0].activity_name = "Session with my coach"
    judgement = planning.judge(plan, inputs)
    assert judgement.ok
    assert plan.workouts[0].blocks[1].items[0].activity_name == "Activity"
    assert any(
        v.rule == "wording.forbidden_term" and "activity name" in v.detail for v in judgement.fired
    )


def test_the_time_budget_never_trims_an_activity() -> None:
    squat = Prescription(
        exercise_id="barbell_back_squat",
        sets=5,
        reps_min=5,
        reps_max=5,
        load=Load(kind="calibration"),
        rest_seconds=180,
    )
    workout = Workout(
        key="A",
        title="Squat, then class",
        blocks=[
            Block(kind="single", items=[squat]),
            Block(kind="single", items=[timed(_ACTIVITY, 5400, name="BJJ class")]),
        ],
    )
    time_budget.fit_workout(workout, load_catalog(), 60)
    ids = [item.exercise_id for block in workout.blocks for item in block.items]
    assert _ACTIVITY in ids
    assert workout.blocks[-1].items[0].seconds == 5400
