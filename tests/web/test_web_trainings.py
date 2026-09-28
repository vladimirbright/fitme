"""A§9.4 training-log delete: single and bulk, via the real web routes. Foreign/unknown id
aborts the whole batch; an open hold and non-`fine` check-ins are detached, never deleted;
`fine` check-ins are deleted; the weekly increment cap (read from `decisions`, never
`set_logs`) still blocks a second increase after the session is gone; one `session_delete`
decision is written."""

from __future__ import annotations

from datetime import timedelta

from conftest import SentCode, get_csrf_token, login
from httpx import AsyncClient

from fitme import clock
from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import (
    answer_checkin,
    insert_checkin,
    insert_health_hold,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.selectors.decisions import list_decisions_for_user, recent_increase_deltas_by_exercise
from fitme.db.selectors.training import (
    get_checkin,
    historical_max_by_exercise,
    list_open_health_holds,
    list_set_logs_for_session,
)
from fitme.domain.enums import DecisionKind
from fitme.domain.models import Block, Load, LoadChange, Plan, Prescription, ScheduledDay, Workout
from fitme.guards.progression import check_weekly_increment

_SQUAT = "barbell_back_squat"


async def seed_plan(db: Database, user_id: int) -> int:
    plan = Plan(
        name="P",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="A",
                blocks=[
                    Block(
                        kind="single",
                        items=[
                            Prescription(
                                exercise_id=_SQUAT,
                                sets=1,
                                reps_min=5,
                                reps_max=8,
                                load=Load(kind="kg", kg=60.0),
                                rest_seconds=90,
                            )
                        ],
                    )
                ],
            )
        ],
    )
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
            conn, user_id=user_id, name="P", is_default=True, status="active"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body=plan.model_dump(mode="json"),
            origin="llm",
            decision_id=decision_id,
        )
    return version_id


async def seed_session(
    db: Database, user_id: int, version_id: int, *, kg: float, reps: int = 6
) -> int:
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="completed"
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id=_SQUAT,
            set_index=1,
            planned_load_kg=kg,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=kg,
            actual_reps=reps,
            rpe=None,
            source="button",
        )
        await conn.execute(
            "UPDATE workout_sessions SET started_at = ?, finished_at = ? WHERE id = ?",
            (clock.utc_now(), clock.utc_now(), session_id),
        )
    return session_id


async def test_a_draft_sessions_missing_started_at_shows_a_fallback_label(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    """M9 review ("ALSO" #7): a `draft` session has no `started_at` yet — the list shows a
    fallback label, not an empty, invisible link."""
    version_id = await seed_plan(db, user_id)
    async with db.transaction() as conn:
        await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="draft"
        )

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    assert page.status_code == 200
    assert "—" in page.text  # the em-dash fallback, not an empty <a></a>


