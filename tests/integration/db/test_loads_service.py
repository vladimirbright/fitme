"""`services.loads.next_load_for_exercise`: the thin async wrapper around the pure load
engine, reading history through a single `db.read()` (A§4.6 unit-of-work rules)."""

from __future__ import annotations

from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer
from fitme.services.loads import next_load_for_exercise

_EXERCISE = Exercise.model_validate(
    {
        "id": "barbell_back_squat",
        "names": {"en": "Barbell back squat"},
        "kind": "compound",
        "pattern": "squat",
        "equipment": ["barbell", "rack"],
        "locations": ["public_gym"],
        "loads_areas": ["knee", "lower_back"],
        "increment_kg": 2.5,
        "start": {"kind": "kg", "kg": 20.0},
        "instructions": {"en": "..."},
    }
)

_FINE_CHECKINS = {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE}


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


async def test_next_load_for_exercise_with_no_history_is_calibration(
    db: Database, user_id: int
) -> None:
    decision = await next_load_for_exercise(
        db,
        user_id=user_id,
        exercise=_EXERCISE,
        checkins=_FINE_CHECKINS,
        flagged_areas=frozenset({"knee", "lower_back"}),
        cap_kg=2.5,
        since_7d="1970-01-01T00:00:00.000000Z",
    )
    assert decision.load.kind == "calibration"
    assert "calibration" in decision.reason


async def test_next_load_for_exercise_reads_real_history_and_increments(
    db: Database, user_id: int
) -> None:
    plan_version_id = await _insert_plan_version(db, user_id)
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        await insert_set_log(
            conn,
            session_id=session_id,
            exercise_id=_EXERCISE.id,
            set_index=1,  # A§4.2: set_index is 1-based
            planned_load_kg=40.0,
            planned_reps_min=5,
            planned_reps_max=8,
            actual_load_kg=40.0,
            actual_reps=8,
            rpe=7.0,
            source="button",
        )

    decision = await next_load_for_exercise(
        db,
        user_id=user_id,
        exercise=_EXERCISE,
        checkins=_FINE_CHECKINS,
        flagged_areas=frozenset({"knee", "lower_back"}),
        cap_kg=2.5,
        since_7d="1970-01-01T00:00:00.000000Z",
    )
    assert decision.load.kind == "kg"
    assert decision.load.kg == 42.5
