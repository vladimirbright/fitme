"""Write-side access for `workout_sessions`, `set_logs`, `checkins` and `health_holds`
(A§4.2). All timestamps default to `clock.utc_now()` internally (A§4.2); none of these
functions take a timestamp parameter.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import aiosqlite

from fitme import clock


class SessionsNotOwnedError(RuntimeError):
    """One or more session ids in a delete batch aren't `user_id`'s own, or don't exist at
    all (A§9.4: "any foreign or unknown id aborts the whole batch")."""


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


async def insert_imported_workout_session(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    workout_key: str,
    performed_at: datetime,
    import_hash: str,
) -> int:
    """One imported past training (M11, `fitme history import`): a `completed` session that
    belongs to no plan (`plan_version_id` NULL, migration 0007: it came from no stored plan),
    whose `started_at` and `finished_at` are both the instant the file gave (`performed_at`,
    a timezone-aware datetime, formatted here by `clock.format_timestamp` like every other
    timestamp; no duration is invented), and whose `import_hash` (migration 0006) makes a
    second import of the same content a no-op. The only session insert that takes a time from
    the caller: the time is the record's own content, not "now"."""
    at = clock.format_timestamp(performed_at)
    cursor = await conn.execute(
        "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
        "current_block, started_at, finished_at, import_hash) "
        "VALUES (?, NULL, ?, 'completed', 0, ?, ?, ?)",
        (user_id, workout_key, at, at, import_hash),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def link_imported_sessions(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    links: Sequence[tuple[int, int, str]],  # (session_id, plan_version_id, workout_key)
) -> None:
    """M12 (`fitme history import`, `docs/import-format.md` "Linking imported trainings"):
    connect one or more previously-unlinked imported sessions to the plan workout they were
    trained from. Each row's `WHERE` repeats `import_hash IS NOT NULL AND plan_version_id IS
    NULL` so this can never touch a session the app itself created, nor re-link or change one
    an earlier run already linked (A§9.4-style idempotency) — a no-op `UPDATE` if the row no
    longer matches, never an error. Scoped to `user_id` so a caller can never link another
    user's row. The caller runs this inside the same `db.transaction()` as the sessions and
    plan versions it references, so the `plan_version_id` foreign key is always satisfied."""
    for session_id, plan_version_id, workout_key in links:
        await conn.execute(
            "UPDATE workout_sessions SET plan_version_id = ?, workout_key = ? "
            "WHERE id = ? AND user_id = ? AND import_hash IS NOT NULL AND plan_version_id IS NULL",
            (plan_version_id, workout_key, session_id, user_id),
        )


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
    `None`, so a partial or halted session stays visible in the data (A§7.3).

    `set_index` is 1-based (matches `domain.results.SetResult.set_index`, A§4.2's own
    convention): the first set of a block is `1`, never `0`. Enforced here rather than in
    `0001_init.sql` — the migration has no `CHECK` for it, and this project's migrations are
    forward-only and never edited once applied (A§4.7)."""
    if set_index < 1:
        raise ValueError(f"set_index must be >= 1 (1-based), got {set_index}")
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


async def update_set_log_actual(
    conn: aiosqlite.Connection,
    set_log_id: int,
    *,
    actual_load_kg: float | None,
    actual_reps: int,
    source: str,
) -> None:
    """Record what the user actually did for one prescribed set (A§6.5 step 5: "According
    to plan" or a confirmed `result_parse`). Clears `skipped`, since a logged set was
    performed (the schema forbids the two together)."""
    await conn.execute(
        "UPDATE set_logs SET actual_load_kg = ?, actual_reps = ?, skipped = 0, source = ? "
        "WHERE id = ?",
        (actual_load_kg, actual_reps, source, set_log_id),
    )


async def restore_set_log(
    conn: aiosqlite.Connection,
    set_log_id: int,
    *,
    actual_load_kg: float | None,
    actual_reps: int | None,
    skipped: bool,
    source: str,
) -> None:
    """Put one set row back exactly as it was (ADR 0003: undoing a correction of a logged
    set), including an unlogged (`actual_*` NULL, not skipped) or skipped row."""
    await conn.execute(
        "UPDATE set_logs SET actual_load_kg = ?, actual_reps = ?, skipped = ?, source = ? "
        "WHERE id = ?",
        (actual_load_kg, actual_reps, int(skipped), source, set_log_id),
    )


async def mark_set_log_skipped(conn: aiosqlite.Connection, set_log_id: int, *, source: str) -> None:
    """A prescribed set the user didn't perform (A§4.2): `skipped = 1` with both `actual_*`
    columns NULL, so it stays visible as a gap in the log."""
    await conn.execute(
        "UPDATE set_logs SET skipped = 1, actual_load_kg = NULL, actual_reps = NULL, source = ? "
        "WHERE id = ?",
        (source, set_log_id),
    )


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


async def abort_sessions_for_plan(
    conn: aiosqlite.Connection, *, user_id: int, plan_id: int
) -> None:
    """`services.planning.delete_plan` step 2 (A§4.3): abort this user's unfinished sessions
    (`draft`/`confirmed`/`in_progress`) on any version of `plan_id`, before they are detached
    from it. A `completed`, `aborted` or `halted` session is left as it is."""
    await conn.execute(
        "UPDATE workout_sessions SET status = 'aborted', finished_at = ? "
        "WHERE user_id = ? AND status IN ('draft', 'confirmed', 'in_progress') "
        "AND plan_version_id IN (SELECT id FROM plan_versions WHERE plan_id = ?)",
        (clock.utc_now(), user_id, plan_id),
    )


async def detach_sessions_for_plan(
    conn: aiosqlite.Connection, *, user_id: int, plan_id: int
) -> None:
    """`services.planning.delete_plan` step 3 (A§4.3): detach every one of this user's
    sessions (any status) from any version of `plan_id` (`plan_version_id` -> NULL).
    Training history — the session row, its `set_logs`, `workout_key` — is kept; only the
    plan link is cleared. Must run before the plan's versions are deleted:
    `workout_sessions.plan_version_id` is `ON DELETE RESTRICT`."""
    await conn.execute(
        "UPDATE workout_sessions SET plan_version_id = NULL "
        "WHERE user_id = ? "
        "AND plan_version_id IN (SELECT id FROM plan_versions WHERE plan_id = ?)",
        (user_id, plan_id),
    )


async def delete_workout_sessions(
    conn: aiosqlite.Connection, *, user_id: int, session_ids: Sequence[int]
) -> None:
    """Hard delete a batch of this user's own sessions (A§9.4). Raises `SessionsNotOwnedError`
    (nothing is deleted) if any id isn't this user's own or doesn't exist — the caller runs
    this inside one `db.transaction()`, so the raise rolls the whole batch back.

    `set_logs` and `chat_messages` cascade from `workout_sessions` (`ON DELETE CASCADE`,
    0001_init.sql). `health_holds.source_session_id` and `checkins.session_id` are
    `ON DELETE SET NULL` there, which already gives every *other* check-in the detach A§9.4
    asks for — except a `fine` one, which A§9.4 says to delete outright, so those are removed
    explicitly first, before they'd otherwise just get detached like the rest.
    """
    ids = list(dict.fromkeys(session_ids))  # de-dupe, keep order
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    async with conn.execute(
        f"SELECT id FROM workout_sessions WHERE id IN ({placeholders}) AND user_id = ?",
        (*ids, user_id),
    ) as cursor:
        owned = {row[0] for row in await cursor.fetchall()}
    missing = [session_id for session_id in ids if session_id not in owned]
    if missing:
        raise SessionsNotOwnedError(
            f"session id(s) not owned by user {user_id} or don't exist: {missing}"
        )
    await conn.execute(
        f"DELETE FROM checkins WHERE session_id IN ({placeholders}) AND answer = 'fine'", ids
    )
    await conn.execute(f"DELETE FROM workout_sessions WHERE id IN ({placeholders})", ids)
