"""`guards.experience`: an `advanced` catalog exercise needs 2+ years of training; unknown
experience fails closed; the plan guard and the allowed-set filter both apply it."""

from __future__ import annotations

import pytest

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import Equipment, ExerciseLevel, Experience, Location
from fitme.domain.models import Load, Prescription
from fitme.guards.context import GuardContext
from fitme.guards.experience import RULE, exercise_level_allowed
from fitme.guards.plan import prescription_verdicts
from fitme.services.catalog import available_exercises


def _exercise(level: str) -> Exercise:
    return Exercise.model_validate(
        {
            "id": f"{level}_pull",
            "names": {"en": "Pull"},
            "kind": "bodyweight",
            "level": level,
            "pattern": "vertical_pull",
            "equipment": ["pull_up_bar"],
            "locations": ["public_gym"],
            "loads_areas": ["shoulder", "elbow_wrist"],
            "contraindicated_by": ["shoulder_injury_current", "elbow_wrist_injury_current"],
            "increment_kg": 2.5,
            "start": {"kind": "bodyweight"},
            "instructions": {"en": "..."},
        }
    )


@pytest.mark.parametrize(
    "experience", [None, Experience.NONE, Experience.LT_6M, Experience.M6_TO_2Y]
)
def test_an_advanced_exercise_is_blocked_without_two_years(experience: Experience | None) -> None:
    verdict = exercise_level_allowed(_exercise("advanced"), experience)
    assert verdict.ok is False and verdict.rule == RULE


@pytest.mark.parametrize("experience", [Experience.Y2_TO_5Y, Experience.GT_5Y])
def test_an_advanced_exercise_is_allowed_with_two_years(experience: Experience) -> None:
    assert exercise_level_allowed(_exercise("advanced"), experience).ok is True


def test_a_general_exercise_needs_no_experience() -> None:
    assert exercise_level_allowed(_exercise("general"), None).ok is True
    assert _exercise("general").level == ExerciseLevel.GENERAL


def test_the_plan_guard_rejects_an_advanced_prescription_for_a_novice() -> None:
    advanced = _exercise("advanced")
    ctx = GuardContext(
        catalog=Catalog(exercise=[advanced]),
        flags=[],
        equipment=frozenset({Equipment.PULL_UP_BAR}),
        location=Location.PUBLIC_GYM,
        sessions_per_week=3,
        experience=Experience.LT_6M,
    )
    prescription = Prescription(
        exercise_id=advanced.id,
        sets=3,
        reps_min=3,
        reps_max=5,
        rest_seconds=90,
        load=Load(kind="bodyweight"),
    )
    failed = [v for v in prescription_verdicts(prescription, ctx) if not v.ok]
    assert [v.rule for v in failed] == [RULE]


def test_the_allowed_set_leaves_advanced_out_unless_experienced() -> None:
    catalog = Catalog(exercise=[_exercise("advanced"), _exercise("general")])
    kit = frozenset({Equipment.PULL_UP_BAR})
    novice = available_exercises(catalog, Location.PUBLIC_GYM, kit, [])
    veteran = available_exercises(catalog, Location.PUBLIC_GYM, kit, [], Experience.GT_5Y)
    assert [e.id for e in novice] == ["general_pull"]
    assert [e.id for e in veteran] == ["advanced_pull", "general_pull"]
