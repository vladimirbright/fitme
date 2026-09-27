"""controllers/selectors for `workout_sessions`, `set_logs`, `checkins` and `health_holds`
(A§4.2)."""

from __future__ import annotations

import sqlite3

import pytest

from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import (
    answer_checkin,
    clear_health_hold,
    finish_workout_session,
    insert_checkin,
    insert_health_hold,
    insert_set_log,
    insert_workout_session,
    mark_set_log_skipped,
    start_workout_session,
    update_set_log_actual,
    update_workout_session_progress,
)
from fitme.db.selectors.training import (
    get_active_workout_session,
    get_checkin,
    get_workout_session,
    historical_max_by_exercise,
    historical_max_by_exercise_before_session,
    historical_max_kg,
    last_completed_workout_key_for_plan,
    latest_checkin_answers,
    list_checkins_for_session,
    list_open_health_holds,
    list_set_logs_for_session,
    list_workout_sessions_for_user,
    recent_session_outcomes,
    recent_session_outcomes_by_exercise,
)
from fitme.domain.enums import CheckinAnswer


async def _insert_plan_version(db: Database, user_id: int) -> int:
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_generate",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )
        plan_id = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="active"
        )
        return await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "Plan A", "workouts": []},
            origin="llm",
            decision_id=decision_id,
        )


async def test_workout_session_lifecycle(db: Database, user_id: int) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)

    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=plan_version_id, workout_key="A", status="draft"
        )
        await start_workout_session(conn, session_id)
        await update_workout_session_progress(
            conn, session_id, status="in_progress", current_block=1
        )

    async with db.read() as conn:
        session = await get_workout_session(conn, session_id)
        sessions = await list_workout_sessions_for_user(conn, user_id)
    assert session is not None
    assert session.status == "in_progress"
    assert session.current_block == 1
    assert session.started_at is not None
    assert sessions == [session]

    async with db.transaction() as conn:
        await finish_workout_session(conn, session_id, status="completed")

    async with db.read() as conn:
        session = await get_workout_session(conn, session_id)
    assert session is not None
    assert session.status == "completed"
    assert session.finished_at is not None


async def test_set_log_and_historical_max(db: Database, user_id: int) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="in_progress",
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id="barbell_back_squat",
            set_index=1,
            planned_load_kg=40.0,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=40.0,
            actual_reps=5,
            rpe=7.0,
            source="button",
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id="barbell_back_squat",
            set_index=2,
            planned_load_kg=40.0,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=42.5,
            actual_reps=5,
            rpe=8.0,
            source="button",
        )

    async with db.read() as conn:
        logs = await list_set_logs_for_session(conn, session_id)
        max_kg = await historical_max_kg(conn, user_id, "barbell_back_squat")
        no_history = await historical_max_kg(conn, user_id, "deadlift")

    assert len(logs) == 2
    assert all(log.skipped is False for log in logs)
    assert max_kg == 42.5
    assert no_history is None


async def _log_session(
    db: Database,
    user_id: int,
    plan_version_id: int,
    *,
    status: str = "completed",
    sets: list[dict[str, object]] | None = None,
    actual_reps: int | None = None,
    reps_max: int = 8,
    reps_min: int = 5,
) -> int:
    """Log one session with one or more sets for `barbell_back_squat`.

    `sets`, when given, is a list of `insert_set_log` overrides (one dict per set); this is
    how the B3 tests build a partially-logged or skipped-set session. Otherwise a single set
    is logged with `actual_reps` (or nothing, for a session with an entirely unlogged set).
    """
    if sets is None:
        sets = [
            {
                "actual_load_kg": 40.0 if actual_reps is not None else None,
                "actual_reps": actual_reps,
                "skipped": False,
            }
        ]
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status=status,
        )
        for index, overrides in enumerate(sets):
            defaults: dict[str, object] = {
                "session_id": session_id,
                "exercise_id": "barbell_back_squat",
                "set_index": index + 1,  # A§4.2: set_index is 1-based
                "planned_load_kg": 40.0,
                "planned_reps_min": reps_min,
                "planned_reps_max": reps_max,
                "actual_load_kg": None,
                "actual_reps": None,
                "skipped": False,
                "rpe": 7.0,
                "source": "button",
            }
            defaults.update(overrides)
            await insert_set_log(conn, **defaults)  # type: ignore[arg-type]
    return session_id


