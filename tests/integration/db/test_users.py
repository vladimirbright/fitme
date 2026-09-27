"""controllers/selectors for `users` and `telegram_accounts` (A§4.1); M1 acceptance: a
second user insert is rejected."""

from __future__ import annotations

import sqlite3

import pytest

from fitme.clock import utc_now
from fitme.db.connection import Database
from fitme.db.controllers.users import (
    SecondUserError,
    delete_telegram_account,
    insert_telegram_account,
    insert_user,
    update_user_language,
    update_user_timezone,
)
from fitme.db.selectors.users import (
    count_users,
    get_telegram_account_by_telegram_user_id,
    get_telegram_account_by_user_id,
    get_the_user,
    get_user,
)


async def test_insert_and_get_user(db: Database) -> None:
    async with db.transaction() as conn:
        created = await insert_user(conn, language="en", timezone=None)
    assert created == 1

    async with db.read() as conn:
        record = await get_user(conn, 1)
    assert record is not None
    assert record.language == "en"
    assert record.timezone is None

    async with db.read() as conn:
        assert await count_users(conn) == 1
        assert (await get_the_user(conn)) == record


async def test_second_user_insert_is_rejected(db: Database) -> None:
    async with db.transaction() as conn:
        await insert_user(conn, language="en", timezone=None)

    with pytest.raises(SecondUserError):
        async with db.transaction() as conn:
            await insert_user(conn, language="ru", timezone=None)

    async with db.read() as conn:
        assert await count_users(conn) == 1


async def test_users_id_check_constraint_rejects_a_non_1_id(db: Database) -> None:
    """M1 acceptance: "at most one user" is enforced by a DB constraint too, not just the
    controller. This bypasses insert_user (which always writes id=1) to prove the CHECK
    constraint itself, independently of the application code."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "INSERT INTO users (id, language, timezone, created_at) VALUES (2, 'en', NULL, ?)",
            (utc_now(),),
        )


async def test_update_user_timezone(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await update_user_timezone(conn, user_id, "America/New_York")

    async with db.read() as conn:
        record = await get_user(conn, user_id)
    assert record is not None
    assert record.timezone == "America/New_York"


async def test_update_user_language(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await update_user_language(conn, user_id, "ru")

    async with db.read() as conn:
        record = await get_user(conn, user_id)
    assert record is not None
    assert record.language == "ru"


async def test_telegram_account_link_and_unlink(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        account_id = await insert_telegram_account(
            conn, user_id=user_id, telegram_user_id=555, chat_id=555
        )
    assert account_id > 0

    async with db.read() as conn:
        by_telegram_id = await get_telegram_account_by_telegram_user_id(conn, 555)
        by_user_id = await get_telegram_account_by_user_id(conn, user_id)
    assert by_telegram_id is not None
    assert by_telegram_id.user_id == user_id
    assert by_user_id == by_telegram_id

    async with db.transaction() as conn:
        await delete_telegram_account(conn, user_id)

    async with db.read() as conn:
        assert await get_telegram_account_by_user_id(conn, user_id) is None