async def test_single_delete_removes_session_and_set_logs(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    version_id = await seed_plan(db, user_id)
    session_id = await seed_session(db, user_id, version_id, kg=60.0)

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/trainings/delete",
        data={"csrf_token": token, "ids": str(session_id), "next": "/app/trainings"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    assert rows == []


async def test_bulk_delete_rejects_whole_batch_on_foreign_id(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    version_id = await seed_plan(db, user_id)
    session_a = await seed_session(db, user_id, version_id, kg=60.0)
    session_b = await seed_session(db, user_id, version_id, kg=60.0)
    foreign_id = session_b + 9999

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/trainings/delete",
        data={
            "csrf_token": token,
            "ids": [str(session_a), str(session_b), str(foreign_id)],
            "next": "/app/trainings",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db.read() as conn:
        rows_a = await list_set_logs_for_session(conn, session_a)
        rows_b = await list_set_logs_for_session(conn, session_b)
    assert rows_a != []
    assert rows_b != []


async def test_delete_keeps_open_hold_and_detaches_non_fine_checkin_deletes_fine_one(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    version_id = await seed_plan(db, user_id)
    session_id = await seed_session(db, user_id, version_id, kg=60.0)

    async with db.transaction() as conn:
        hold_id = await insert_health_hold(
            conn, user_id=user_id, reason="stop_word", source_session_id=session_id
        )
        worse_checkin_id = await insert_checkin(
            conn, user_id=user_id, session_id=session_id, question_key="area:lower_back"
        )
        await answer_checkin(conn, worse_checkin_id, answer="worse")
        fine_checkin_id = await insert_checkin(
            conn, user_id=user_id, session_id=session_id, question_key="area:knee"
        )
        await answer_checkin(conn, fine_checkin_id, answer="fine")

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/trainings/delete",
        data={"csrf_token": token, "ids": str(session_id), "next": "/app/trainings"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        worse_checkin = await get_checkin(conn, worse_checkin_id)
        fine_checkin = await get_checkin(conn, fine_checkin_id)
    assert any(h.id == hold_id for h in holds)
    assert worse_checkin is not None
    assert worse_checkin.session_id is None
    assert fine_checkin is None


async def test_delete_writes_session_delete_decision(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    version_id = await seed_plan(db, user_id)
    session_id = await seed_session(db, user_id, version_id, kg=60.0)

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    await client.post(
        "/app/trainings/delete",
        data={"csrf_token": token, "ids": str(session_id), "next": "/app/trainings"},
        follow_redirects=False,
    )

    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    session_delete = next(d for d in decisions if d.kind == "session_delete")
    assert session_delete.user_report is not None
    assert session_delete.user_report["sessions"][0]["session_id"] == session_id


async def test_deleting_an_in_progress_session_logs_it_as_aborted(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    """A§9.4/M9 review ("ALSO" #7): a still-`in_progress` session is marked `aborted` before
    it's deleted — the decision log should describe what actually happened (an abort), not
    a status that no longer applies once the row is gone."""
    version_id = await seed_plan(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="A",
            status="in_progress",
        )

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    response = await client.post(
        "/app/trainings/delete",
        data={"csrf_token": token, "ids": str(session_id), "next": "/app/trainings"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    session_delete = next(d for d in decisions if d.kind == "session_delete")
    assert session_delete.user_report is not None
    assert session_delete.user_report["sessions"][0]["status"] == "aborted"


async def test_weekly_cap_still_blocks_after_deleting_the_increase_session(
    client: AsyncClient, db: Database, user_id: int, sent_codes: list[SentCode]
) -> None:
    """The increase-session (60 kg, +2.5 kg over a 57.5 kg history) is deleted; the applying
    decision that recorded the +2.5 kg change is untouched (append-only), so a further
    2.5 kg increase this week is still rejected by the guard."""
    version_id = await seed_plan(db, user_id)
    await seed_session(db, user_id, version_id, kg=57.5)
    increase_session_id = await seed_session(db, user_id, version_id, kg=60.0)

    async with db.transaction() as conn:
        await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.SESSION_ADJUST.value,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report={"session_id": increase_session_id, "event": "start"},
            proposal=None,
            guards_fired=[],
            load_changes=[LoadChange(exercise_id=_SQUAT, from_kg=57.5, to_kg=60.0)],
        )

    await login(client, sent_codes)
    page = await client.get("/app/trainings")
    token = get_csrf_token(page.text)
    await client.post(
        "/app/trainings/delete",
        data={"csrf_token": token, "ids": str(increase_session_id), "next": "/app/trainings"},
        follow_redirects=False,
    )

    since = clock.format_timestamp(clock.now() - timedelta(days=7))
    async with db.read() as conn:
        increases = await recent_increase_deltas_by_exercise(conn, user_id, since=since)
        history_max = await historical_max_by_exercise(conn, user_id)

    assert increases.get(_SQUAT) == [2.5]
    # The historical max can only go down after a delete (the conservative direction): the
    # deleted session's 60 kg is gone, the remaining one is 57.5 kg.
    assert history_max.get(_SQUAT) == 57.5

    exercise = load_catalog().by_id(_SQUAT)
    assert exercise is not None
    verdict = check_weekly_increment(
        exercise, increases_7d=increases[_SQUAT], proposed_increase_kg=2.5, cap_kg=2.5
    )
    assert not verdict.ok
