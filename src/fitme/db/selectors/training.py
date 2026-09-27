"""Read-side access for `workout_sessions`, `set_logs`, `checkins` and `health_holds`
(A§4.2)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import CheckinRecord, HealthHoldRecord, SetLogRecord, WorkoutSessionRecord

_SESSION_COLUMNS = (
    "id, user_id, plan_version_id, workout_key, status, current_block, started_at, "
    "finished_at, halt_reason"
)


def _session_from_row(row: aiosqlite.Row) -> WorkoutSessionRecord:
    return WorkoutSessionRecord(
        id=row[0],
        user_id=row[1],
        plan_version_id=row[2],
        workout_key=row[3],
        status=row[4],
        current_block=row[5],
        started_at=row[6],
        finished_at=row[7],
        halt_reason=row[8],
    )


async def get_workout_session(
    conn: aiosqlite.Connection, session_id: int
) -> WorkoutSessionRecord | None:
    async with conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM workout_sessions WHERE id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _session_from_row(row)


async def list_workout_sessions_for_user(
    conn: aiosqlite.Connection, user_id: int
) -> list[WorkoutSessionRecord]:
    async with conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM workout_sessions WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_session_from_row(row) for row in rows]


async def list_set_logs_for_session(
    conn: aiosqlite.Connection, session_id: int
) -> list[SetLogRecord]:
    async with conn.execute(
        "SELECT id, session_id, exercise_id, set_index, planned_load_kg, planned_reps, "
        "actual_load_kg, actual_reps, rpe, source, created_at FROM set_logs "
        "WHERE session_id = ? ORDER BY set_index",
        (session_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        SetLogRecord(
            id=row[0],
            session_id=row[1],
            exercise_id=row[2],
            set_index=row[3],
            planned_load_kg=row[4],
            planned_reps=row[5],
            actual_load_kg=row[6],
            actual_reps=row[7],
            rpe=row[8],
            source=row[9],
            created_at=row[10],
        )
        for row in rows
    ]


async def historical_max_kg(
    conn: aiosqlite.Connection, user_id: int, exercise_id: str
) -> float | None:
    """The highest logged `actual_load_kg` for this exercise across this user's sessions
    (A§4.6 example; used by the load engine's ceiling guard, A§7.3)."""
    async with conn.execute(
        "SELECT MAX(s.actual_load_kg) FROM set_logs s "
        "JOIN workout_sessions w ON w.id = s.session_id "
        "WHERE w.user_id = ? AND s.exercise_id = ? AND s.actual_load_kg IS NOT NULL",
        (user_id, exercise_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return None if row[0] is None else float(row[0])


async def list_open_health_holds(
    conn: aiosqlite.Connection, user_id: int
) -> list[HealthHoldRecord]:
    async with conn.execute(
        "SELECT id, user_id, reason, source_session_id, created_at, cleared_at "
        "FROM health_holds WHERE user_id = ? AND cleared_at IS NULL",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        HealthHoldRecord(
            id=row[0],
            user_id=row[1],
            reason=row[2],
            source_session_id=row[3],
            created_at=row[4],
            cleared_at=row[5],
        )
        for row in rows
    ]


async def get_checkin(conn: aiosqlite.Connection, checkin_id: int) -> CheckinRecord | None:
    async with conn.execute(
        "SELECT id, user_id, session_id, question_key, answer, asked_at, answered_at "
        "FROM checkins WHERE id = ?",
        (checkin_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return CheckinRecord(
        id=row[0],
        user_id=row[1],
        session_id=row[2],
        question_key=row[3],
        answer=row[4],
        asked_at=row[5],
        answered_at=row[6],
    )


async def list_checkins_for_session(
    conn: aiosqlite.Connection, session_id: int
) -> list[CheckinRecord]:
    async with conn.execute(
        "SELECT id, user_id, session_id, question_key, answer, asked_at, answered_at "
        "FROM checkins WHERE session_id = ? ORDER BY asked_at",
        (session_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        CheckinRecord(
            id=row[0],
            user_id=row[1],
            session_id=row[2],
            question_key=row[3],
            answer=row[4],
            asked_at=row[5],
            answered_at=row[6],
        )
        for row in rows
    ]