async def test_recent_session_outcomes_hit_reps_max(db: Database, user_id: int) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(db, user_id, plan_version_id, actual_reps=8)

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert len(outcomes) == 1
    assert outcomes[0].load_kg == 40.0
    assert outcomes[0].hit_reps_max is True
    assert outcomes[0].below_reps_min is False


async def test_recent_session_outcomes_below_reps_min(db: Database, user_id: int) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(db, user_id, plan_version_id, actual_reps=3)

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert outcomes[0].hit_reps_max is False
    assert outcomes[0].below_reps_min is True


async def test_recent_session_outcomes_orders_most_recent_first_and_respects_limit(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(db, user_id, plan_version_id, actual_reps=8)
    await _log_session(db, user_id, plan_version_id, actual_reps=3)
    await _log_session(db, user_id, plan_version_id, actual_reps=8)

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat", limit=2)

    assert len(outcomes) == 2
    # Most recent (third logged) session first.
    assert outcomes[0].hit_reps_max is True
    assert outcomes[1].below_reps_min is True


async def test_recent_session_outcomes_empty_with_no_history(db: Database, user_id: int) -> None:
    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")
    assert outcomes == []


async def test_recent_session_outcomes_excludes_a_halted_session(
    db: Database, user_id: int
) -> None:
    """B3: only `status = 'completed'` sessions count. A halted session, even one whose
    logged set looks like a success, must not produce an increase."""
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(db, user_id, plan_version_id, status="halted", actual_reps=8)

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert outcomes == []


async def test_recent_session_outcomes_excludes_an_aborted_session(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(db, user_id, plan_version_id, status="aborted", actual_reps=8)

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert outcomes == []


async def test_recent_session_outcomes_partial_logging_is_not_a_success(
    db: Database, user_id: int
) -> None:
    """B3: a completed session with one set unlogged (no actual_reps at all) must not read as
    a success, even though every set that *was* logged hit reps_max."""
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(
        db,
        user_id,
        plan_version_id,
        sets=[
            {"actual_load_kg": 40.0, "actual_reps": 8, "skipped": False},
            {"actual_load_kg": None, "actual_reps": None, "skipped": False},  # never logged
        ],
    )

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert len(outcomes) == 1
    assert outcomes[0].hit_reps_max is False


async def test_recent_session_outcomes_skipped_set_is_not_a_success(
    db: Database, user_id: int
) -> None:
    """B3: a completed session with one set explicitly skipped must not read as a success."""
    plan_version_id = await _insert_plan_version(db, user_id)
    await _log_session(
        db,
        user_id,
        plan_version_id,
        sets=[
            {"actual_load_kg": 40.0, "actual_reps": 8, "skipped": False},
            {"actual_load_kg": None, "actual_reps": None, "skipped": True},
        ],
    )

    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, "barbell_back_squat")

    assert len(outcomes) == 1
    assert outcomes[0].hit_reps_max is False


async def test_insert_set_log_rejects_a_zero_set_index(db: Database, user_id: int) -> None:
    """ALSO REQUIRED: set_index is 1-based everywhere (A§4.2) — `0001_init.sql` has no CHECK
    for it (and migrations are never edited once applied, A§4.7), so `insert_set_log` itself
    rejects a 0 (or negative) set_index before it ever reaches the database."""
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="in_progress",
        )
        with pytest.raises(ValueError, match="set_index must be >= 1"):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id="barbell_back_squat",
                set_index=0,
                planned_load_kg=40.0,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=None,
                actual_reps=None,
                rpe=None,
                source="button",
            )


