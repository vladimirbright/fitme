"""Write-side access for `conversations` (ADR 0004) and the conversation link of
`chat_messages`. Timestamps from `fitme.clock` (A§4.2)."""

from __future__ import annotations

import aiosqlite

from fitme import clock


async def insert_conversation(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    kind: str,
    plan_id: int | None,
    workout_session_id: int | None,
) -> int:
    now = clock.utc_now()
    cursor = await conn.execute(
        "INSERT INTO conversations (user_id, kind, plan_id, workout_session_id, status, "
        "started_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (user_id, kind, plan_id, workout_session_id, now, now),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def set_conversation_draft(
    conn: aiosqlite.Connection, conversation_id: int, *, draft_decision_id: int | None
) -> None:
    await conn.execute(
        "UPDATE conversations SET draft_decision_id = ?, updated_at = ? WHERE id = ?",
        (draft_decision_id, clock.utc_now(), conversation_id),
    )


async def set_conversation_plan(
    conn: aiosqlite.Connection, conversation_id: int, *, plan_id: int | None
) -> None:
    await conn.execute(
        "UPDATE conversations SET plan_id = ?, updated_at = ? WHERE id = ?",
        (plan_id, clock.utc_now(), conversation_id),
    )


async def touch_conversation(conn: aiosqlite.Connection, conversation_id: int) -> None:
    await conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (clock.utc_now(), conversation_id)
    )


async def close_conversation(
    conn: aiosqlite.Connection, conversation_id: int, *, status: str
) -> None:
    """`status`: "saved" | "discarded" | "closed". Only an open session is closed."""
    now = clock.utc_now()
    await conn.execute(
        "UPDATE conversations SET status = ?, updated_at = ?, closed_at = ? "
        "WHERE id = ? AND status = 'open'",
        (status, now, now, conversation_id),
    )


async def link_chat_message(
    conn: aiosqlite.Connection, chat_message_id: int, *, conversation_id: int
) -> None:
    await conn.execute(
        "UPDATE chat_messages SET conversation_id = ? WHERE id = ?",
        (conversation_id, chat_message_id),
    )
