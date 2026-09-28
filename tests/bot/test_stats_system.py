"""`/stats` and `/system` (A§6.2, A§6.7): real implementations replacing the M9 stubs."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from conftest import FakeSession, make_user, message_update
from test_setup_flow import _run_full_setup_with_no_red_flags

from fitme import clock
from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision, insert_llm_call
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.domain.enums import DecisionKind
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout

OWNER_CHAT_ID = 1
_SQUAT = "barbell_back_squat"


async def _seed_plan_and_session(db: Database, user_id: int) -> None:
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
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="completed"
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id=_SQUAT,
            set_index=1,
            planned_load_kg=60.0,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=60.0,
            actual_reps=6,
            rpe=None,
            source="button",
        )
        await conn.execute(
            "UPDATE workout_sessions SET started_at = ?, finished_at = ? WHERE id = ?",
            (clock.utc_now(), clock.utc_now(), session_id),
        )
        await insert_llm_call(
            conn,
            decision_id=None,
            purpose="plan_generate",
            model="anthropic:claude-sonnet-5",
            input_tokens=1000,
            output_tokens=200,
            cost_estimate_usd=0.01,
            latency_ms=500,
            ok=True,
        )


async def test_stats_reports_sessions_volume_and_top_lift(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _run_full_setup_with_no_red_flags(dispatcher, bot, db)
    await _seed_plan_and_session(db, user_id)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/stats")
    )

    text = session.last_sent_text()
    assert text is not None
    assert "360" in text  # 60 kg * 6 reps = 360 kg volume
    assert "60" in text  # the top lift's current working load


async def test_system_reports_db_size_sessions_and_llm_usage(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _run_full_setup_with_no_red_flags(dispatcher, bot, db)
    await _seed_plan_and_session(db, user_id)
    owner = make_user(OWNER_CHAT_ID)

    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text="/system")
    )

    text = session.last_sent_text()
    assert text is not None
    assert "1" in text  # one session logged
    assert "1000" in text  # input tokens
    assert "0.0100" in text  # cost estimate
    assert content_version()[:12] in text
