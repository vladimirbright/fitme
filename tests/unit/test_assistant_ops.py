"""`services.assistant.apply_plan_ops` (ADR 0003): the deterministic step between the
assistant agent's typed ops and `plan_edit.save_edit`'s guards. Pure — no DB, no model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.domain.assistant import (
    AddExercise,
    AssistantAction,
    AssistantEdits,
    FixLoggedSet,
    RemoveExercise,
    RenameWorkout,
    SetPrescription,
    SetSchedule,
    SwapExercise,
)
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.services.assistant import OpError, apply_plan_ops

ALLOWED = frozenset({"barbell_back_squat", "dumbbell_bench_press", "pushup", "goblet_squat"})


def _item(exercise_id: str, load: Load) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=3, reps_min=8, reps_max=10, load=load, rest_seconds=90
    )


def _plan() -> Plan:
    return Plan(
        name="Home plan",
        schedule=[
            ScheduledDay(weekday=1, workout_key="A"),
            ScheduledDay(weekday=4, workout_key="B"),
        ],
        workouts=[
            Workout(
                key="A",
                title="Legs",
                blocks=[
                    Block(
                        kind="single", items=[_item("barbell_back_squat", Load(kind="kg", kg=60))]
                    )
                ],
            ),
            Workout(
                key="B",
                title="Push",
                blocks=[
                    Block(
                        kind="superset",
                        items=[
                            _item("dumbbell_bench_press", Load(kind="kg", kg=20)),
                            _item("pushup", Load(kind="bodyweight")),
                        ],
                    )
                ],
            ),
        ],
    )


def test_set_prescription_changes_only_the_named_fields_and_leaves_the_input_untouched() -> None:
    plan = _plan()
    op = SetPrescription(
        op="set_prescription",
        plan_id=1,
        workout_key="a",  # case-insensitive
        exercise_id="barbell_back_squat",
        load=Load(kind="kg", kg=62.5),
        sets=4,
    )
    edited = apply_plan_ops(plan, [op], ALLOWED)
    item = edited.workouts[0].blocks[0].items[0]
    assert (item.sets, item.reps_min, item.reps_max, item.load.kg) == (4, 8, 10, 62.5)
    assert plan.workouts[0].blocks[0].items[0].load.kg == 60  # not mutated


def test_a_single_rep_target_outside_the_range_moves_both_ends() -> None:
    op = SetPrescription(
        op="set_prescription",
        plan_id=1,
        workout_key="A",
        exercise_id="barbell_back_squat",
        reps_min=12,
    )
    item = apply_plan_ops(_plan(), [op], ALLOWED).workouts[0].blocks[0].items[0]
    assert (item.reps_min, item.reps_max) == (12, 12)


def test_set_prescription_needs_at_least_one_change() -> None:
    with pytest.raises(ValidationError):
        SetPrescription(op="set_prescription", plan_id=1, workout_key="A", exercise_id="x")


def test_swap_resets_the_load_to_calibration_and_refuses_a_disallowed_exercise() -> None:
    swap = SwapExercise(
        op="swap_exercise",
        plan_id=1,
        workout_key="A",
        exercise_id="barbell_back_squat",
        new_exercise_id="goblet_squat",
    )
    item = apply_plan_ops(_plan(), [swap], ALLOWED).workouts[0].blocks[0].items[0]
    assert item.exercise_id == "goblet_squat"
    assert item.load == Load(kind="calibration")  # never carries the old exercise's kg over

    bad = swap.model_copy(update={"new_exercise_id": "barbell_deadlift"})
    with pytest.raises(OpError) as exc:
        apply_plan_ops(_plan(), [bad], ALLOWED)
    assert exc.value.code == "exercise_not_allowed"


def test_add_exercise_appends_a_single_block_and_checks_the_allowed_list() -> None:
    add = AddExercise(
        op="add_exercise",
        plan_id=1,
        workout_key="A",
        exercise_id="pushup",
        sets=3,
        reps_min=10,
        reps_max=15,
        load=Load(kind="bodyweight"),
    )
    blocks = apply_plan_ops(_plan(), [add], ALLOWED).workouts[0].blocks
    assert [b.items[0].exercise_id for b in blocks] == ["barbell_back_squat", "pushup"]
    with pytest.raises(OpError):
        apply_plan_ops(_plan(), [add.model_copy(update={"exercise_id": "nope"})], ALLOWED)


def test_remove_turns_a_two_item_superset_into_a_single_and_refuses_an_empty_workout() -> None:
    remove = RemoveExercise(op="remove_exercise", plan_id=1, workout_key="B", exercise_id="pushup")
    block = apply_plan_ops(_plan(), [remove], ALLOWED).workouts[1].blocks[0]
    assert block.kind == "single" and [i.exercise_id for i in block.items] == [
        "dumbbell_bench_press"
    ]

    last = RemoveExercise(
        op="remove_exercise", plan_id=1, workout_key="A", exercise_id="barbell_back_squat"
    )
    with pytest.raises(OpError) as exc:
        apply_plan_ops(_plan(), [last], ALLOWED)
    assert exc.value.code == "workout_empty"


def test_unknown_workout_or_exercise_is_an_op_error() -> None:
    with pytest.raises(OpError) as exc:
        apply_plan_ops(
            _plan(),
            [
                RemoveExercise(
                    op="remove_exercise", plan_id=1, workout_key="Z", exercise_id="pushup"
                )
            ],
            ALLOWED,
        )
    assert exc.value.code == "unknown_workout"
    with pytest.raises(OpError) as exc:
        apply_plan_ops(
            _plan(),
            [
                RemoveExercise(
                    op="remove_exercise", plan_id=1, workout_key="A", exercise_id="pushup"
                )
            ],
            ALLOWED,
        )
    assert exc.value.code == "unknown_exercise"


def test_set_schedule_sorts_days_and_rejects_duplicates_and_unknown_keys() -> None:
    op = SetSchedule(
        op="set_schedule",
        plan_id=1,
        days=[ScheduledDay(weekday=3, workout_key="b"), ScheduledDay(weekday=0, workout_key="A")],
    )
    schedule = apply_plan_ops(_plan(), [op], ALLOWED).schedule
    assert [(d.weekday, d.workout_key) for d in schedule] == [(0, "A"), (3, "B")]

    twice = op.model_copy(
        update={
            "days": [
                ScheduledDay(weekday=0, workout_key="A"),
                ScheduledDay(weekday=0, workout_key="B"),
            ]
        }
    )
    with pytest.raises(OpError) as exc:
        apply_plan_ops(_plan(), [twice], ALLOWED)
    assert exc.value.code == "schedule_invalid"

    unknown = op.model_copy(update={"days": [ScheduledDay(weekday=0, workout_key="C")]})
    with pytest.raises(OpError) as exc:
        apply_plan_ops(_plan(), [unknown], ALLOWED)
    assert exc.value.code == "unknown_workout"


def test_rename_workout_runs_the_wording_check() -> None:
    op = RenameWorkout(
        op="rename_workout", plan_id=1, workout_key="A", title="Your personal coach day"
    )
    with pytest.raises(OpError) as exc:
        apply_plan_ops(_plan(), [op], ALLOWED)
    assert exc.value.code == "forbidden_term"


def test_one_message_edits_one_plan_or_one_session_never_both() -> None:
    set_op = SetPrescription(
        op="set_prescription", plan_id=1, workout_key="A", exercise_id="barbell_back_squat", sets=4
    )
    fix = FixLoggedSet(
        op="fix_logged_set", session_id=7, exercise_id="barbell_back_squat", set_number=1, reps=5
    )
    with pytest.raises(ValidationError):
        AssistantEdits(ops=[set_op, fix])
    with pytest.raises(ValidationError):
        AssistantEdits(ops=[set_op, set_op.model_copy(update={"plan_id": 2})])
    assert AssistantEdits(ops=[set_op, set_op]).ops


def test_actions_require_their_arguments() -> None:
    with pytest.raises(ValidationError):
        AssistantAction(action="show_plan")
    with pytest.raises(ValidationError):
        AssistantAction(action="revise_plan", plan_id=1, request="  ")
    assert AssistantAction(action="train").action == "train"