async def test_skipped_set_invariant_is_a_schema_check_not_just_convention(
    db: Database, user_id: int
) -> None:
    """ALSO REQUIRED: `skipped = 1` must always pair with both `actual_reps` and
    `actual_load_kg` NULL, enforced by the DB itself, not just by `insert_set_log`'s default
    parameters."""
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )

    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "INSERT INTO set_logs (session_id, exercise_id, set_index, planned_load_kg, "
            "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, "
            "rpe, source, created_at) VALUES (?, 'barbell_back_squat', 1, 40.0, 5, 8, "
            "40.0, 8, 1, NULL, 'button', ?)",
            (session_id, "2024-01-01T00:00:00.000000Z"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "INSERT INTO set_logs (session_id, exercise_id, set_index, planned_load_kg, "
            "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, "
            "rpe, source, created_at) VALUES (?, 'barbell_back_squat', 1, 40.0, 5, 8, "
            "NULL, 8, 1, NULL, 'button', ?)",
            (session_id, "2024-01-01T00:00:00.000000Z"),
        )


async def test_checkin_is_created_unknown_and_only_changes_via_answer_checkin(
    db: Database, user_id: int
) -> None:
    """Proves the invariant end to end: a freshly-created check-in is 'unknown' with no
    answered_at, insert_checkin has no way to set anything else, and it becomes a real
    answer only through the explicit answer_checkin call."""
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="in_progress",
        )
        checkin_id = await insert_checkin(
            conn, user_id=user_id, session_id=session_id, question_key="area:lower_back"
        )

    async with db.read() as conn:
        checkin = await get_checkin(conn, checkin_id)
        checkins = await list_checkins_for_session(conn, session_id)
    assert checkin is not None
    assert checkin.answer == "unknown"
    assert checkin.answered_at is None
    assert checkins == [checkin]

    async with db.transaction() as conn:
        await answer_checkin(conn, checkin_id, answer="fine")

    async with db.read() as conn:
        checkin = await get_checkin(conn, checkin_id)
    assert checkin is not None
    assert checkin.answer == "fine"
    assert checkin.answered_at is not None


async def test_checkin_invariant_is_a_schema_check_not_just_convention(
    db: Database, user_id: int
) -> None:
    """(answer = 'unknown') = (answered_at IS NULL): pairing 'unknown' with a real
    answered_at (or a real answer with a NULL answered_at) must be rejected by the DB
    itself, independent of any application code."""
    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "INSERT INTO checkins (user_id, session_id, question_key, answer, asked_at, "
            "answered_at) VALUES (?, NULL, 'area:knee', 'unknown', ?, ?)",
            (user_id, "2024-01-01T00:00:00.000000Z", "2024-01-01T00:00:00.000000Z"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "INSERT INTO checkins (user_id, session_id, question_key, answer, asked_at, "
            "answered_at) VALUES (?, NULL, 'area:knee', 'fine', ?, NULL)",
            (user_id, "2024-01-01T00:00:00.000000Z"),
        )


async def test_health_hold_open_and_clear(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        hold_id = await insert_health_hold(
            conn, user_id=user_id, reason="stop_word", source_session_id=None
        )

    async with db.read() as conn:
        open_holds = await list_open_health_holds(conn, user_id)
    assert len(open_holds) == 1
    assert open_holds[0].id == hold_id
    assert open_holds[0].cleared_at is None

    async with db.transaction() as conn:
        await clear_health_hold(conn, hold_id)

    async with db.read() as conn:
        open_holds = await list_open_health_holds(conn, user_id)
    assert open_holds == []


async def _completed_session_with_sets(
    db: Database, user_id: int, plan_version_id: int, sets: list[tuple[str, float, int]]
) -> int:
    """One completed session; `sets` = (exercise_id, load_kg, reps), planned 5-8 at that load."""
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        for index, (exercise_id, load_kg, reps) in enumerate(sets, start=1):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=exercise_id,
                set_index=index,
                planned_load_kg=load_kg,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=load_kg,
                actual_reps=reps,
                rpe=None,
                source="button",
            )
    return session_id


