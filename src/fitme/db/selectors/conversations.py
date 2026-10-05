"""Read-side access for `conversations` and a conversation's messages (ADR 0004)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import ChatMessageRecord, ConversationRecord

_COLUMNS = (
    "id, user_id, kind, plan_id, workout_session_id, draft_decision_id, status, started_at, "
    "updated_at, closed_at"
)


def _from_row(row: aiosqlite.Row) -> ConversationRecord:
    return ConversationRecord(
        id=row[0],
        user_id=row[1],
        kind=row[2],
        plan_id=row[3],
        workout_session_id=row[4],
        draft_decision_id=row[5],
        status=row[6],
        started_at=row[7],
        updated_at=row[8],
        closed_at=row[9],
    )


async def list_open_conversations(
    conn: aiosqlite.Connection, user_id: int
) -> list[ConversationRecord]:
    """Open sessions, newest first."""
    async with conn.execute(
        f"SELECT {_COLUMNS} FROM conversations WHERE user_id = ? AND status = 'open' "
        "ORDER BY id DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_from_row(row) for row in rows]


async def get_conversation(
    conn: aiosqlite.Connection, conversation_id: int
) -> ConversationRecord | None:
    async with conn.execute(
        f"SELECT {_COLUMNS} FROM conversations WHERE id = ?", (conversation_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _from_row(row)


async def list_conversation_messages(
    conn: aiosqlite.Connection, conversation_id: int, *, limit: int
) -> list[ChatMessageRecord]:
    """The last `limit` messages of a session, oldest first."""
    async with conn.execute(
        "SELECT id, user_id, session_id, direction, text, created_at FROM chat_messages "
        "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
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
        for row in reversed(list(rows))
    ]
