"""Read-side access for `plans` and `plan_versions` (A§4.3)."""

from __future__ import annotations

import json

import aiosqlite

from fitme.db.records import PlanRecord, PlanVersionRecord

_PLAN_COLUMNS = "id, user_id, name, is_default, status, created_at"
_PLAN_VERSION_COLUMNS = "id, plan_id, version, body, origin, decision_id, created_at"


def _plan_from_row(row: aiosqlite.Row) -> PlanRecord:
    return PlanRecord(
        id=row[0],
        user_id=row[1],
        name=row[2],
        is_default=bool(row[3]),
        status=row[4],
        created_at=row[5],
    )


def _plan_version_from_row(row: aiosqlite.Row) -> PlanVersionRecord:
    return PlanVersionRecord(
        id=row[0],
        plan_id=row[1],
        version=row[2],
        body=json.loads(row[3]),
        origin=row[4],
        decision_id=row[5],
        created_at=row[6],
    )


async def get_plan(conn: aiosqlite.Connection, plan_id: int) -> PlanRecord | None:
    async with conn.execute(
        f"SELECT {_PLAN_COLUMNS} FROM plans WHERE id = ?", (plan_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _plan_from_row(row)


async def list_plans_for_user(conn: aiosqlite.Connection, user_id: int) -> list[PlanRecord]:
    async with conn.execute(
        f"SELECT {_PLAN_COLUMNS} FROM plans WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_plan_from_row(row) for row in rows]


async def get_default_plan(conn: aiosqlite.Connection, user_id: int) -> PlanRecord | None:
    async with conn.execute(
        f"SELECT {_PLAN_COLUMNS} FROM plans WHERE user_id = ? AND is_default = 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _plan_from_row(row)


async def get_plan_version(
    conn: aiosqlite.Connection, plan_version_id: int
) -> PlanVersionRecord | None:
    async with conn.execute(
        f"SELECT {_PLAN_VERSION_COLUMNS} FROM plan_versions WHERE id = ?", (plan_version_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _plan_version_from_row(row)


async def get_latest_plan_version(
    conn: aiosqlite.Connection, plan_id: int
) -> PlanVersionRecord | None:
    async with conn.execute(
        f"SELECT {_PLAN_VERSION_COLUMNS} FROM plan_versions WHERE plan_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (plan_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _plan_version_from_row(row)


async def get_history_import_holder_version(
    conn: aiosqlite.Connection, user_id: int
) -> PlanVersionRecord | None:
    """The plan version imported training sessions are attached to (M11): `workout_sessions.
    plan_version_id` is NOT NULL, so `fitme history import` keeps one archived "Imported
    history" plan with a single empty workout for them. It is found through the record that
    created it — the `history_import` decision whose outcome names it as
    `holder_plan_version_id` — never through the plan's display name or body, which are
    user-visible copy. The earliest one wins, so every later import reuses it; `None` before
    the first import."""
    async with conn.execute(
        "SELECT v.id, v.plan_id, v.version, v.body, v.origin, v.decision_id, v.created_at "
        "FROM plan_versions v "
        "JOIN plans p ON p.id = v.plan_id "
        "JOIN decisions d ON d.id = v.decision_id "
        "JOIN decision_outcomes o ON o.decision_id = d.id "
        "WHERE p.user_id = ? AND d.kind = 'history_import' "
        "AND json_extract(o.outcome, '$.holder_plan_version_id') = v.id "
        "ORDER BY v.id LIMIT 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _plan_version_from_row(row)


async def list_plan_version_bodies(
    conn: aiosqlite.Connection, user_id: int, *, origin: str
) -> list[dict[str, object]]:
    """Every stored plan body of this user's versions with the given `origin` (M11:
    `services.history_import` compares an imported `[[plan]]` against the `import`-origin
    bodies already saved, so re-importing the same file saves no second copy)."""
    async with conn.execute(
        "SELECT v.body FROM plan_versions v JOIN plans p ON p.id = v.plan_id "
        "WHERE p.user_id = ? AND v.origin = ? ORDER BY v.id",
        (user_id, origin),
    ) as cursor:
        rows = await cursor.fetchall()
    bodies: list[dict[str, object]] = []
    for row in rows:
        body = json.loads(row[0])
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


async def list_plan_versions(conn: aiosqlite.Connection, plan_id: int) -> list[PlanVersionRecord]:
    async with conn.execute(
        f"SELECT {_PLAN_VERSION_COLUMNS} FROM plan_versions WHERE plan_id = ? ORDER BY version",
        (plan_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_plan_version_from_row(row) for row in rows]
