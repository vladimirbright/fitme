"""The retention job (A§8.4).

Deletes `chat_messages` older than `FITME_CHAT_RETENTION_DAYS`, plus expired
`activation_codes`, `login_codes` and `web_sessions`. The structured training log
(`set_logs`) is never touched.
"""

from __future__ import annotations

from dataclasses import dataclass

from fitme import clock
from fitme.db.connection import Database
from fitme.db.controllers.retention import (
    purge_chat_messages_older_than,
    purge_expired_activation_codes,
    purge_expired_login_codes,
    purge_expired_web_sessions,
)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    chat_messages_deleted: int
    activation_codes_deleted: int
    login_codes_deleted: int
    web_sessions_deleted: int


async def purge(db: Database, *, chat_retention_days: int) -> RetentionResult:
    """Run retention once. Safe to call repeatedly (e.g. the daily background job, A§3)."""
    cutoff = clock.days_ago(chat_retention_days)
    async with db.transaction() as conn:
        chat_messages_deleted = await purge_chat_messages_older_than(conn, cutoff)
        activation_codes_deleted = await purge_expired_activation_codes(conn)
        login_codes_deleted = await purge_expired_login_codes(conn)
        web_sessions_deleted = await purge_expired_web_sessions(conn)
    return RetentionResult(
        chat_messages_deleted=chat_messages_deleted,
        activation_codes_deleted=activation_codes_deleted,
        login_codes_deleted=login_codes_deleted,
        web_sessions_deleted=web_sessions_deleted,
    )
