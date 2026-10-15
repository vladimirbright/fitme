"""`/stats` and `/system` (A§6.2, A§6.7): real implementations replacing the M9 stubs."""

from __future__ import annotations

import html
import re
from datetime import timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from conftest import FakeSession, make_user, message_update
from test_setup_flow import _run_full_setup_with_no_red_flags

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision, insert_llm_call
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.domain.enums import DecisionKind
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.i18n import t
from fitme.services import profile as profile_service

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
    assert "sonnet-5" in text  # a column for the one model called
    assert "0.01" in text  # its cost
    assert content_version()[:12] in text


async def test_system_breaks_costs_down_by_model_and_window(
    bot: Bot, session: FakeSession, db: Database, settings: Settings, tmp_path: Path
) -> None:
    """Per window (today, this week, this month, all time) a total and one line per model,
    most expensive first. A call stored without a cost (made before its model had a price)
    is estimated from today's price table, and the note says so."""
    prices = tmp_path / "prices.toml"
    prices.write_text(
        '[models."anthropic:claude-haiku-4-5"]\n'
        "input_per_million_usd = 1.0\noutput_per_million_usd = 5.0\n"
    )
    priced = settings.model_copy(update={"prices_file": prices})
    dispatcher = build_dispatcher(db, priced)
    user_id = await _run_full_setup_with_no_red_flags(dispatcher, bot, db)
    await _seed_plan_and_session(db, user_id)  # sonnet: $0.01, today
    async with db.transaction() as conn:
        await insert_llm_call(
            conn,
            decision_id=None,
            purpose="result_parse",
            model="anthropic:claude-haiku-4-5",
            input_tokens=1_000_000,
            output_tokens=0,
            cost_estimate_usd=None,  # before prices were configured: $1.00 at today's
            latency_ms=100,
            ok=True,
        )
        old = await insert_llm_call(
            conn,
            decision_id=None,
            purpose="plan_generate",
            model="anthropic:claude-opus-5",
            input_tokens=10,
            output_tokens=10,
            cost_estimate_usd=None,  # not in the table: stays unknown
            latency_ms=100,
            ok=True,
        )
        long_ago = clock.format_timestamp(clock.now() - timedelta(days=400))
        await conn.execute("UPDATE llm_calls SET created_at = ? WHERE id = ?", (long_ago, old))

    await dispatcher.feed_update(
        bot, message_update(user=make_user(OWNER_CHAT_ID), chat_id=OWNER_CHAT_ID, text="/system")
    )
    text = session.last_sent_text()
    assert text is not None
    lang = (await profile_service.get_snapshot(db, user_id)).language
    tables = re.findall(r"<pre>(.*?)</pre>", text, flags=re.S)
    assert len(tables) == 2  # cost, then calls
    costs = [line.split() for line in html.unescape(tables[0]).splitlines()]
    calls = [line.split() for line in html.unescape(tables[1]).splitlines()]
    total = t("system.total_column", lang)
    # Columns: every model ever called, the most expensive first, then the total.
    assert costs[0] == calls[0] == ["haiku-4-5", "sonnet-5", "opus-5", total]
    rows = [t(f"system.row_{key}", lang) for key in ("today", "week", "month", "all")]
    assert [line[0] for line in costs[1:]] == rows
    # $1.00 estimated at today's prices + $0.01 stored; opus has no price ("?"), and no
    # call outside all time ("–").
    for line in costs[1:4]:
        assert line[1:] == ["1.00", "0.01", "–", "1.01"]
    assert costs[4][1:] == ["1.00", "0.01", "?", "1.01"]
    for line in calls[1:4]:
        assert line[1:] == ["1", "1", "–", "2"]
    assert calls[4][1:] == ["1", "1", "1", "3"]
    # Aligned: the total column is right-aligned, so every line is the same width.
    widths = {len(line) for line in html.unescape(tables[0]).splitlines()}
    assert len(widths) == 1
    assert t("system.unpriced_note", lang) in html.unescape(text)
    assert t("system.estimate_note", lang) in html.unescape(text)
