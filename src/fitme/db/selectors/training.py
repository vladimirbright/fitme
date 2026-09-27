"""Read-side access for `workout_sessions`, `set_logs`, `checkins` and `health_holds`
(A§4.2)."""

from __future__ import annotations

from collections.abc import Iterable

import aiosqlite

from fitme.db.records import (
    CheckinRecord,
    HealthHoldRecord,
    SessionOutcome,
    SetLogRecord,
    WorkoutSessionRecord,
)
from fitme.domain.enums import CheckinAnswer

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


async def get_active_workout_session(
    conn: aiosqlite.Connection, user_id: int
) -> WorkoutSessionRecord | None:
    """The user's one unfinished session (A§6.5: `draft` = precheck pending, `confirmed` =
    in review, `in_progress` = training), newest first if several ever exist. `None` when
    every session is completed, aborted or halted."""
    async with conn.execute(
        f"SELECT {_SESSION_COLUMNS} FROM workout_sessions WHERE user_id = ? "
        "AND status IN ('draft', 'confirmed', 'in_progress') ORDER BY id DESC LIMIT 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _session_from_row(row)


async def last_completed_workout_key_for_plan(
    conn: aiosqlite.Connection, user_id: int, plan_id: int
) -> str | None:
    """The `workout_key` of the user's most recent `completed` session on any version of
    `plan_id` (A§6.5 step 2: "the next one in rotation after the last completed one"), or
    `None` if the plan has no completed session yet."""
    async with conn.execute(
        "SELECT w.workout_key FROM workout_sessions w "
        "JOIN plan_versions v ON v.id = w.plan_version_id "
        "WHERE w.user_id = ? AND v.plan_id = ? AND w.status = 'completed' "
        "ORDER BY w.id DESC LIMIT 1",
        (user_id, plan_id),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else str(row[0])


async def list_set_logs_for_session(
    conn: aiosqlite.Connection, session_id: int
) -> list[SetLogRecord]:
    """Every set row of a session in creation order (blocks are sent, and their rows
    created, in workout order, A§4.2), which also orders sets within a prescription by
    `set_index`."""
    async with conn.execute(
        "SELECT id, session_id, exercise_id, set_index, planned_load_kg, planned_reps_min, "
        "planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, source, created_at "
        "FROM set_logs WHERE session_id = ? ORDER BY id",
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
            planned_reps_min=row[5],
            planned_reps_max=row[6],
            actual_load_kg=row[7],
            actual_reps=row[8],
            skipped=bool(row[9]),
            rpe=row[10],
            source=row[11],
            created_at=row[12],
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


async def historical_max_by_exercise(conn: aiosqlite.Connection, user_id: int) -> dict[str, float]:
    """`historical_max_kg` for every exercise this user has ever logged a kg load for, in one
    query (the plan flow, A§6.4, needs the whole map to build a `GuardContext`, not one
    exercise at a time). An exercise with no logged kg load has no entry."""
    async with conn.execute(
        "SELECT s.exercise_id, MAX(s.actual_load_kg) FROM set_logs s "
        "JOIN workout_sessions w ON w.id = s.session_id "
        "WHERE w.user_id = ? AND s.actual_load_kg IS NOT NULL GROUP BY s.exercise_id",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0]: float(row[1]) for row in rows if row[1] is not None}


async def historical_max_by_exercise_before_session(
    conn: aiosqlite.Connection, user_id: int, session_id: int
) -> dict[str, float]:
    """`historical_max_by_exercise` over every session *except* `session_id`: what the max
    was before this session, so the recap (A§6.5 step 6, M8) can say whether it set a new
    one."""
    async with conn.execute(
        "SELECT s.exercise_id, MAX(s.actual_load_kg) FROM set_logs s "
        "JOIN workout_sessions w ON w.id = s.session_id "
        "WHERE w.user_id = ? AND s.session_id != ? AND s.actual_load_kg IS NOT NULL "
        "GROUP BY s.exercise_id",
        (user_id, session_id),
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0]: float(row[1]) for row in rows if row[1] is not None}


async def recent_session_outcomes(
    conn: aiosqlite.Connection, user_id: int, exercise_id: str, *, limit: int = 2
) -> list[SessionOutcome]:
    """The most recent `limit` **completed** sessions' outcomes for this exercise (A§7.3
    double progression), most recent first.

    Only `workout_sessions.status = 'completed'` sessions count at all (B3/A§7.3): a halted,
    aborted or still-`in_progress` session's rows are excluded outright, as if that session
    hadn't happened for progression purposes. Within a completed session:

    - `hit_reps_max` (success) requires every prescribed set to have been *performed* (not
      `skipped`, with a logged `actual_reps`) at or above the prescribed load, with
      `actual_reps >= planned_reps_max`. A skipped set or one missing its actual result means
      the session can't be a success — that's the only way to keep a partial or
      under-reported session from producing an increase.
    - `below_reps_min` is set when the session isn't a success and at least one *performed*
      set fell below `planned_reps_min`.
    - A skipped/unlogged set contributes to neither flag on its own; it just prevents
      `hit_reps_max`.
    """
    async with conn.execute(
        "SELECT w.id, s.actual_load_kg, s.actual_reps, s.planned_load_kg, "
        "s.planned_reps_min, s.planned_reps_max, s.skipped "
        "FROM set_logs s JOIN workout_sessions w ON w.id = s.session_id "
        "WHERE w.user_id = ? AND s.exercise_id = ? AND w.status = 'completed' "
        "ORDER BY w.id DESC, s.set_index ASC",
        (user_id, exercise_id),
    ) as cursor:
        rows = await cursor.fetchall()

    return _group_outcomes(rows, limit=limit)


def _group_outcomes(rows: Iterable[aiosqlite.Row], *, limit: int) -> list[SessionOutcome]:
    """`rows` (one per set, most recent session first, sets in order) -> at most `limit`
    `SessionOutcome`s, most recent first."""
    session_ids_in_order: list[int] = []
    sets_by_session: dict[int, list[aiosqlite.Row]] = {}
    for row in rows:
        session_id = row[0]
        if session_id not in sets_by_session:
            if len(session_ids_in_order) >= limit:
                continue
            session_ids_in_order.append(session_id)
            sets_by_session[session_id] = []
        sets_by_session[session_id].append(row)

    outcomes: list[SessionOutcome] = []
    for session_id in session_ids_in_order:
        outcomes.append(_session_outcome_from_set_rows(sets_by_session[session_id]))
    return outcomes


async def recent_session_outcomes_by_exercise(
    conn: aiosqlite.Connection, user_id: int, *, limit: int = 2
) -> dict[str, list[SessionOutcome]]:
    """`recent_session_outcomes` for every exercise this user has a completed session for,
    in one query: the plan flow (A§6.4) runs the load engine (A§7.3) for any exercise the LLM
    proposes, so it needs every exercise's history up front rather than one query per
    exercise. Same derivation rules as `recent_session_outcomes`; an exercise with no
    completed session has no entry."""
    async with conn.execute(
        "SELECT w.id, s.actual_load_kg, s.actual_reps, s.planned_load_kg, "
        "s.planned_reps_min, s.planned_reps_max, s.skipped, s.exercise_id "
        "FROM set_logs s JOIN workout_sessions w ON w.id = s.session_id "
        "WHERE w.user_id = ? AND w.status = 'completed' "
        "ORDER BY w.id DESC, s.set_index ASC",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    rows_by_exercise: dict[str, list[aiosqlite.Row]] = {}
    for row in rows:
        rows_by_exercise.setdefault(row[7], []).append(row)
    return {
        exercise_id: _group_outcomes(exercise_rows, limit=limit)
        for exercise_id, exercise_rows in rows_by_exercise.items()
    }


def _constant_or_max(values: set[float]) -> float | None:
    """Assumed constant across sets of the same exercise in one session (A§7.3's engine works
    with one working load per session); fall back to the highest logged value if that
    assumption ever doesn't hold, rather than guessing further."""
    if not values:
        return None
    return next(iter(values)) if len(values) == 1 else max(values)


def _session_outcome_from_set_rows(set_rows: list[aiosqlite.Row]) -> SessionOutcome:
    logged_loads = {row[1] for row in set_rows if row[1] is not None}
    load_kg = _constant_or_max(logged_loads)
    planned_loads = {row[3] for row in set_rows if row[3] is not None}
    planned_load_kg = _constant_or_max(planned_loads)

    all_performed_at_or_above_target = True
    any_performed_below_min = False
    min_reps_performed: int | None = None
    for row in set_rows:
        (
            _session_id,
            actual_load_kg,
            actual_reps,
            planned_load_kg_row,
            reps_min,
            reps_max,
            skipped,
        ) = row[:7]
        if skipped or actual_reps is None or actual_load_kg is None:
            all_performed_at_or_above_target = False
            continue
        if min_reps_performed is None or actual_reps < min_reps_performed:
            min_reps_performed = int(actual_reps)
        if (planned_load_kg_row is not None and actual_load_kg < planned_load_kg_row) or (
            reps_max is not None and actual_reps < reps_max
        ):
            all_performed_at_or_above_target = False
        if reps_min is not None and actual_reps < reps_min:
            any_performed_below_min = True

    hit_reps_max = all_performed_at_or_above_target
    below_reps_min = (not hit_reps_max) and any_performed_below_min
    return SessionOutcome(
        load_kg=load_kg,
        planned_load_kg=planned_load_kg,
        hit_reps_max=hit_reps_max,
        below_reps_min=below_reps_min,
        min_reps_performed=min_reps_performed,
    )


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


_AREA_QUESTION_PREFIX = "area:"


async def latest_checkin_answers(
    conn: aiosqlite.Connection, user_id: int
) -> dict[str, CheckinAnswer]:
    """The latest answer per body area (A§4.2 `checkins.question_key = "area:<area>"`), keyed
    by the catalog `loads_areas` short name, as `guards.checkins.increase_allowed` and
    `guards.context.GuardContext.checkins` consume it. "Latest" is by `asked_at` (then id):
    a newer unanswered check-in (`unknown`) supersedes an older `fine` one — silence is not
    consent (AGENTS.md §2). Areas never asked about have no entry, which the guard also
    treats as `unknown`. Question keys without the `area:` prefix are ignored."""
    async with conn.execute(
        "SELECT question_key, answer FROM checkins WHERE user_id = ? ORDER BY asked_at, id",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    answers: dict[str, CheckinAnswer] = {}
    for question_key, answer in rows:
        if not str(question_key).startswith(_AREA_QUESTION_PREFIX):
            continue
        answers[str(question_key)[len(_AREA_QUESTION_PREFIX) :]] = CheckinAnswer(answer)
    return answers
