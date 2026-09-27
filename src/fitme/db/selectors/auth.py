"""Read-side access for `activation_codes`, `login_codes` and `web_sessions` (A§4.1)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import ActivationCodeRecord, LoginCodeRecord, WebSessionRecord


async def get_activation_code_by_hash(
    conn: aiosqlite.Connection, code_hash: str
) -> ActivationCodeRecord | None:
    async with conn.execute(
        "SELECT id, code_hash, expires_at, used_at FROM activation_codes WHERE code_hash = ?",
        (code_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return ActivationCodeRecord(id=row[0], code_hash=row[1], expires_at=row[2], used_at=row[3])


async def list_pending_activation_codes(conn: aiosqlite.Connection) -> list[ActivationCodeRecord]:
    """Every activation code not yet used (expired or not). Used for a constant-time compare
    against a submitted `/activate` code (A§6.1): comparing against every pending hash with
    `hmac.compare_digest`, rather than an equality `WHERE code_hash = ?` lookup, avoids a
    timing side channel on which prefix of the hash matched."""
    async with conn.execute(
        "SELECT id, code_hash, expires_at, used_at FROM activation_codes WHERE used_at IS NULL"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        ActivationCodeRecord(id=row[0], code_hash=row[1], expires_at=row[2], used_at=row[3])
        for row in rows
    ]


async def get_activation_failed_attempts(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT failed_attempts FROM activation_state WHERE id = 1") as cursor:
        row = await cursor.fetchone()
    return 0 if row is None else int(row[0])


async def get_login_code_by_hash(
    conn: aiosqlite.Connection, code_hash: str
) -> LoginCodeRecord | None:
    async with conn.execute(
        "SELECT id, user_id, code_hash, expires_at, attempts, used_at FROM login_codes "
        "WHERE code_hash = ?",
        (code_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return LoginCodeRecord(
        id=row[0],
        user_id=row[1],
        code_hash=row[2],
        expires_at=row[3],
        attempts=row[4],
        used_at=row[5],
    )


async def get_web_session(conn: aiosqlite.Connection, id_hash: str) -> WebSessionRecord | None:
    async with conn.execute(
        "SELECT id_hash, user_id, created_at, last_seen_at, expires_at FROM web_sessions "
        "WHERE id_hash = ?",
        (id_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return WebSessionRecord(
        id_hash=row[0], user_id=row[1], created_at=row[2], last_seen_at=row[3], expires_at=row[4]
    )


async def list_web_sessions_for_user(
    conn: aiosqlite.Connection, user_id: int
) -> list[WebSessionRecord]:
    async with conn.execute(
        "SELECT id_hash, user_id, created_at, last_seen_at, expires_at FROM web_sessions "
        "WHERE user_id = ? ORDER BY last_seen_at DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        WebSessionRecord(
            id_hash=row[0],
            user_id=row[1],
            created_at=row[2],
            last_seen_at=row[3],
            expires_at=row[4],
        )
        for row in rows
    ]
