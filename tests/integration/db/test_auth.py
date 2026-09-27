"""controllers/selectors for `activation_codes`, `login_codes` and `web_sessions` (A§4.1)."""

from __future__ import annotations

from datetime import timedelta

from fitme.clock import now
from fitme.db.connection import Database
from fitme.db.controllers.auth import (
    delete_web_session,
    increment_login_code_attempts,
    insert_activation_code,
    insert_login_code,
    insert_web_session,
    mark_activation_code_used,
    mark_login_code_used,
    touch_web_session,
)
from fitme.db.selectors.auth import (
    get_activation_code_by_hash,
    get_login_code_by_hash,
    get_web_session,
    list_web_sessions_for_user,
)


async def test_activation_code_lifecycle(db: Database) -> None:
    expires_at = now() + timedelta(minutes=15)
    async with db.transaction() as conn:
        code_id = await insert_activation_code(conn, code_hash="hash-a", expires_at=expires_at)

    async with db.read() as conn:
        record = await get_activation_code_by_hash(conn, "hash-a")
    assert record is not None
    assert record.id == code_id
    assert record.used_at is None

    async with db.transaction() as conn:
        await mark_activation_code_used(conn, code_id)

    async with db.read() as conn:
        record = await get_activation_code_by_hash(conn, "hash-a")
    assert record is not None
    assert record.used_at is not None


async def test_login_code_lifecycle(db: Database, user_id: int) -> None:
    expires_at = now() + timedelta(minutes=5)
    async with db.transaction() as conn:
        code_id = await insert_login_code(
            conn, user_id=user_id, code_hash="hash-b", expires_at=expires_at
        )

    async with db.transaction() as conn:
        await increment_login_code_attempts(conn, code_id)
        await increment_login_code_attempts(conn, code_id)
        await mark_login_code_used(conn, code_id)

    async with db.read() as conn:
        record = await get_login_code_by_hash(conn, "hash-b")
    assert record is not None
    assert record.attempts == 2
    assert record.used_at is not None


async def test_web_session_lifecycle(db: Database, user_id: int) -> None:
    expires_at = now() + timedelta(days=14)
    async with db.transaction() as conn:
        await insert_web_session(
            conn, id_hash="session-hash", user_id=user_id, expires_at=expires_at
        )

    async with db.read() as conn:
        record = await get_web_session(conn, "session-hash")
        sessions = await list_web_sessions_for_user(conn, user_id)
    assert record is not None
    assert sessions == [record]

    async with db.transaction() as conn:
        await touch_web_session(conn, "session-hash")

    async with db.read() as conn:
        touched = await get_web_session(conn, "session-hash")
    assert touched is not None
    assert touched.last_seen_at >= record.last_seen_at

    async with db.transaction() as conn:
        await delete_web_session(conn, "session-hash")

    async with db.read() as conn:
        assert await get_web_session(conn, "session-hash") is None
