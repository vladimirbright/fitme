"""Every stored timestamp matches the canonical A§4.2 format:
`YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC, always 6 fractional digits). 0001_init.sql doesn't
enforce this with a CHECK on every timestamp column (it would be the same GLOB pattern
repeated ~15 times), so this test asserts it by seeding a representative row in every
timestamp-bearing table and inspecting the stored values directly.
"""

from __future__ import annotations

import re
from datetime import timedelta

from fitme.clock import now
from fitme.db.connection import Database
from fitme.db.controllers.auth import insert_activation_code, insert_login_code, insert_web_session
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome, insert_llm_call
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.profile import (
    insert_screening_note,
    upsert_profile,
    upsert_screening_flag,
)
from fitme.db.controllers.training import (
    answer_checkin,
    clear_health_hold,
    finish_workout_session,
    insert_checkin,
    insert_health_hold,
    insert_set_log,
    insert_workout_session,
    start_workout_session,
)
from fitme.db.controllers.users import insert_telegram_account

_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


async def _timestamp_columns(db: Database, table: str) -> list[str]:
    async with db.read() as conn, conn.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return [row[1] for row in rows if row[1].endswith("_at")]


async def _application_tables(db: Database) -> list[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def test_every_stored_timestamp_matches_the_canonical_format(
    db: Database, user_id: int
) -> None:
    expires_at = now() + timedelta(minutes=15)
    async with db.transaction() as conn:
        await insert_telegram_account(conn, user_id=user_id, telegram_user_id=1, chat_id=1)
        await insert_activation_code(conn, code_hash="a", expires_at=expires_at)
        await insert_login_code(conn, user_id=user_id, code_hash="b", expires_at=expires_at)
        await insert_web_session(conn, id_hash="s", user_id=user_id, expires_at=expires_at)
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket=None,
            experience=None,
            barbell_experience=None,
            preferences=[],
            location=None,
            equipment=[],
            sessions_per_week=None,
            session_minutes=None,
            focus=None,
            completed_at=now(),
        )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="hernia", value="no", clearance=None
        )
        await insert_screening_note(conn, user_id=user_id, text="note")

        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_generate",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )
        await insert_decision_outcome(conn, decision_id=decision_id, outcome={})
        await insert_llm_call(
            conn,
            decision_id=decision_id,
            purpose="plan_generate",
            model="m",
            input_tokens=1,
            output_tokens=1,
            cost_estimate_usd=None,
            latency_ms=1,
            ok=True,
        )

        plan_id = await insert_plan(
            conn, user_id=user_id, name="P", is_default=True, status="active"
        )
        plan_version_id = await insert_plan_version(
            conn, plan_id=plan_id, version=1, body={}, origin="llm", decision_id=decision_id
        )

        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=plan_version_id, workout_key="A", status="draft"
        )
        await start_workout_session(conn, session_id)
        await finish_workout_session(conn, session_id, status="completed")
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id="e",
            set_index=1,  # A§4.2: set_index is 1-based
            planned_load_kg=None,
            planned_reps_min=None,
            planned_reps_max=None,
            actual_load_kg=None,
            actual_reps=None,
            rpe=None,
            source="button",
        )
        hold_id = await insert_health_hold(
            conn, user_id=user_id, reason="stop_word", source_session_id=session_id
        )
        await clear_health_hold(conn, hold_id)
        checkin_id = await insert_checkin(
            conn, user_id=user_id, session_id=session_id, question_key="area:knee"
        )
        await answer_checkin(conn, checkin_id, answer="fine")
        await insert_chat_message(
            conn, user_id=user_id, session_id=session_id, direction="in", text="hi"
        )
    violations: list[str] = []
    for table in await _application_tables(db):
        columns = await _timestamp_columns(db, table)
        if not columns:
            continue
        column_list = ", ".join(columns)
        async with (
            db.read() as conn,
            conn.execute(f"SELECT {column_list} FROM {table}") as cursor,  # trusted, from PRAGMA
        ):
            rows = await cursor.fetchall()
        for row in rows:
            for column, value in zip(columns, row, strict=True):
                if value is None:
                    continue
                if not _TIMESTAMP_PATTERN.match(value):
                    violations.append(f"{table}.{column} = {value!r}")

    assert not violations, "timestamps not in canonical format:\n" + "\n".join(violations)
