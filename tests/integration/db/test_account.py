"""Export/delete acceptance (M1, A§8.3): seeds every table, exports, deletes, and asserts
zero rows remain — the table list is derived from `sqlite_master` so it can't drift."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fitme.clock import now
from fitme.db.connection import Database, open_database
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
    insert_checkin,
    insert_health_hold,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.controllers.users import insert_telegram_account, insert_user
from fitme.db.migrate import migrate
from fitme.services.account import delete_user, export_user

# Every application table (i.e. every sqlite_master table except migration-runner
# infrastructure). Used only to assert this test seeded everything; the *behavior* under
# test derives its own list from sqlite_master, independent of this one.
_EXPECTED_TABLES = {
    "users",
    "telegram_accounts",
    "activation_codes",
    "login_codes",
    "web_sessions",
    "profiles",
    "screening_flags",
    "screening_notes",
    "plans",
    "decisions",
    "plan_versions",
    "workout_sessions",
    "set_logs",
    "health_holds",
    "checkins",
    "chat_messages",
    "decision_outcomes",
    "llm_calls",
}


async def _all_application_tables(db: Database) -> set[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


async def _row_count(db: Database, table: str) -> int:
    async with (
        db.read() as conn,
        conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor,  # test-only, trusted name
    ):
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _seed_every_table(db: Database, user_id: int) -> int:
    """Seeds every table and returns the workout_sessions id, for the wal-checkpoint marker
    test below."""
    expires_at = now() + timedelta(minutes=15)
    async with db.transaction() as conn:
        await insert_telegram_account(conn, user_id=user_id, telegram_user_id=1, chat_id=1)
        await insert_activation_code(conn, code_hash="activation-hash", expires_at=expires_at)
        await insert_login_code(
            conn, user_id=user_id, code_hash="login-hash", expires_at=expires_at
        )
        await insert_web_session(
            conn, id_hash="session-hash", user_id=user_id, expires_at=expires_at
        )
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="lt_6m",
            barbell_experience="some",
            preferences=["full_body"],
            location="home_equipment",
            equipment=["dumbbells"],
            sessions_per_week=3,
            session_minutes=45,
            focus="strength",
            completed_at=now(),
        )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="knee_injury_current", value="yes", clearance="yes"
        )
        await insert_screening_note(conn, user_id=user_id, text="note")

        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_generate",
            prompt_template="t",
            prompt_version="v1",
            model="anthropic:claude-opus-5",
            content_version="abc123def456",
            llm_input={"a": 1},
            user_report=None,
            proposal={"b": 2},
            guards_fired=[],
        )
        await insert_decision_outcome(conn, decision_id=decision_id, outcome={"c": 3})
        await insert_llm_call(
            conn,
            decision_id=decision_id,
            purpose="plan_generate",
            model="anthropic:claude-opus-5",
            input_tokens=1,
            output_tokens=1,
            cost_estimate_usd=0.001,
            latency_ms=1,
            ok=True,
        )

        plan_id = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="active"
        )
        plan_version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "Plan A"},
            origin="llm",
            decision_id=decision_id,
        )

        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id="barbell_back_squat",
            set_index=0,
            planned_load_kg=40.0,
            planned_reps=5,
            actual_load_kg=40.0,
            actual_reps=5,
            rpe=None,
            source="button",
        )
        await insert_health_hold(
            conn, user_id=user_id, reason="stop_word", source_session_id=session_id
        )
        await insert_checkin(conn, user_id=user_id, session_id=session_id, question_key="area:knee")
        await insert_chat_message(
            conn,
            user_id=user_id,
            session_id=session_id,
            direction="in",
            text="wal-checkpoint-marker-9f3a1c",
        )
    return session_id


async def test_seed_covers_every_application_table(db: Database, user_id: int) -> None:
    assert await _all_application_tables(db) == _EXPECTED_TABLES
    await _seed_every_table(db, user_id)
    for table in _EXPECTED_TABLES:
        assert await _row_count(db, table) >= 1, f"seed left {table} empty"


async def test_export_returns_every_seeded_table(db: Database, user_id: int) -> None:
    await _seed_every_table(db, user_id)

    exported = await export_user(db, user_id)

    assert set(exported) == _EXPECTED_TABLES
    for table in _EXPECTED_TABLES:
        assert len(exported[table]) >= 1, f"export returned nothing for {table}"


async def test_delete_user_leaves_zero_rows_in_every_table(db: Database, user_id: int) -> None:
    await _seed_every_table(db, user_id)

    await delete_user(db, user_id)

    tables = await _all_application_tables(db)
    assert tables == _EXPECTED_TABLES  # delete must not have dropped a table, only its rows
    for table in tables:
        assert await _row_count(db, table) == 0, f"{table} still has rows after delete_user"


async def test_delete_user_checkpoints_the_wal_so_no_trace_survives_on_disk(
    tmp_path: Path,
) -> None:
    """After delete + VACUUM, wal_checkpoint(TRUNCATE) folds the WAL back into the main file
    and truncates it, so deleted data can't be recovered by reading the raw files while the
    connection is still open."""
    # Doesn't use the `db`/`user_id` fixtures: this test needs its own on-disk path (to read
    # the raw file bytes afterwards), not the shared tmp_path/fitme-test.db from conftest.
    db_path = tmp_path / "fitme.db"
    marker = "wal-checkpoint-marker-9f3a1c"

    database = await open_database(db_path)
    try:
        await migrate(database)
        async with database.transaction() as conn:
            uid = await insert_user(conn, language="en", timezone=None)
        await _seed_every_table(database, uid)
        await delete_user(database, uid)

        db_bytes = db_path.read_bytes()
        wal_path = db_path.with_name(db_path.name + "-wal")
        wal_bytes = wal_path.read_bytes() if wal_path.exists() else b""
        assert marker.encode() not in db_bytes
        assert marker.encode() not in wal_bytes
    finally:
        await database.close()
