"""controllers/selectors for `chat_messages` (A§4.2)."""

from __future__ import annotations

from fitme.db.connection import Database
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.selectors.chat import count_chat_messages_for_user, list_chat_messages_for_user


async def test_insert_and_list_chat_messages(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await insert_chat_message(
            conn, user_id=user_id, session_id=None, direction="in", text="hello"
        )
        await insert_chat_message(
            conn, user_id=user_id, session_id=None, direction="out", text="hi there"
        )

    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
        count = await count_chat_messages_for_user(conn, user_id)

    assert count == 2
    assert len(messages) == 2
    assert {m.direction for m in messages} == {"in", "out"}
