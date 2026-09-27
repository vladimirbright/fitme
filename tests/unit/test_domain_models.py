"""Model validators for `domain/models.py` (A§4.5, IMPLEMENTATION_PLAN M2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.domain.enums import RefusalCode
from fitme.domain.models import (
    Block,
    Load,
    LoadChange,
    Plan,
    Prescription,
    Refusal,
    ScheduledDay,
    Workout,
)


def _prescription(**overrides: object) -> Prescription:
    defaults: dict[str, object] = {
        "exercise_id": "barbell_back_squat",
        "sets": 3,
        "reps_min": 5,
        "reps_max": 8,
        "load": Load(kind="kg", kg=60.0),
        "rest_seconds": 120,
    }
    defaults.update(overrides)
    return Prescription.model_validate(defaults)


def test_load_kg_required_when_kind_is_kg() -> None:
    with pytest.raises(ValidationError, match="required"):
        Load(kind="kg", kg=None)


def test_load_kg_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="> 0"):
        Load(kind="kg", kg=0.0)
    with pytest.raises(ValidationError, match="> 0"):
        Load(kind="kg", kg=-5.0)


def test_load_kg_must_be_none_for_non_kg_kinds() -> None:
    with pytest.raises(ValidationError, match="must be None"):
        Load(kind="bodyweight", kg=10.0)


def test_load_allows_bodyweight_and_calibration_without_kg() -> None:
    assert Load(kind="bodyweight").kg is None
    assert Load(kind="calibration").kg is None


def test_prescription_rejects_reps_min_above_reps_max() -> None:
    with pytest.raises(ValidationError, match="reps_min"):
        _prescription(reps_min=10, reps_max=5)


def test_prescription_allows_reps_min_equal_to_reps_max() -> None:
    prescription = _prescription(reps_min=8, reps_max=8)
    assert prescription.reps_min == prescription.reps_max == 8


def test_single_block_requires_exactly_one_item() -> None:
    with pytest.raises(ValidationError, match="exactly 1 item"):
        Block(kind="single", items=[_prescription(), _prescription()])


def test_single_block_with_one_item_is_valid() -> None:
    block = Block(kind="single", items=[_prescription()])
    assert len(block.items) == 1


def test_superset_block_rejects_fewer_than_two_items() -> None:
    with pytest.raises(ValidationError, match="2-4 items"):
        Block(kind="superset", items=[_prescription()])


def test_superset_block_rejects_more_than_four_items() -> None:
    with pytest.raises(ValidationError, match="2-4 items"):
        Block(kind="superset", items=[_prescription() for _ in range(5)])


def test_superset_block_accepts_two_to_four_items() -> None:
    for count in (2, 3, 4):
        block = Block(kind="superset", items=[_prescription() for _ in range(count)])
        assert len(block.items) == count


def test_plan_proposal_accepts_a_refusal() -> None:
    refusal = Refusal(code=RefusalCode.NEEDS_CLEARANCE, message="Please see a doctor first.")
    assert refusal.code is RefusalCode.NEEDS_CLEARANCE


def test_refusal_message_at_the_length_ceiling_is_accepted() -> None:
    Refusal(code=RefusalCode.OUT_OF_SCOPE, message="x" * 1000)


def test_refusal_message_over_the_length_ceiling_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Refusal(code=RefusalCode.OUT_OF_SCOPE, message="x" * 1001)


def test_plan_round_trips_through_json() -> None:
    plan = Plan(
        name="Starter plan",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[Block(kind="single", items=[_prescription()])],
            )
        ],
    )
    restored = Plan.model_validate_json(plan.model_dump_json())
    assert restored == plan


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Load.model_validate({"kind": "bodyweight", "unexpected": True})


def test_load_kg_rejects_nan() -> None:
    """B2: `allow_inf_nan=False` on the domain strict config. A NaN load would otherwise
    compare unequal to everything, including itself, and silently bypass numeric guard
    checks (`proposed_kg > ceiling` is always False when `proposed_kg` is NaN)."""
    with pytest.raises(ValidationError):
        Load.model_validate({"kind": "kg", "kg": float("nan")})


def test_load_kg_rejects_infinity() -> None:
    with pytest.raises(ValidationError):
        Load.model_validate({"kind": "kg", "kg": float("inf")})


def test_load_change_rejects_nan_and_infinity() -> None:
    for bad_value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            LoadChange.model_validate(
                {"exercise_id": "barbell_back_squat", "from_kg": 100.0, "to_kg": bad_value}
            )


def test_load_change_requires_positive_kg_values() -> None:
    with pytest.raises(ValidationError):
        LoadChange.model_validate(
            {"exercise_id": "barbell_back_squat", "from_kg": 0.0, "to_kg": 100.0}
        )
    with pytest.raises(ValidationError):
        LoadChange.model_validate(
            {"exercise_id": "barbell_back_squat", "from_kg": 100.0, "to_kg": -1.0}
        )


def test_load_change_round_trips() -> None:
    change = LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=102.5)
    assert LoadChange.model_validate_json(change.model_dump_json()) == change
