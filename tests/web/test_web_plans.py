"""A§9.1 plan pages: plan detail's "recent trainings" window, and the structured edit form's
over-the-cap warning/confirmation flow (logged as a `user_edit` decision with `load_changes`).
"""

from __future__ import annotations

from datetime import timedelta

from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient

from fitme import clock
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, DecisionKind
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout

_SQUAT = "barbell_back_squat"


async def seed_profile(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="6m_2y",
            barbell_experience="some",
            preferences=["weight_training"],
            location="home_equipment",
            equipment=["barbell", "rack", "dumbbells", "bench"],
            sessions_per_week=2,
            session_minutes=60,
            focus="strength",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        for flag in AREA_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


def make_plan(squat_kg: float) -> Plan:
    squat = Prescription(
        exercise_id=_SQUAT,
        sets=3,
        reps_min=5,
        reps_max=8,
        load=Load(kind="kg", kg=squat_kg),
        rest_seconds=90,
    )
    pushup = Prescription(
        exercise_id="pushup",
        sets=3,
        reps_min=8,
        reps_max=12,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
    )
    return Plan(
        name="Home strength",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=2, workout_key="A"),
        ],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[Block(kind="single", items=[squat]), Block(kind="single", items=[pushup])],
            )
        ],
    )


async def seed_confirmed_plan(db: Database, user_id: int, plan: Plan) -> tuple[int, int]:
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.PLAN_CONFIRM.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
            load_changes=[],
        )
        plan_id = await insert_plan(
            conn, user_id=user_id, name=plan.name, is_default=True, status="active"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body=plan.model_dump(mode="json"),
            origin="llm",
            decision_id=decision_id,
        )
    return plan_id, version_id


async def seed_completed_session(
    db: Database, user_id: int, plan_version_id: int, *, kg: float, finished_days_ago: int
) -> int:
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        for index in (1, 2, 3):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=_SQUAT,
                set_index=index,
                planned_load_kg=kg,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=kg,
                actual_reps=6,
                rpe=None,
                source="button",
            )
        finished_at = clock.format_timestamp(clock.now() - timedelta(days=finished_days_ago))
        started_at = finished_at
        await conn.execute(
            "UPDATE workout_sessions SET started_at = ?, finished_at = ? WHERE id = ?",
            (started_at, finished_at, session_id),
        )
    return session_id


async def test_plan_detail_lists_only_sessions_from_last_14_days_across_versions(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    recent = await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=3)
    old = await seed_completed_session(db, user_id, version_id, kg=55.0, finished_days_ago=20)

    await login(client, sent_codes)
    response = await client.get(f"/app/plans/{plan_id}")
    assert response.status_code == 200
    assert f"/app/trainings/{recent}" in response.text
    assert f"/app/trainings/{old}" not in response.text


async def test_plan_edit_over_cap_needs_confirmation_and_logs_user_edit(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await seed_completed_session(db, user_id, version_id, kg=60.0, finished_days_ago=3)

    await login(client, sent_codes)
    edit_page = await client.get(f"/app/plans/{plan_id}/edit")
    assert edit_page.status_code == 200
    token = get_csrf_token(edit_page.text)

    # 70 kg is well above the 60 kg history max + one increment: a load-cap warning.
    form = {
        "csrf_token": token,
        "w0_b0_i0_exercise": _SQUAT,
        "w0_b0_i0_sets": "3",
        "w0_b0_i0_reps_min": "5",
        "w0_b0_i0_reps_max": "8",
        "w0_b0_i0_rest": "90",
        "w0_b0_i0_load_kind": "kg",
        "w0_b0_i0_load_kg": "70",
    }
    warned = await client.post(f"/app/plans/{plan_id}/edit", data=form)
    assert warned.status_code == 200
    assert "confirm_over_cap" in warned.text

    decisions_before = await list_decisions_for_user_helper(db, user_id)
    assert not any(d.kind == "user_edit" for d in decisions_before)

    form["confirm_over_cap"] = "on"
    saved = await client.post(f"/app/plans/{plan_id}/edit", data=form, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"] == f"/app/plans/{plan_id}"

    decisions_after = await list_decisions_for_user_helper(db, user_id)
    user_edit = next(d for d in decisions_after if d.kind == "user_edit")
    assert user_edit.load_changes
    change = user_edit.load_changes[0]
    assert change["exercise_id"] == _SQUAT
    assert change["to_kg"] == 70.0


async def list_decisions_for_user_helper(db: Database, user_id: int):
    async with db.read() as conn:
        return await list_decisions_for_user(conn, user_id)
