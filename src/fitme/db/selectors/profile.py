"""Read-side access for `profiles`, `screening_flags` and `screening_notes` (A§4.2)."""

from __future__ import annotations

import json

import aiosqlite

from fitme.db.records import ProfileRecord, ScreeningFlagRecord, ScreeningNoteRecord

_PROFILE_COLUMNS = (
    "user_id, age_bucket, weight_bucket, experience, barbell_experience, preferences, "
    "location, equipment, sessions_per_week, session_minutes, focus, completed_at, updated_at"
)


def _profile_from_row(row: aiosqlite.Row) -> ProfileRecord:
    return ProfileRecord(
        user_id=row[0],
        age_bucket=row[1],
        weight_bucket=row[2],
        experience=row[3],
        barbell_experience=row[4],
        preferences=json.loads(row[5]) if row[5] is not None else [],
        location=row[6],
        equipment=json.loads(row[7]) if row[7] is not None else [],
        sessions_per_week=row[8],
        session_minutes=row[9],
        focus=row[10],
        completed_at=row[11],
        updated_at=row[12],
    )


async def get_profile(conn: aiosqlite.Connection, user_id: int) -> ProfileRecord | None:
    async with conn.execute(
        f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return _profile_from_row(row)


async def list_screening_flags(
    conn: aiosqlite.Connection, user_id: int
) -> list[ScreeningFlagRecord]:
    async with conn.execute(
        "SELECT id, user_id, flag, value, clearance, answered_at FROM screening_flags "
        "WHERE user_id = ? ORDER BY flag",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        ScreeningFlagRecord(
            id=row[0],
            user_id=row[1],
            flag=row[2],
            value=row[3],
            clearance=row[4],
            answered_at=row[5],
        )
        for row in rows
    ]


async def get_screening_flag(
    conn: aiosqlite.Connection, user_id: int, flag: str
) -> ScreeningFlagRecord | None:
    async with conn.execute(
        "SELECT id, user_id, flag, value, clearance, answered_at FROM screening_flags "
        "WHERE user_id = ? AND flag = ?",
        (user_id, flag),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return ScreeningFlagRecord(
        id=row[0], user_id=row[1], flag=row[2], value=row[3], clearance=row[4], answered_at=row[5]
    )


async def list_screening_notes(
    conn: aiosqlite.Connection, user_id: int
) -> list[ScreeningNoteRecord]:
    async with conn.execute(
        "SELECT id, user_id, text, created_at FROM screening_notes WHERE user_id = ? "
        "ORDER BY created_at",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        ScreeningNoteRecord(id=row[0], user_id=row[1], text=row[2], created_at=row[3])
        for row in rows
    ]
