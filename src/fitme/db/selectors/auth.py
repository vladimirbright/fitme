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


_LOGIN_CODE_COLUMNS = "id, user_id, code_hash, expires_at, attempts, used_at, created_at"


def _login_code_from_row(row: aiosqlite.Row) -> LoginCodeRecord:
    return LoginCodeRecord(
        id=row[0],
        user_id=row[1],
        code_hash=row[2],
        expires_at=row[3],
        attempts=row[4],
        used_at=row[5],
        created_at=row[6],
    )


async def get_login_code_by_hash(
    conn: aiosqlite.Connection, code_hash: str
) -> LoginCodeRecord | None:
    async with conn.execute(
        f"SELECT {_LOGIN_CODE_COLUMNS} FROM login_codes WHERE code_hash = ?",
        (code_hash,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _login_code_from_row(row)


async def get_latest_unused_login_code(
    conn: aiosqlite.Connection, user_id: int
) -> LoginCodeRecord | None:
    """The newest not-yet-used login code for `user_id` (the M1 note, A§9.2: "look codes up
    by user_id + latest unused, not by hash alone"). `services.webauth.verify_login_code`
    compares the submitted code's hash against this one row with `hmac.compare_digest`,
    rather than an equality `WHERE code_hash = ?` lookup — the same reasoning as
    `list_pending_activation_codes` (A§6.1): a request-code always supersedes the previous
    one, so there is at most one "current" code to check per user at any time."""
    async with conn.execute(
        f"SELECT {_LOGIN_CODE_COLUMNS} FROM login_codes "
        "WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _login_code_from_row(row)


async def count_login_codes_since(conn: aiosqlite.Connection, user_id: int, since: str) -> int:
    """How many login codes were issued to `user_id` at or after `since` (A§9.1 rate limit:
    1 per 60s, 5 per hour), including already-used/expired ones — a spent code still counts
    against the rate limit, exactly like a Telegram message already sent."""
    async with conn.execute(
        "SELECT COUNT(*) FROM login_codes WHERE user_id = ? AND created_at >= ?",
        (user_id, since),
    ) as cursor:
        row = await cursor.fetchone()
    return 0 if row is None else int(row[0])


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
