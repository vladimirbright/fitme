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


def test_applicable_picks_every_plan_rows_plus_the_plan_worked_on() -> None:
    """ADR 0010: a plan-scoped exclusion applies to its plan only, a new-plan one to the new
    plan being drafted only; an `all` one everywhere."""
    rows = [
        ExclusionRecord(id=1, user_id=1, kind="name", value="squat", created_at=""),
        ExclusionRecord(
            id=2, user_id=1, kind="name", value="lunge", created_at="", scope="plan", plan_id=7
        ),
        ExclusionRecord(
            id=3, user_id=1, kind="name", value="kettlebell", created_at="", scope="new_plan"
        ),
    ]

    def ids(**kwargs: object) -> list[int]:
        return [row.id for row in exclusions.applicable(rows, **kwargs)]  # type: ignore[arg-type]

    assert ids(plan_id=7) == [1, 2]
    assert ids(plan_id=8) == [1]
    assert ids(plan_id=None, new_plan=True) == [1, 3]
    assert ids(plan_id=None) == [1]


def test_the_word_squat_is_far_narrower_than_the_squat_pattern() -> None:
    """The observed failure: "убери приседания" stored `pattern:squat`, 92 exercises with
    every lunge, step-up and leg press. The word keeps to squats."""
    catalog = load_catalog()
    word = set(exclusions.preview("name", "squat", catalog).exercise_ids)
    pattern = set(exclusions.preview("pattern", "squat", catalog).exercise_ids)
    assert "barbell_back_squat" in word
    assert not {"dumbbell_reverse_lunge", "dumbbell_step_up", "machine_leg_press"} & word
    assert len(word) < len(pattern)
