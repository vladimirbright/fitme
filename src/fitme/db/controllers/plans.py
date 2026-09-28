"""Write-side access for `plans` and `plan_versions` (A§4.3).

`plan_versions` is append-only (A§4.6 rule 5): this module has no update/delete for it.
Timestamps default to `clock.utc_now()` internally (A§4.2).
"""

from __future__ import annotations

import json

import aiosqlite

from fitme import clock


class PlanNotOwnedError(RuntimeError):
    """Raised when a plan operation targets a plan_id that isn't this user's own plan."""


async def insert_plan(
    conn: aiosqlite.Connection, *, user_id: int, name: str, is_default: bool, status: str
) -> int:
    cursor = await conn.execute(
        "INSERT INTO plans (user_id, name, is_default, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, int(is_default), status, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def set_default_plan(conn: aiosqlite.Connection, user_id: int, plan_id: int) -> None:
    """Make `plan_id` the default and every other plan of this user's not-default.

    Verifies ownership first with a plain SELECT (no write), so a plan_id that isn't this
    user's own raises `PlanNotOwnedError` without touching any row — in particular, without
    clearing the user's existing default.
    """
    async with conn.execute(
        "SELECT 1 FROM plans WHERE id = ? AND user_id = ?", (plan_id, user_id)
    ) as cursor:
        owned = await cursor.fetchone()
    if owned is None:
        raise PlanNotOwnedError(f"plan {plan_id} does not belong to user {user_id}")

    # Clear the old default before setting the new one: idx_plans_one_default_per_user
    # allows only one is_default = 1 row per user at a time.
    await conn.execute(
        "UPDATE plans SET is_default = 0 WHERE user_id = ? AND id != ?", (user_id, plan_id)
    )
    cursor = await conn.execute(
        "UPDATE plans SET is_default = 1 WHERE id = ? AND user_id = ?", (plan_id, user_id)
    )
    assert cursor.rowcount == 1  # guaranteed by the ownership check above


async def rename_plan(conn: aiosqlite.Connection, user_id: int, plan_id: int, name: str) -> None:
    """Set `plans.name` (a plain column, not append-only: the stored versions keep their own
    body `name`). `name` is already validated by `services.planning.rename_plan` (trimmed,
    1–60 characters, wording-checked). A plan_id that isn't this user's own raises
    `PlanNotOwnedError` and changes nothing."""
    cursor = await conn.execute(
        "UPDATE plans SET name = ? WHERE id = ? AND user_id = ?", (name, plan_id, user_id)
    )
    if cursor.rowcount != 1:
        raise PlanNotOwnedError(f"plan {plan_id} does not belong to user {user_id}")


async def insert_plan_version(
    conn: aiosqlite.Connection,
    *,
    plan_id: int,
    version: int,
    body: dict[str, object],
    origin: str,
    decision_id: int,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO plan_versions (plan_id, version, body, origin, decision_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_id, version, json.dumps(body), origin, decision_id, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
