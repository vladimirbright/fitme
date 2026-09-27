"""Write-side access for `users` and `telegram_accounts` (A§4.1).

"At most one user" is enforced two ways: the `users.id` CHECK (id = 1) in the schema, and
`insert_user` checking for an existing row before it even attempts the insert (belt and
braces, per docs/IMPLEMENTATION_PLAN.md M1). The check-then-insert is race-free because
`Database.transaction()` holds one lock for its whole body (A§4.6, A§3): no other coroutine
can insert a user in between the check and the insert.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

from fitme import clock

_SINGLE_USER_ID = 1
_SECOND_USER_MESSAGE = "a user row already exists; this instance is single-user"


class SecondUserError(RuntimeError):
    """Raised when a second user row is attempted. This instance is single-user (ADR 0002)."""


async def insert_user(conn: aiosqlite.Connection, *, language: str, timezone: str | None) -> int:
    """Create the one user row. Raises SecondUserError if a user already exists."""
    async with conn.execute("SELECT COUNT(*) FROM users") as cursor:
        row = await cursor.fetchone()
    if row is not None and row[0] > 0:
        raise SecondUserError(_SECOND_USER_MESSAGE)

    try:
        await conn.execute(
            "INSERT INTO users (id, language, timezone, created_at) VALUES (?, ?, ?, ?)",
            (_SINGLE_USER_ID, language, timezone, clock.utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        # Only the id/CHECK violation this table can produce means "a second user"; any
        # other integrity error (e.g. a future NOT NULL column) should surface as itself,
        # not be misreported as SecondUserError.
        message = str(exc)
        if "users.id" in message or "CHECK constraint failed" in message:
            raise SecondUserError(_SECOND_USER_MESSAGE) from exc
        raise
    return _SINGLE_USER_ID


async def update_user_language(conn: aiosqlite.Connection, user_id: int, language: str) -> None:
    await conn.execute("UPDATE users SET language = ? WHERE id = ?", (language, user_id))


async def update_user_timezone(conn: aiosqlite.Connection, user_id: int, timezone: str) -> None:
    await conn.execute("UPDATE users SET timezone = ? WHERE id = ?", (timezone, user_id))


async def insert_telegram_account(
    conn: aiosqlite.Connection, *, user_id: int, telegram_user_id: int, chat_id: int
) -> int:
    cursor = await conn.execute(
        "INSERT INTO telegram_accounts (user_id, telegram_user_id, chat_id, linked_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, telegram_user_id, chat_id, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def delete_telegram_account(conn: aiosqlite.Connection, user_id: int) -> None:
    """Unlink the current Telegram account, e.g. for `fitme activate --rebind`."""
    await conn.execute("DELETE FROM telegram_accounts WHERE user_id = ?", (user_id,))
