"""ADR 0016: progression from real training history in a migrated SQLite file — the bench
press example week by week through the load engine's own selectors, timed work through
`recent_completed_set_logs`, imported history left out, and exercise variations kept
apart."""

from __future__ import annotations

from datetime import UTC, datetime

from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import (
    insert_imported_workout_session,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.selectors.training import recent_completed_set_logs
from fitme.domain.catalog import Exercise
from fitme.domain.models import Load, Prescription
from fitme.domain.results import ChangeTarget
from fitme.services import progression
from fitme.services.loads import LoadKind, next_load_for_exercise
from fitme.services.progression import TargetKind

_CATALOG = load_catalog()
_SINCE = "1970-01-01T00:00:00.000000Z"


def _exercise(exercise_id: str) -> Exercise:
    exercise = _CATALOG.by_id(exercise_id)
    assert exercise is not None
    return exercise


async def _plan_version(db: Database, user_id: int) -> int:
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
            conn, user_id=user_id, name="Plan", is_default=True, status="active"
        )
        return await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "Plan", "workouts": []},
            origin="llm",
            decision_id=decision_id,
        )


async def _bench_session(
    db: Database, user_id: int, version: int, kg: float, reps: list[int], *, exercise_id: str
) -> None:
    """One completed session: 4 × 4-6 prescribed at `kg`, performed as `reps` at `kg`."""
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version, workout_key="A", status="completed"
        )
        for index, done in enumerate(reps, start=1):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=exercise_id,
                set_index=index,
                planned_load_kg=kg,
                planned_reps_min=4,
                planned_reps_max=6,
                actual_load_kg=kg,
                actual_reps=done,
                rpe=None,
                source="button",
            )


async def _next(db: Database, user_id: int, exercise: Exercise) -> tuple[Load, LoadKind]:
    decision = await next_load_for_exercise(
        db,
        user_id=user_id,
        exercise=exercise,
        checkins={},
        flagged_areas=frozenset(),
        cap_kg=2.5,
        since_7d=_SINCE,
    )
    return decision.load, decision.kind


async def test_the_bench_press_example_week_by_week(db: Database, user_id: int) -> None:
    """Simulated history (labelled as such in the ADR): 60 × 6,6,6,6; 62.5 × 6,6,5,5;
    62.5 × 6,6,6,6."""
    bench = _exercise("barbell_bench_press")
    version = await _plan_version(db, user_id)
    assert await _next(db, user_id, bench) == (Load(kind="calibration"), LoadKind.CALIBRATION)

    await _bench_session(db, user_id, version, 60.0, [6, 6, 6, 6], exercise_id=bench.id)
    assert await _next(db, user_id, bench) == (Load(kind="kg", kg=62.5), LoadKind.INCREASE)

    await _bench_session(db, user_id, version, 62.5, [6, 6, 5, 5], exercise_id=bench.id)
    assert await _next(db, user_id, bench) == (Load(kind="kg", kg=62.5), LoadKind.HOLD)

    await _bench_session(db, user_id, version, 62.5, [6, 6, 6, 6], exercise_id=bench.id)
    assert await _next(db, user_id, bench) == (Load(kind="kg", kg=65.0), LoadKind.INCREASE)


async def test_a_variations_history_never_sets_another_variations_load(
    db: Database, user_id: int
) -> None:
    version = await _plan_version(db, user_id)
    await _bench_session(
        db, user_id, version, 60.0, [6, 6, 6, 6], exercise_id="barbell_bench_press"
    )
    await _bench_session(db, user_id, version, 100.0, [6, 6, 6, 6], exercise_id="barbell_deadlift")
    for related in (
        "dumbbell_bench_press",
        "barbell_incline_bench_press",
        "barbell_romanian_deadlift",
    ):
        exercise = _CATALOG.by_id(related)
        if exercise is None:
            continue
        load, kind = await _next(db, user_id, exercise)
        assert (load, kind) == (Load(kind="calibration"), LoadKind.CALIBRATION), related


async def _timed_session(
    db: Database,
    user_id: int,
    version: int,
    seconds: list[int | None],
    *,
    planned: int = 40,
    imported: str | None = None,
) -> None:
    async with db.transaction() as conn:
        if imported is not None:
            session_id = await insert_imported_workout_session(
                conn,
                user_id=user_id,
                workout_key="A",
                performed_at=datetime(2026, 1, 1, tzinfo=UTC),
                import_hash=imported,
            )
        else:
            session_id = await insert_workout_session(
                conn, user_id=user_id, plan_version_id=version, workout_key="A", status="completed"
            )
        for index, done in enumerate(seconds, start=1):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id="plank",
                set_index=index,
                planned_load_kg=None,
                planned_reps_min=None,
                planned_reps_max=None,
                actual_load_kg=None,
                actual_reps=None,
                rpe=None,
                source="button",
                planned_seconds=planned,
                actual_seconds=done,
            )


_PLANK = Prescription.model_validate(
    {
        "exercise_id": "plank",
        "sets": 3,
        "measure": "time",
        "seconds": 40,
        "load": {"kind": "bodyweight"},
        "rest_seconds": 45,
    }
)


async def _plank_target(db: Database, user_id: int) -> progression.TargetDecision:
    async with db.read() as conn:
        sessions = await recent_completed_set_logs(conn, user_id, "plank", limit=2)
    return progression.next_target(_PLANK, _exercise("plank"), sessions)


async def test_a_timed_target_grows_only_after_two_app_sessions_at_it(
    db: Database, user_id: int
) -> None:
    version = await _plan_version(db, user_id)
    assert (await _plank_target(db, user_id)).kind == TargetKind.HOLD  # no history: nothing

    # Imported history is not evidence (A§7.3), however good it looks.
    await _timed_session(db, user_id, version, [60, 60, 60], imported="first")
    await _timed_session(db, user_id, version, [60, 60, 60], imported="second")
    assert (await _plank_target(db, user_id)).change is None

    await _timed_session(db, user_id, version, [40, 40, 40])
    assert (await _plank_target(db, user_id)).kind == TargetKind.HOLD  # one is not a trend
    await _timed_session(db, user_id, version, [40, 45, 40])
    decision = await _plank_target(db, user_id)
    assert decision.kind == TargetKind.MORE
    assert decision.change == ChangeTarget(exercise_id="plank", from_target=40, seconds=45)


async def test_an_incomplete_session_earns_no_longer_target(db: Database, user_id: int) -> None:
    version = await _plan_version(db, user_id)
    await _timed_session(db, user_id, version, [40, 40, 40])
    await _timed_session(db, user_id, version, [40, 40, None])  # a set left unlogged
    assert (await _plank_target(db, user_id)).change is None


async def test_only_the_newest_sessions_are_read_and_in_order(db: Database, user_id: int) -> None:
    version = await _plan_version(db, user_id)
    for seconds in ([10, 10, 10], [20, 20, 20], [30, 30, 30]):
        await _timed_session(db, user_id, version, seconds)
    async with db.read() as conn:
        sessions = await recent_completed_set_logs(conn, user_id, "plank", limit=2)
    assert [[row.actual_seconds for row in rows] for rows in sessions] == [
        [30, 30, 30],
        [20, 20, 20],
    ]
