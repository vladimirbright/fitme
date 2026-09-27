"""Retention job acceptance (M1): purges old `chat_messages` and expired auth artifacts,
keeps `set_logs` untouched (A§8.4)."""

from __future__ import annotations

from datetime import datetime, timedelta

from fitme.clock import format_timestamp, now
from fitme.db.connection import Database
from fitme.db.controllers.auth import insert_activation_code, insert_login_code, insert_web_session
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.db.selectors.auth import (
    get_activation_code_by_hash,
    get_login_code_by_hash,
    get_web_session,
)
from fitme.db.selectors.chat import count_chat_messages_for_user
from fitme.db.selectors.training import list_set_logs_for_session
from fitme.services.retention import purge


async def _insert_chat_message_at(
    db: Database, *, user_id: int, text: str, created_at: datetime
) -> None:
    """Test-only helper: insert_chat_message always stamps 'now' (A§4.2), so a fixture that
    needs a specific, controllable created_at goes straight to SQL instead."""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO chat_messages (user_id, session_id, direction, text, created_at) "
            "VALUES (?, NULL, 'in', ?, ?)",
            (user_id, text, format_timestamp(created_at)),
        )


async def test_purge_deletes_old_chat_messages_and_keeps_recent_ones(
    db: Database, user_id: int
) -> None:
    await _insert_chat_message_at(
        db, user_id=user_id, text="old message", created_at=now() - timedelta(days=400)
    )
    async with db.transaction() as conn:
        await insert_chat_message(
            conn, user_id=user_id, session_id=None, direction="in", text="recent message"
        )

    result = await purge(db, chat_retention_days=365)
    assert result.chat_messages_deleted == 1

    async with db.read() as conn:
        remaining = await count_chat_messages_for_user(conn, user_id)
    assert remaining == 1


async def test_purge_keeps_set_logs(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
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
        plan_id = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="active"
        )
        plan_version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "Plan A", "workouts": []},
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

    # Retention never touches set_logs regardless of age, so its actual created_at doesn't
    # matter here; a very long retention window just makes the intent explicit.
    await purge(db, chat_retention_days=365)

    async with db.read() as conn:
        logs = await list_set_logs_for_session(conn, session_id)
    assert len(logs) == 1


async def test_purge_expired_auth_artifacts(db: Database, user_id: int) -> None:
    expired = now() - timedelta(days=1)
    valid = now() + timedelta(days=1)
    async with db.transaction() as conn:
        await insert_activation_code(conn, code_hash="expired-code", expires_at=expired)
        await insert_activation_code(conn, code_hash="valid-code", expires_at=valid)
        await insert_login_code(
            conn, user_id=user_id, code_hash="expired-login", expires_at=expired
        )
        await insert_login_code(conn, user_id=user_id, code_hash="valid-login", expires_at=valid)
        await insert_web_session(
            conn, id_hash="expired-session", user_id=user_id, expires_at=expired
        )
        await insert_web_session(conn, id_hash="valid-session", user_id=user_id, expires_at=valid)

    result = await purge(db, chat_retention_days=365)
    assert result.activation_codes_deleted == 1
    assert result.login_codes_deleted == 1
    assert result.web_sessions_deleted == 1

    async with db.read() as conn:
        assert await get_activation_code_by_hash(conn, "expired-code") is None
        assert await get_activation_code_by_hash(conn, "valid-code") is not None
        assert await get_login_code_by_hash(conn, "expired-login") is None
        assert await get_login_code_by_hash(conn, "valid-login") is not None
        assert await get_web_session(conn, "expired-session") is None
        assert await get_web_session(conn, "valid-session") is not None