async def test_historical_max_by_exercise_covers_every_logged_exercise(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    await _completed_session_with_sets(
        db, user_id, plan_version_id, [("barbell_back_squat", 40.0, 8), ("pushup", 1.0, 10)]
    )
    await _completed_session_with_sets(
        db, user_id, plan_version_id, [("barbell_back_squat", 50.0, 5)]
    )

    async with db.read() as conn:
        by_exercise = await historical_max_by_exercise(conn, user_id)
    assert by_exercise == {"barbell_back_squat": 50.0, "pushup": 1.0}


async def test_historical_max_by_exercise_is_empty_with_no_logs(db: Database, user_id: int) -> None:
    async with db.read() as conn:
        assert await historical_max_by_exercise(conn, user_id) == {}


async def test_recent_session_outcomes_by_exercise_matches_the_per_exercise_selector(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    for load, reps in ((40.0, 8), (42.5, 6), (42.5, 8)):
        await _completed_session_with_sets(
            db, user_id, plan_version_id, [("barbell_back_squat", load, reps), ("pushup", 1.0, 12)]
        )

    async with db.read() as conn:
        by_exercise = await recent_session_outcomes_by_exercise(conn, user_id, limit=2)
        squat = await recent_session_outcomes(conn, user_id, "barbell_back_squat", limit=2)
        pushup = await recent_session_outcomes(conn, user_id, "pushup", limit=2)

    assert set(by_exercise) == {"barbell_back_squat", "pushup"}
    assert by_exercise["barbell_back_squat"] == squat
    assert by_exercise["pushup"] == pushup
    assert len(by_exercise["barbell_back_squat"]) == 2  # limit respected
    assert by_exercise["barbell_back_squat"][0].planned_load_kg == 42.5  # most recent first
    assert by_exercise["barbell_back_squat"][0].min_reps_performed == 8
    assert by_exercise["barbell_back_squat"][1].min_reps_performed == 6


async def test_latest_checkin_answers_uses_the_newest_row_per_area(
    db: Database, user_id: int
) -> None:
    async with db.transaction() as conn:
        older = await insert_checkin(
            conn, user_id=user_id, session_id=None, question_key="area:knee"
        )
        await answer_checkin(conn, older, answer="fine")
        await insert_checkin(conn, user_id=user_id, session_id=None, question_key="area:knee")
        back = await insert_checkin(
            conn, user_id=user_id, session_id=None, question_key="area:lower_back"
        )
        await answer_checkin(conn, back, answer="worse")
        await insert_checkin(conn, user_id=user_id, session_id=None, question_key="precheck")

    async with db.read() as conn:
        answers = await latest_checkin_answers(conn, user_id)

    # The newer, unanswered knee check-in wins over the older "fine" (silence is not consent).
    assert answers == {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.WORSE}


# --- M7 additions ------------------------------------------------------------------------


async def _insert_prescribed_set(
    conn: object, session_id: int, exercise_id: str, set_index: int, planned_kg: float | None
) -> int:
    return await insert_set_log(
        conn,  # type: ignore[arg-type]
        session_id=session_id,
        exercise_id=exercise_id,
        set_index=set_index,
        planned_load_kg=planned_kg,
        planned_reps_min=5,
        planned_reps_max=8,
        actual_load_kg=None,
        actual_reps=None,
        rpe=None,
        source="button",
    )


async def test_update_set_log_actual_and_mark_skipped(db: Database, user_id: int) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="in_progress",
        )
        first = await _insert_prescribed_set(conn, session_id, "barbell_back_squat", 1, 40.0)
        second = await _insert_prescribed_set(conn, session_id, "barbell_back_squat", 2, 40.0)
        await update_set_log_actual(
            conn, first, actual_load_kg=40.0, actual_reps=8, source="free_text"
        )
        await mark_set_log_skipped(conn, second, source="button")

    async with db.read() as conn:
        logs = {log.id: log for log in await list_set_logs_for_session(conn, session_id)}
    assert logs[first].actual_load_kg == 40.0 and logs[first].actual_reps == 8
    assert logs[first].source == "free_text" and not logs[first].skipped
    assert logs[second].skipped and logs[second].actual_reps is None
    assert logs[second].actual_load_kg is None

    # Logging a set again clears `skipped` (the schema forbids skipped + actuals together).
    async with db.transaction() as conn:
        await update_set_log_actual(
            conn, second, actual_load_kg=None, actual_reps=5, source="button"
        )
    async with db.read() as conn:
        logs = {log.id: log for log in await list_set_logs_for_session(conn, session_id)}
    assert not logs[second].skipped and logs[second].actual_reps == 5


async def test_list_set_logs_for_session_is_in_creation_order(db: Database, user_id: int) -> None:
    """Blocks are sent in workout order; a repeated exercise later in the workout restarts its
    `set_index` at 1, so ordering by `set_index` would interleave two blocks' rows."""
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="in_progress",
        )
        await _insert_prescribed_set(conn, session_id, "barbell_back_squat", 1, 40.0)
        await _insert_prescribed_set(conn, session_id, "barbell_back_squat", 2, 40.0)
        await _insert_prescribed_set(conn, session_id, "pushup", 1, None)
        await _insert_prescribed_set(conn, session_id, "barbell_back_squat", 1, 30.0)
    async with db.read() as conn:
        logs = await list_set_logs_for_session(conn, session_id)
    assert [(log.exercise_id, log.set_index) for log in logs] == [
        ("barbell_back_squat", 1),
        ("barbell_back_squat", 2),
        ("pushup", 1),
        ("barbell_back_squat", 1),
    ]


