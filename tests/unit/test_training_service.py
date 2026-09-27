"""Pure helpers of `services.training` (M7): workout selection by weekday and rotation, and
mapping a session's `set_logs` rows back onto blocks (so a resume never duplicates rows)."""

from __future__ import annotations

from fitme.db.records import SetLogRecord
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.services.training import _assign_rows, _block_rows_exist, _pick_workout


def _prescription(exercise_id: str, sets: int) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        sets=sets,
        reps_min=5,
        reps_max=8,
        load=Load(kind="calibration"),
        rest_seconds=60,
    )


def _plan() -> Plan:
    return Plan(
        name="P",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=3, workout_key="B"),
        ],
        workouts=[
            Workout(
                key="A",
                title="A",
                blocks=[Block(kind="single", items=[_prescription("pushup", 3)])],
            ),
            Workout(
                key="B",
                title="B",
                blocks=[Block(kind="single", items=[_prescription("pushup", 3)])],
            ),
            Workout(
                key="C",
                title="C",
                blocks=[Block(kind="single", items=[_prescription("pushup", 3)])],
            ),
        ],
    )


def test_pick_workout_prefers_todays_schedule() -> None:
    workout, today = _pick_workout(_plan(), weekday=3, last_completed_key="C")
    assert workout.key == "B" and today


def test_pick_workout_rotates_after_the_last_completed_and_wraps() -> None:
    assert _pick_workout(_plan(), weekday=1, last_completed_key="A")[0].key == "B"
    assert _pick_workout(_plan(), weekday=1, last_completed_key="C")[0].key == "A"


def test_pick_workout_starts_at_the_first_workout_without_history_or_with_a_gone_key() -> None:
    assert _pick_workout(_plan(), weekday=1, last_completed_key=None)[0].key == "A"
    assert _pick_workout(_plan(), weekday=1, last_completed_key="Z")[0].key == "A"


def _row(row_id: int, exercise_id: str, set_index: int) -> SetLogRecord:
    return SetLogRecord(
        id=row_id,
        session_id=1,
        exercise_id=exercise_id,
        set_index=set_index,
        planned_load_kg=None,
        planned_reps_min=5,
        planned_reps_max=8,
        actual_load_kg=None,
        actual_reps=None,
        skipped=False,
        rpe=None,
        source="button",
        created_at="2026-01-01T00:00:00.000000Z",
    )


def test_assign_rows_maps_rows_onto_blocks_in_order_even_for_a_repeated_exercise() -> None:
    workout = Workout(
        key="A",
        title="A",
        blocks=[
            Block(kind="single", items=[_prescription("squat", 2)]),
            Block(kind="superset", items=[_prescription("bench", 2), _prescription("pushup", 1)]),
            Block(kind="single", items=[_prescription("squat", 2)]),
        ],
    )
    rows = [
        _row(1, "squat", 1),
        _row(2, "squat", 2),
        _row(3, "bench", 1),
        _row(4, "bench", 2),
        _row(5, "pushup", 1),
        _row(6, "squat", 1),
    ]
    assigned = _assign_rows(rows, workout)
    assert [[r.id for r in item] for item in assigned[0]] == [[1, 2]]
    assert [[r.id for r in item] for item in assigned[1]] == [[3, 4], [5]]
    assert [[r.id for r in item] for item in assigned[2]] == [[6]]  # block 2 only half created
    assert _block_rows_exist(assigned, workout, 0)
    assert _block_rows_exist(assigned, workout, 1)
    assert not _block_rows_exist(assigned, workout, 2)
    assert not _block_rows_exist(_assign_rows([], workout), workout, 0)
