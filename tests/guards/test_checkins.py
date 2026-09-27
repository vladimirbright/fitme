"""`guards.checkins.increase_allowed` (A§7, A§7.4: "an unknown check-in blocks an increase";
ALSO REQUIRED: empty `loads_areas` fails closed)."""

from __future__ import annotations

from fitme.domain.catalog import Exercise
from fitme.domain.enums import CheckinAnswer
from fitme.guards.checkins import increase_allowed


def _squat() -> Exercise:
    return Exercise.model_validate(
        {
            "id": "barbell_back_squat",
            "names": {"en": "Barbell back squat"},
            "kind": "compound",
            "equipment": ["barbell", "rack"],
            "locations": ["public_gym"],
            "loads_areas": ["knee", "lower_back"],
            "increment_kg": 2.5,
            "start": {"kind": "kg", "kg": 20.0},
            "instructions": {"en": "..."},
        }
    )


def test_unknown_checkin_blocks_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.UNKNOWN, "lower_back": CheckinAnswer.FINE}
    )
    assert verdict.ok is False


def test_missing_checkin_for_a_loaded_area_is_treated_as_unknown() -> None:
    verdict = increase_allowed(_squat(), {"lower_back": CheckinAnswer.FINE})
    assert verdict.ok is False


def test_worse_checkin_blocks_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.WORSE, "lower_back": CheckinAnswer.FINE}
    )
    assert verdict.ok is False


def test_pain_checkin_blocks_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.PAIN, "lower_back": CheckinAnswer.FINE}
    )
    assert verdict.ok is False


def test_all_fine_checkins_allow_an_increase() -> None:
    verdict = increase_allowed(
        _squat(), {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE}
    )
    assert verdict.ok is True


def test_an_exercise_with_no_loaded_areas_fails_closed() -> None:
    """An exercise reaching this guard with empty `loads_areas` has nothing to check an
    increase against; that must block, not silently allow (fail closed)."""
    exercise = Exercise.model_validate(
        {
            "id": "plank",
            "names": {"en": "Plank"},
            "kind": "bodyweight",
            "equipment": [],
            "locations": ["public_gym"],
            "increment_kg": 1.0,
            "start": {"kind": "bodyweight"},
            "instructions": {"en": "..."},
        }
    )
    assert increase_allowed(exercise, {}).ok is False
