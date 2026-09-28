"""A§9.1 plan pages: plan detail's "recent trainings" window, and the structured edit form's
over-the-cap warning/confirmation flow (logged as a `user_edit` decision with `load_changes`).
"""

from __future__ import annotations

import html
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


# --- Rename (all plans are equal, A§4.3) ---------------------------------------------------


async def _plan_name(db: Database, plan_id: int) -> str:
    from fitme.db.selectors.plans import get_plan

    async with db.read() as conn:
        record = await get_plan(conn, plan_id)
    assert record is not None
    return record.name


async def test_rename_form_renames_the_plan_and_logs_a_user_edit(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    page = await client.get(f"/app/plans/{plan_id}")
    assert f'action="/app/plans/{plan_id}/rename"' in page.text
    token = get_csrf_token(page.text)

    saved = await client.post(
        f"/app/plans/{plan_id}/rename",
        data={"csrf_token": token, "name": "  Upper / lower  "},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == f"/app/plans/{plan_id}"
    assert await _plan_name(db, plan_id) == "Upper / lower"
    detail = await client.get(f"/app/plans/{plan_id}")
    assert "<h1>Upper / lower</h1>" in detail.text
    user_edits = [
        d for d in await list_decisions_for_user_helper(db, user_id) if d.kind == "user_edit"
    ]
    assert len(user_edits) == 1
    assert user_edits[0].user_report == {"action": "rename", "plan_id": plan_id}
    assert user_edits[0].load_changes == []


async def test_rename_form_rejects_empty_long_forbidden_and_foreign_names(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    token = get_csrf_token((await client.get(f"/app/plans/{plan_id}")).text)

    for name, message in (
        ("   ", "The name can't be empty."),
        ("x" * 61, "The name can be at most 60 characters."),
        ("my personal trainer plan", "The name can't contain “trainer”."),
    ):
        rejected = await client.post(
            f"/app/plans/{plan_id}/rename", data={"csrf_token": token, "name": name}
        )
        assert rejected.status_code == 400, name
        assert message in html.unescape(rejected.text)
        assert "<h1>Home strength</h1>" in rejected.text  # the detail page, re-shown
    assert await _plan_name(db, plan_id) == "Home strength"

    foreign = await client.post(
        "/app/plans/999/rename", data={"csrf_token": token, "name": "Nope"}, follow_redirects=False
    )
    assert foreign.status_code == 303 and foreign.headers["location"] == "/app/plans"
    assert not any(d.kind == "user_edit" for d in await list_decisions_for_user_helper(db, user_id))


async def test_rename_form_requires_the_csrf_token(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    missing = await client.post(f"/app/plans/{plan_id}/rename", data={"name": "New"})
    assert missing.status_code == 403
    wrong = await client.post(
        f"/app/plans/{plan_id}/rename", data={"csrf_token": "bogus", "name": "New"}
    )
    assert wrong.status_code == 403
    assert await _plan_name(db, plan_id) == "Home strength"


async def test_plan_list_and_detail_show_no_status(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, make_plan(60.0))
    await login(client, sent_codes)
    listing = await client.get("/app/plans")
    assert 'class="status"' not in listing.text
    assert ">default<" in listing.text
    detail = await client.get(f"/app/plans/{plan_id}")
    assert "archived" not in detail.text and "Archive" not in detail.text


# --- The revise page shows the plan being revised ------------------------------------------


async def test_revise_page_shows_the_current_plan_below_the_form(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    from fitme.domain.models import Block, Load, Prescription, Workout

    plan = make_plan(60.0)
    bench = Prescription(
        exercise_id="dumbbell_bench_press",
        sets=3,
        reps_min=8,
        reps_max=12,
        load=Load(kind="calibration"),
        rest_seconds=90,
        note="pause at the bottom",
        declared_kg=22.5,
    )
    plan.workouts.append(
        Workout(key="B", title="Push", blocks=[Block(kind="single", items=[bench])])
    )
    await seed_profile(db, user_id)
    plan_id, _version_id = await seed_confirmed_plan(db, user_id, plan)
    await login(client, sent_codes)

    page = await client.get(f"/app/plans/{plan_id}/revise")
    assert page.status_code == 200
    form_end = page.text.index("</form>")
    body = page.text[form_end:]
    assert "Monday: A" in body and "Wednesday: A" in body
    assert "Workout A — Full body" in body and "Workout B — Push" in body
    assert "Barbell back squat" in body and "3 × 5–8" in body and "60.0 kg" in body
    assert "Push-up" in body and "bodyweight" in body
    assert "22.5 kg each" in body and "pause at the bottom" in body
    assert "calibration" in body

    # The very same partial renders the detail page.
    detail = await client.get(f"/app/plans/{plan_id}")
    assert "22.5 kg each" in detail.text and "pause at the bottom" in detail.text
