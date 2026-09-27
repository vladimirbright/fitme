"""Read-side access for `chat_messages` (A§4.2)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import ChatMessageRecord


async def list_chat_messages_for_user(
    conn: aiosqlite.Connection, user_id: int, *, limit: int = 100
) -> list[ChatMessageRecord]:
    async with conn.execute(
        "SELECT id, user_id, session_id, direction, text, created_at FROM chat_messages "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        ChatMessageRecord(
            id=row[0],
            user_id=row[1],
            session_id=row[2],
            direction=row[3],
            text=row[4],
            created_at=row[5],
        )
        for row in rows
    ]


async def count_chat_messages_for_user(conn: aiosqlite.Connection, user_id: int) -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])
