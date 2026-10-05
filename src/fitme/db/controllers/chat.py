"""Write-side access for `chat_messages` (A§4.2). Raw text, purged by the retention job.
`created_at` defaults to `clock.utc_now()` internally (A§4.2)."""

from __future__ import annotations

import aiosqlite

from fitme import clock


async def insert_chat_message(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    session_id: int | None,
    direction: str,
    text: str,
    conversation_id: int | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO chat_messages (user_id, session_id, direction, text, created_at, "
        "conversation_id) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, session_id, direction, text, clock.utc_now(), conversation_id),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
