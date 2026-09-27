"""B1: `Database` serializes transaction()/read() with one lock, so concurrent asyncio tasks
on the shared connection never race "cannot start a transaction within a transaction", and a
read can never observe another task's uncommitted writes."""

from __future__ import annotations

import asyncio

import pytest

from fitme.db.connection import Database
from fitme.db.controllers.chat import insert_chat_message
from fitme.db.selectors.chat import count_chat_messages_for_user, list_chat_messages_for_user


async def test_concurrent_transactions_one_fails_one_commits(db: Database, user_id: int) -> None:
    async def _failing_write() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with db.transaction() as conn:
                await insert_chat_message(
                    conn,
                    user_id=user_id,
                    session_id=None,
                    direction="in",
                    text="should-not-persist",
                )
                raise RuntimeError("boom")

    async def _succeeding_write() -> None:
        async with db.transaction() as conn:
            await insert_chat_message(
                conn, user_id=user_id, session_id=None, direction="in", text="should-persist"
            )

    # Both run "concurrently" on asyncio's event loop; the Database lock must fully
    # serialize them (one BEGIN...COMMIT/ROLLBACK completes before the other's BEGIN runs),
    # so neither raises "cannot start a transaction within a transaction".
    await asyncio.gather(_failing_write(), _succeeding_write())

    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
    texts = {m.text for m in messages}
    assert texts == {"should-persist"}


async def test_read_never_observes_an_uncommitted_write(db: Database, user_id: int) -> None:
    writer_has_inserted = asyncio.Event()
    writer_may_commit = asyncio.Event()

    async def _writer() -> None:
        async with db.transaction() as conn:
            await insert_chat_message(
                conn, user_id=user_id, session_id=None, direction="in", text="in-flight"
            )
            writer_has_inserted.set()
            await writer_may_commit.wait()
        # The transaction (and its COMMIT) has now completed.

    async def _reader() -> int:
        await writer_has_inserted.wait()
        async with db.read() as conn:
            return await count_chat_messages_for_user(conn, user_id)

    writer_task = asyncio.create_task(_writer())
    reader_task = asyncio.create_task(_reader())

    await writer_has_inserted.wait()
    # Let the reader run as far as it can; it must be blocked acquiring the Database lock
    # (the writer's transaction is still open), not actually inside db.read() yet.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not reader_task.done()

    writer_may_commit.set()
    await writer_task
    count = await reader_task

    # The reader was forced to wait for the writer's transaction to finish, so it only ever
    # sees the fully-committed state — never the row mid-transaction.
    assert count == 1
