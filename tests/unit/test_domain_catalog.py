"""`domain/catalog.py` model and validation (A§4.4, IMPLEMENTATION_PLAN M2). Loading the TOML
file is M3; this only exercises the pydantic model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.models import Load


def _exercise(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "id": "barbell_back_squat",
        "names": {"en": "Barbell back squat", "ru": "Приседания со штангой на спине"},
        "kind": "compound",
        "pattern": "squat",
        "equipment": ["barbell", "rack"],
        "locations": ["public_gym", "studio_gym"],
        "loads_areas": ["knee", "lower_back", "hip"],
        "contraindicated_by": ["knee_injury_current", "lower_back_injury_current", "hernia"],
        "increment_kg": 2.5,
        "start": {"kind": "kg", "kg": 20.0},
        "instructions": {"en": "Bar on the back rack.", "ru": "Штанга на спине."},
    }
    defaults.update(overrides)
    return defaults


def test_exercise_defaults_load_step_kg_to_2_5_for_barbell_equipment() -> None:
    exercise = Exercise.model_validate(_exercise())
    assert exercise.load_step_kg == 2.5


def test_exercise_defaults_load_step_kg_to_1_0_without_barbell() -> None:
    exercise = Exercise.model_validate(
        _exercise(
            id="dumbbell_row",
            equipment=["dumbbells"],
            loads_areas=["lower_back"],
            contraindicated_by=[],
        )
    )
    assert exercise.load_step_kg == 1.0


def test_exercise_honors_an_explicit_load_step_kg() -> None:
    exercise = Exercise.model_validate(_exercise(load_step_kg=1.25))
    assert exercise.load_step_kg == 1.25


def test_exercise_start_is_a_load_model() -> None:
    exercise = Exercise.model_validate(_exercise())
    assert exercise.start == Load(kind="kg", kg=20.0)


def test_catalog_lookup_by_id() -> None:
    catalog = Catalog.model_validate({"exercise": [_exercise()]})
    found = catalog.by_id("barbell_back_squat")
    assert found is not None
    assert found.id == "barbell_back_squat"
    assert catalog.by_id("nonexistent") is None
    assert catalog.ids == frozenset({"barbell_back_squat"})


def test_catalog_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        Catalog.model_validate({"exercise": [_exercise(), _exercise()]})


def test_exercise_rejects_unknown_equipment_value() -> None:
    with pytest.raises(ValidationError):
        Exercise.model_validate(_exercise(equipment=["not_a_real_equipment"]))


def test_exercise_rejects_an_unknown_loads_area() -> None:
    with pytest.raises(ValidationError, match="unknown loads_areas"):
        Exercise.model_validate(_exercise(loads_areas=["not_a_real_area"]))


def test_kg_capable_exercise_requires_non_empty_loads_areas() -> None:
    """A kg-capable exercise (start.kind == "kg") with no declared loads_areas would let
    `checkins.increase_allowed` gate an increase against nothing at all — reject it at the
    catalog level instead."""
    with pytest.raises(ValidationError, match="loads_areas"):
        Exercise.model_validate(_exercise(loads_areas=[]))


def test_bodyweight_start_does_not_require_loads_areas() -> None:
    exercise = Exercise.model_validate(
        _exercise(
            id="plank",
            loads_areas=[],
            contraindicated_by=[],
            start={"kind": "bodyweight"},
        )
    )
    assert exercise.loads_areas == []
