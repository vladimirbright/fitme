"""Read-side access for `users` and `telegram_accounts` (A§4.1)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import TelegramAccountRecord, UserRecord


async def get_user(conn: aiosqlite.Connection, user_id: int) -> UserRecord | None:
    async with conn.execute(
        "SELECT id, language, timezone, created_at FROM users WHERE id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return UserRecord(id=row[0], language=row[1], timezone=row[2], created_at=row[3])


async def get_the_user(conn: aiosqlite.Connection) -> UserRecord | None:
    """This instance is single-user (ADR 0002): there is at most one row to fetch."""
    async with conn.execute(
        "SELECT id, language, timezone, created_at FROM users LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return UserRecord(id=row[0], language=row[1], timezone=row[2], created_at=row[3])


async def count_users(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def any_telegram_account_bound(conn: aiosqlite.Connection) -> bool:
    """Whether *any* Telegram account is currently linked (A§6.1): `/activate <code>` is only
    allowed while this is false. Distinct from `count_users`: a user row can outlive its
    Telegram link across a `--rebind` (the old link is deleted first, A§6.1)."""
    async with conn.execute("SELECT 1 FROM telegram_accounts LIMIT 1") as cursor:
        row = await cursor.fetchone()
    return row is not None


async def get_telegram_account_by_telegram_user_id(
    conn: aiosqlite.Connection, telegram_user_id: int
) -> TelegramAccountRecord | None:
    async with conn.execute(
        "SELECT id, user_id, telegram_user_id, chat_id, linked_at FROM telegram_accounts "
        "WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return TelegramAccountRecord(
        id=row[0], user_id=row[1], telegram_user_id=row[2], chat_id=row[3], linked_at=row[4]
    )


async def get_telegram_account_by_user_id(
    conn: aiosqlite.Connection, user_id: int
) -> TelegramAccountRecord | None:
    async with conn.execute(
        "SELECT id, user_id, telegram_user_id, chat_id, linked_at FROM telegram_accounts "
        "WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return TelegramAccountRecord(
        id=row[0], user_id=row[1], telegram_user_id=row[2], chat_id=row[3], linked_at=row[4]
    )
