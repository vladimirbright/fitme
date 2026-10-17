"""A plan with a non-catalog activity item (`activity:<slug>`) and its own location
(`Plan.location`) through the services that judge, review, time and save plans (A§4.5),
against a real migrated temp DB. No LLM is called: the draft is written with
`planning.record_edit_draft`, the same judging path every model draft takes."""

from __future__ import annotations

from test_planning_service import _settings

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
