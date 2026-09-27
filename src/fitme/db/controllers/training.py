"""Write-side access for `workout_sessions`, `set_logs`, `checkins` and `health_holds`
(A§4.2). All timestamps default to `clock.utc_now()` internally (A§4.2); none of these
functions take a timestamp parameter.
"""

from __future__ import annotations

import aiosqlite

from fitme import clock


async def insert_workout_session(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    plan_version_id: int,
    workout_key: str,
    status: str,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
        "current_block) VALUES (?, ?, ?, ?, 0)",
        (user_id, plan_version_id, workout_key, status),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def update_workout_session_progress(
    conn: aiosqlite.Connection, session_id: int, *, status: str, current_block: int
) -> None:
    await conn.execute(
        "UPDATE workout_sessions SET status = ?, current_block = ? WHERE id = ?",
        (status, current_block, session_id),
    )


async def finish_workout_session(
    conn: aiosqlite.Connection, session_id: int, *, status: str, halt_reason: str | None = None
) -> None:
    await conn.execute(
        "UPDATE workout_sessions SET status = ?, finished_at = ?, halt_reason = ? WHERE id = ?",
        (status, clock.utc_now(), halt_reason, session_id),
    )


async def start_workout_session(conn: aiosqlite.Connection, session_id: int) -> None:
    await conn.execute(
        "UPDATE workout_sessions SET started_at = ? WHERE id = ?", (clock.utc_now(), session_id)
    )


async def insert_set_log(
    conn: aiosqlite.Connection,
    *,
    session_id: int,
    exercise_id: str,
    set_index: int,
    planned_load_kg: float | None,
    planned_reps_min: int | None,
    planned_reps_max: int | None,
    actual_load_kg: float | None,
    actual_reps: int | None,
    skipped: bool = False,
    rpe: float | None,
    source: str,
) -> int:
    """One row per *prescribed* set (A§4.2), created when the block is sent. A set the user
    didn't perform is logged with `skipped=True` and `actual_load_kg`/`actual_reps` left
    `None`, so a partial or halted session stays visible in the data (A§7.3)."""
    cursor = await conn.execute(
        "INSERT INTO set_logs (session_id, exercise_id, set_index, planned_load_kg, "
        "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, "
        "source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            exercise_id,
            set_index,
            planned_load_kg,
            planned_reps_min,
            planned_reps_max,
            actual_load_kg,
            actual_reps,
            int(skipped),
            rpe,
            source,
            clock.utc_now(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def insert_checkin(
    conn: aiosqlite.Connection, *, user_id: int, session_id: int | None, question_key: str
) -> int:
    """A check-in always starts as 'unknown' with no answered_at (AGENTS.md §2: silence is
    not consent); it is only ever updated by `answer_checkin`. There is deliberately no
    `answer` parameter here — the schema's CHECK also enforces this (`answer = 'unknown'`
    if and only if `answered_at IS NULL`)."""
    cursor = await conn.execute(
        "INSERT INTO checkins (user_id, session_id, question_key, answer, asked_at) "
        "VALUES (?, ?, ?, 'unknown', ?)",
        (user_id, session_id, question_key, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def answer_checkin(conn: aiosqlite.Connection, checkin_id: int, *, answer: str) -> None:
    """`answer` must be an explicit answer ('fine'/'worse'/'pain'), not 'unknown' — the
    schema's CHECK rejects 'unknown' paired with a non-NULL answered_at."""
    await conn.execute(
        "UPDATE checkins SET answer = ?, answered_at = ? WHERE id = ?",
        (answer, clock.utc_now(), checkin_id),
    )


async def insert_health_hold(
    conn: aiosqlite.Connection, *, user_id: int, reason: str, source_session_id: int | None
) -> int:
    cursor = await conn.execute(
        "INSERT INTO health_holds (user_id, reason, source_session_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, reason, source_session_id, clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def clear_health_hold(conn: aiosqlite.Connection, hold_id: int) -> None:
    await conn.execute(
        "UPDATE health_holds SET cleared_at = ? WHERE id = ?", (clock.utc_now(), hold_id)
    )
