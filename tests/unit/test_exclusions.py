"""ADR 0009: hard exclusions — what each kind matches in the shipped catalog."""

from __future__ import annotations

import pytest

from fitme.catalog import load_catalog
from fitme.db.records import ExclusionRecord
from fitme.domain.catalog import LOCATION_DEFAULT_EQUIPMENT
from fitme.domain.enums import ExercisePattern, Experience, Location
from fitme.services import exclusions
from fitme.services.catalog import available_exercises


def _row(kind: str, value: str) -> ExclusionRecord:
    return ExclusionRecord(id=1, user_id=1, kind=kind, value=value, created_at="")


def test_a_pattern_excludes_every_squat_variation_including_lunges() -> None:
    catalog = load_catalog()
    excluded = exclusions.resolve([_row("pattern", "squat")], catalog)
    assert {"barbell_back_squat", "dumbbell_goblet_squat", "dumbbell_reverse_lunge"} <= excluded
    assert all(catalog.by_id(i).pattern == ExercisePattern.SQUAT for i in excluded)  # type: ignore[union-attr]


def test_a_name_word_excludes_a_family_in_either_language() -> None:
    catalog = load_catalog()
    lunges = exclusions.resolve([_row("name", "lunge")], catalog)
    assert "dumbbell_reverse_lunge" in lunges and "barbell_back_squat" not in lunges
    # The Russian word folds the same way ("выпады" ⊃ "выпад").
    stored = exclusions.preview("name", "Выпад", catalog)
    assert stored.value == "выпад" and "dumbbell_reverse_lunge" in stored.exercise_ids


def test_excluded_exercises_are_never_offered() -> None:
    catalog = load_catalog()
    excluded = exclusions.resolve([_row("pattern", "squat")], catalog)
    allowed = available_exercises(
        catalog,
        Location.PUBLIC_GYM,
        LOCATION_DEFAULT_EQUIPMENT[Location.PUBLIC_GYM],
        [],
        Experience.GT_5Y,
        excluded=excluded,
    )
    assert allowed and not any(exercise.pattern == ExercisePattern.SQUAT for exercise in allowed)


@pytest.mark.parametrize(
    ("kind", "value"),
    [("pattern", "legs"), ("exercise", "unicorn_press"), ("name", "ab"), ("colour", "red")],
)
def test_an_exclusion_must_name_something_real(kind: str, value: str) -> None:
    with pytest.raises(exclusions.InvalidExclusionError):
        exclusions.preview(kind, value, load_catalog())