async def test_get_active_workout_session_finds_only_unfinished_sessions(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.read() as conn:
        assert await get_active_workout_session(conn, user_id) is None
    async with db.transaction() as conn:
        done = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        active = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=plan_version_id, workout_key="B", status="draft"
        )
    async with db.read() as conn:
        found = await get_active_workout_session(conn, user_id)
    assert found is not None and found.id == active and found.id != done

    for status in ("confirmed", "in_progress"):
        async with db.transaction() as conn:
            await update_workout_session_progress(conn, active, status=status, current_block=0)
        async with db.read() as conn:
            found = await get_active_workout_session(conn, user_id)
        assert found is not None and found.status == status

    for status in ("aborted", "halted"):
        async with db.transaction() as conn:
            await finish_workout_session(conn, active, status=status)
        async with db.read() as conn:
            assert await get_active_workout_session(conn, user_id) is None


async def test_last_completed_workout_key_for_plan_joins_through_plan_versions(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.read() as conn:
        version = await conn.execute_fetchall(  # test-only: the plan id behind the version
            "SELECT plan_id FROM plan_versions WHERE id = ?", (plan_version_id,)
        )
    plan_id = int(list(version)[0][0])

    async with db.read() as conn:
        assert await last_completed_workout_key_for_plan(conn, user_id, plan_id) is None
    async with db.transaction() as conn:
        await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        # A later session on the same plan, but not completed: it must not count.
        await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="B",
            status="aborted",
        )
    async with db.read() as conn:
        assert await last_completed_workout_key_for_plan(conn, user_id, plan_id) == "A"
        assert await last_completed_workout_key_for_plan(conn, user_id, plan_id + 999) is None


async def test_historical_max_by_exercise_before_session_excludes_that_session(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        earlier = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        today = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        for session_id, kg in ((earlier, 40.0), (today, 45.0)):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id="barbell_back_squat",
                set_index=1,
                planned_load_kg=kg,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=kg,
                actual_reps=8,
                rpe=None,
                source="button",
            )
    async with db.read() as conn:
        before = await historical_max_by_exercise_before_session(conn, user_id, today)
        overall = await historical_max_by_exercise(conn, user_id)
        nothing = await historical_max_by_exercise_before_session(conn, user_id, earlier)
    assert before == {"barbell_back_squat": 40.0}
    assert overall == {"barbell_back_squat": 45.0}
    assert nothing == {"barbell_back_squat": 45.0}
