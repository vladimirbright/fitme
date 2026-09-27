"""`services.catalog.available_exercises` (A§4.4, IMPLEMENTATION_PLAN M3)."""

from __future__ import annotations

from fitme.catalog import load_catalog
from fitme.domain.catalog import HOME_SELECTABLE_EQUIPMENT, LOCATION_DEFAULT_EQUIPMENT
from fitme.domain.enums import Equipment, ExercisePattern, Location, ScreeningFlag
from fitme.domain.screening import ScreeningFlagState
from fitme.services.catalog import available_exercises

# A§4.4 "honest patterns": coverage is per location. `apartment_no_equipment` doesn't
# require `vertical_pull` — a genuine vertical pull needs a bar, which isn't available
# there by definition; a plan for that location compensates with horizontal pulls instead.
_REQUIRED_PATTERNS_BY_LOCATION: dict[Location, tuple[ExercisePattern, ...]] = {
    location: (
        ExercisePattern.SQUAT,
        ExercisePattern.HINGE,
        ExercisePattern.HORIZONTAL_PUSH,
        ExercisePattern.HORIZONTAL_PULL,
        ExercisePattern.VERTICAL_PUSH,
        ExercisePattern.VERTICAL_PULL,
        ExercisePattern.CORE,
        ExercisePattern.MOBILITY,
        ExercisePattern.CONDITIONING,
    )
    for location in Location
} | {
    Location.APARTMENT_NO_EQUIPMENT: (
        ExercisePattern.SQUAT,
        ExercisePattern.HINGE,
        ExercisePattern.HORIZONTAL_PUSH,
        ExercisePattern.HORIZONTAL_PULL,
        ExercisePattern.CORE,
        ExercisePattern.MOBILITY,
        ExercisePattern.CONDITIONING,
    )
}


def _flag(flag: ScreeningFlag, value: str = "yes") -> ScreeningFlagState:
    return ScreeningFlagState(flag=flag, value=value)  # type: ignore[arg-type]


def _equipment_for(location: Location) -> frozenset[Equipment]:
    if location == Location.HOME_EQUIPMENT:
        return HOME_SELECTABLE_EQUIPMENT
    return LOCATION_DEFAULT_EQUIPMENT[location]


def test_every_location_has_a_workable_full_body_set_with_no_flags() -> None:
    """IMPLEMENTATION_PLAN M3 acceptance: every location has at least one exercise per
    required movement pattern when no screening flags are active."""
    catalog = load_catalog()
    for location in Location:
        exercises = available_exercises(catalog, location, _equipment_for(location), [])
        patterns_present = {exercise.pattern for exercise in exercises}
        required = _REQUIRED_PATTERNS_BY_LOCATION[location]
        missing = [p.value for p in required if p not in patterns_present]
        assert not missing, f"{location.value} is missing pattern(s): {missing}"


def test_apartment_no_equipment_does_not_require_vertical_pull() -> None:
    assert (
        ExercisePattern.VERTICAL_PULL
        not in _REQUIRED_PATTERNS_BY_LOCATION[Location.APARTMENT_NO_EQUIPMENT]
    )


def test_available_exercises_excludes_exercises_missing_required_equipment() -> None:
    catalog = load_catalog()
    exercises = available_exercises(catalog, Location.HOME_EQUIPMENT, frozenset(), [])
    assert all(not exercise.equipment for exercise in exercises)


def test_available_exercises_excludes_exercises_not_offered_at_the_location() -> None:
    catalog = load_catalog()
    exercises = available_exercises(catalog, Location.APARTMENT_NO_EQUIPMENT, frozenset(), [])
    assert all(Location.APARTMENT_NO_EQUIPMENT in exercise.locations for exercise in exercises)
    # a gym-only barbell lift must not appear at an apartment with no equipment
    assert "barbell_back_squat" not in {exercise.id for exercise in exercises}


def test_knee_injury_flag_removes_the_barbell_back_squat() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.KNEE_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_back_squat" not in ids


def test_shoulder_injury_flag_removes_overhead_pressing() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.SHOULDER_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_overhead_press" not in ids
    assert "pike_pushup" not in ids


def test_lower_back_injury_flag_removes_spinal_loading() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.LOWER_BACK_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_deadlift" not in ids
    assert "barbell_good_morning" not in ids
    assert "barbell_back_squat" not in ids
    assert "barbell_overhead_press" not in ids  # B1: standing OHP now loads lower_back too


def test_neck_injury_flag_removes_neck_loading_exercises() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.NECK_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_back_squat" not in ids
    assert "barbell_good_morning" not in ids
    assert "barbell_overhead_press" not in ids
    assert "pike_pushup" not in ids


def test_hip_injury_flag_removes_hip_loading_exercises() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.HIP_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_back_squat" not in ids
    assert "barbell_deadlift" not in ids
    assert "kettlebell_swing" not in ids


def test_elbow_wrist_injury_flag_removes_rowing_exercises() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.ELBOW_WRIST_INJURY_CURRENT)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_bent_over_row" not in ids
    assert "dumbbell_row" not in ids


def test_hernia_flag_removes_valsalva_prone_lifts() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.HERNIA)]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    ids = {exercise.id for exercise in exercises}
    assert "barbell_deadlift" not in ids
    assert "barbell_back_squat" not in ids
    assert "kettlebell_swing" not in ids


def test_flag_answered_no_does_not_remove_the_exercise() -> None:
    catalog = load_catalog()
    flags = [_flag(ScreeningFlag.KNEE_INJURY_CURRENT, value="no")]
    exercises = available_exercises(catalog, Location.PUBLIC_GYM, frozenset(Equipment), flags)
    assert "barbell_back_squat" in {exercise.id for exercise in exercises}
