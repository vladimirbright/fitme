"""Write-side access for `profiles`, `screening_flags` and `screening_notes` (A§4.2).

JSON columns (`preferences`, `equipment`) are serialized here; callers pass plain
`list[str]`, never a JSON string. `updated_at`/`answered_at`/`created_at` default to
`clock.utc_now()` internally; `completed_at` is the one caller-controlled timestamp here —
the moment the whole questionnaire was finished — passed as a `datetime | None` and
formatted internally (A§4.2).
"""

from __future__ import annotations

import json
from datetime import datetime

import aiosqlite

from fitme import clock


async def upsert_profile(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    age_bucket: str | None,
    weight_bucket: str | None,
    experience: str | None,
    barbell_experience: str | None,
    preferences: list[str],
    location: str | None,
    equipment: list[str],
    sessions_per_week: int | None,
    session_minutes: int | None,
    focus: str | None,
    completed_at: datetime | None,
) -> None:
    """Create or replace the user's profile row. Setup saves answers step by step (A§5), so
    this is called repeatedly with an increasingly complete set of fields."""
    await conn.execute(
        """
        INSERT INTO profiles (
            user_id, age_bucket, weight_bucket, experience, barbell_experience,
            preferences, location, equipment, sessions_per_week, session_minutes,
            focus, completed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            age_bucket = excluded.age_bucket,
            weight_bucket = excluded.weight_bucket,
            experience = excluded.experience,
            barbell_experience = excluded.barbell_experience,
            preferences = excluded.preferences,
            location = excluded.location,
            equipment = excluded.equipment,
            sessions_per_week = excluded.sessions_per_week,
            session_minutes = excluded.session_minutes,
            focus = excluded.focus,
            completed_at = excluded.completed_at,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            age_bucket,
            weight_bucket,
            experience,
            barbell_experience,
            json.dumps(preferences),
            location,
            json.dumps(equipment),
            sessions_per_week,
            session_minutes,
            focus,
            None if completed_at is None else clock.format_timestamp(completed_at),
            clock.utc_now(),
        ),
    )


async def upsert_screening_flag(
    conn: aiosqlite.Connection, *, user_id: int, flag: str, value: str, clearance: str | None
) -> None:
    """One row per (user_id, flag); re-answering the same flag replaces the row (A§4.2)."""
    await conn.execute(
        """
        INSERT INTO screening_flags (user_id, flag, value, clearance, answered_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id, flag) DO UPDATE SET
            value = excluded.value,
            clearance = excluded.clearance,
            answered_at = excluded.answered_at
        """,
        (user_id, flag, value, clearance, clock.utc_now()),
    )


async def insert_screening_note(conn: aiosqlite.Connection, *, user_id: int, text: str) -> int:
    cursor = await conn.execute(
        "INSERT INTO screening_notes (user_id, text, created_at) VALUES (?, ?, ?)",
        (user_id, text, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def upsert_setup_progress(
    conn: aiosqlite.Connection, *, user_id: int, step: str, data: dict[str, object]
) -> None:
    """Durable marker for where the setup questionnaire is (A§5.1, M5): so a bot restart
    mid-setup resumes at the same step instead of relying on in-memory FSM state."""
    await conn.execute(
        """
        INSERT INTO setup_progress (user_id, step, data, updated_at) VALUES (?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            step = excluded.step,
            data = excluded.data,
            updated_at = excluded.updated_at
        """,
        (user_id, step, json.dumps(data), clock.utc_now()),
    )


async def delete_setup_progress(conn: aiosqlite.Connection, user_id: int) -> None:
    """Called once the questionnaire is confirmed: there is nothing left to resume."""
    await conn.execute("DELETE FROM setup_progress WHERE user_id = ?", (user_id,))
