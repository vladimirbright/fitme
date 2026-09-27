"""Write-side access for `activation_codes`, `login_codes` and `web_sessions` (A§4.1).

Codes and session ids are hashed by the caller (SHA-256) before being passed in here; this
module never sees or stores plaintext. `expires_at` is the one timestamp a caller controls
here — a TTL decided by the service layer, passed as a `datetime` and formatted internally
(A§4.2); everything else (`linked_at`, `used_at`, `created_at`, `last_seen_at`) defaults to
`clock.utc_now()`.
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from fitme import clock


async def insert_activation_code(
    conn: aiosqlite.Connection, *, code_hash: str, expires_at: datetime
) -> int:
    cursor = await conn.execute(
        "INSERT INTO activation_codes (code_hash, expires_at) VALUES (?, ?)",
        (code_hash, clock.format_timestamp(expires_at)),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def mark_activation_code_used(conn: aiosqlite.Connection, code_id: int) -> None:
    await conn.execute(
        "UPDATE activation_codes SET used_at = ? WHERE id = ?", (clock.utc_now(), code_id)
    )


async def insert_login_code(
    conn: aiosqlite.Connection, *, user_id: int, code_hash: str, expires_at: datetime
) -> int:
    cursor = await conn.execute(
        "INSERT INTO login_codes (user_id, code_hash, expires_at) VALUES (?, ?, ?)",
        (user_id, code_hash, clock.format_timestamp(expires_at)),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def increment_login_code_attempts(conn: aiosqlite.Connection, login_code_id: int) -> None:
    await conn.execute(
        "UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?", (login_code_id,)
    )


async def mark_login_code_used(conn: aiosqlite.Connection, login_code_id: int) -> None:
    await conn.execute(
        "UPDATE login_codes SET used_at = ? WHERE id = ?", (clock.utc_now(), login_code_id)
    )


async def insert_web_session(
    conn: aiosqlite.Connection, *, id_hash: str, user_id: int, expires_at: datetime
) -> None:
    now = clock.utc_now()
    await conn.execute(
        "INSERT INTO web_sessions (id_hash, user_id, created_at, last_seen_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (id_hash, user_id, now, now, clock.format_timestamp(expires_at)),
    )


async def touch_web_session(conn: aiosqlite.Connection, id_hash: str) -> None:
    await conn.execute(
        "UPDATE web_sessions SET last_seen_at = ? WHERE id_hash = ?", (clock.utc_now(), id_hash)
    )


async def delete_web_session(conn: aiosqlite.Connection, id_hash: str) -> None:
    await conn.execute("DELETE FROM web_sessions WHERE id_hash = ?", (id_hash,))
