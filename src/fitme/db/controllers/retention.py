"""The retention job's deletes (A§8.4): old `chat_messages`, and expired codes/sessions.

`set_logs` (the structured training log) is never touched here; only `chat_messages` (raw
text) and expired auth artifacts are purged. `cutoff` is the one caller-controlled timestamp
(the retention boundary, which depends on `FITME_CHAT_RETENTION_DAYS`); "now", for comparing
against `expires_at`, is generated internally (A§4.2).
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from fitme import clock


async def purge_chat_messages_older_than(conn: aiosqlite.Connection, cutoff: datetime) -> int:
    cursor = await conn.execute(
        "DELETE FROM chat_messages WHERE created_at < ?", (clock.format_timestamp(cutoff),)
    )
    return cursor.rowcount


async def purge_conversations_closed_before(conn: aiosqlite.Connection, cutoff: datetime) -> int:
    """ADR 0004: a closed session older than the chat retention has no messages left (they
    were purged by `purge_chat_messages_older_than`), so the empty shell goes too."""
    cursor = await conn.execute(
        "DELETE FROM conversations WHERE status != 'open' AND closed_at < ?",
        (clock.format_timestamp(cutoff),),
    )
    return cursor.rowcount


async def purge_expired_activation_codes(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "DELETE FROM activation_codes WHERE expires_at < ? OR used_at IS NOT NULL",
        (clock.utc_now(),),
    )
    return cursor.rowcount


async def purge_expired_login_codes(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "DELETE FROM login_codes WHERE expires_at < ? OR used_at IS NOT NULL",
        (clock.utc_now(),),
    )
    return cursor.rowcount


async def purge_expired_web_sessions(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (clock.utc_now(),))
    return cursor.rowcount
