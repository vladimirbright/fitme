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
            load_unit="single_implement",
            loads_areas=["lower_back"],
            contraindicated_by=[],
        )
    )
    assert exercise.load_step_kg == 1.0


def test_exercise_defaults_load_unit_to_total_without_dumbbells_or_kettlebell() -> None:
    assert Exercise.model_validate(_exercise()).load_unit == "total"
    bodyweight = _exercise(id="pushup", equipment=[], start={"kind": "bodyweight"})
    assert Exercise.model_validate(bodyweight).load_unit == "total"


def test_exercise_requires_an_explicit_load_unit_for_dumbbells_and_kettlebell() -> None:
    """A§4.4 "loads are per implement": a dumbbell/kettlebell exercise must say what its kg
    means; the model refuses a missing value instead of defaulting silently."""
    for equipment in (["dumbbells"], ["kettlebell"], ["dumbbells", "bench"]):
        with pytest.raises(ValidationError, match="load_unit"):
            Exercise.model_validate(_exercise(equipment=equipment))
        exercise = Exercise.model_validate(
            _exercise(equipment=equipment, load_unit="per_implement")
        )
        assert exercise.load_unit == "per_implement"


def test_exercise_rejects_an_unknown_load_unit() -> None:
    with pytest.raises(ValidationError):
        Exercise.model_validate(_exercise(load_unit="per_hand"))


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


def test_kg_loadable_is_derived_from_kind_pattern_and_start() -> None:
    assert Exercise.model_validate(_exercise()).kg_loadable is True  # barbell, kg start
    calibration = _exercise(
        id="machine_leg_press", equipment=["machine"], start={"kind": "calibration"}
    )
    assert Exercise.model_validate(calibration).kg_loadable is True
    bodyweight = _exercise(id="pushup", equipment=[], start={"kind": "bodyweight"})
    assert Exercise.model_validate(bodyweight).kg_loadable is False
    cardio = _exercise(
        id="machine_rower",
        kind="cardio",
        pattern="conditioning",
        equipment=["machine"],
        start={"kind": "calibration"},
    )
    assert Exercise.model_validate(cardio).kg_loadable is False
    mobility = _exercise(
        id="cat_cow",
        kind="mobility",
        pattern="mobility",
        equipment=[],
        start={"kind": "bodyweight"},
    )
    assert Exercise.model_validate(mobility).kg_loadable is False


def test_band_only_exercises_are_not_kg_loadable() -> None:
    """A§4.4: a band's load isn't a kg number. Only *load* equipment counts: a band plus a
    pull-up bar is still band-only, while a band plus a dumbbell is not."""
    band = _exercise(id="band_row", equipment=["resistance_bands"], start={"kind": "calibration"})
    assert Exercise.model_validate(band).kg_loadable is False
    band_and_bar = _exercise(
        id="band_lat_pulldown",
        equipment=["resistance_bands", "pull_up_bar"],
        start={"kind": "calibration"},
    )
    assert Exercise.model_validate(band_and_bar).kg_loadable is False
    band_and_dumbbell = _exercise(
        id="banded_dumbbell_press",
        equipment=["resistance_bands", "dumbbells"],
        load_unit="per_implement",
        start={"kind": "calibration"},
    )
    assert Exercise.model_validate(band_and_dumbbell).kg_loadable is True


def test_kg_loadable_explicit_override_and_consistency_rules() -> None:
    swing = _exercise(
        id="kettlebell_swing",
        kind="cardio",
        pattern="conditioning",
        equipment=["kettlebell"],
        load_unit="single_implement",
        start={"kind": "kg", "kg": 8.0},
    )
    with pytest.raises(ValidationError, match="kg_loadable"):
        Exercise.model_validate(swing)  # derived false, but a kg start: contradiction
    assert Exercise.model_validate({**swing, "kg_loadable": True}).kg_loadable is True
    with pytest.raises(ValidationError, match="loads_areas"):
        Exercise.model_validate(_exercise(kg_loadable=True, loads_areas=[], contraindicated_by=[]))
