"""`services.plan_edit` (A§9.1 structured plan editing; M9 review fix B5): the check-in gate,
a kg load on a non-kg-loadable exercise, and the absolute plausibility bounds always block the
save; only the weekly cap and the historical-max ceiling are overridable warnings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import RED_FLAGS, Equipment, Location, ScreeningFlag
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.services.plan_edit import blocking_errors, load_cap_warnings

_SQUAT = Exercise.model_validate(
    {
        "id": "barbell_back_squat",
        "names": {"en": "Barbell back squat"},
        "kind": "compound",
        "pattern": "squat",
        "equipment": ["barbell", "rack"],
        "locations": ["public_gym"],
        "loads_areas": ["knee", "lower_back"],
        "contraindicated_by": ["knee_injury_current", "lower_back_injury_current"],
        "increment_kg": 2.5,
        "start": {"kind": "kg", "kg": 20.0},
        "instructions": {"en": "..."},
    }
)
_PUSHUP = Exercise.model_validate(
    {
        "id": "pushup",
        "names": {"en": "Push-up"},
        "kind": "bodyweight",
        "pattern": "horizontal_push",
        "equipment": [],
        "locations": ["public_gym"],
        "loads_areas": ["shoulder", "elbow_wrist"],
        "contraindicated_by": ["shoulder_injury_current", "elbow_wrist_injury_current"],
        "increment_kg": 2.5,
        "start": {"kind": "bodyweight"},
        "instructions": {"en": "..."},
    }
)
_CATALOG = Catalog.model_validate({"exercise": [_SQUAT.model_dump(), _PUSHUP.model_dump()]})
_ALL_RED_FLAGS_NO = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]
_LOWER_BACK_FLAGGED = [
    *_ALL_RED_FLAGS_NO,
    ScreeningFlagState(flag=ScreeningFlag.LOWER_BACK_INJURY_CURRENT, value="yes"),
]


def _plan(exercise_id: str, load: Load) -> Plan:
    prescription = Prescription(
        exercise_id=exercise_id, sets=3, reps_min=5, reps_max=8, load=load, rest_seconds=120
    )
    return Plan(
        name="Test plan",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[Block(kind="single", items=[prescription])],
            )
        ],
    )


def _ctx(**overrides: object) -> GuardContext:
    defaults: dict[str, object] = {
        "catalog": _CATALOG,
        "flags": _ALL_RED_FLAGS_NO,
        "location": Location.PUBLIC_GYM,
        "equipment": frozenset({Equipment.BARBELL, Equipment.RACK}),
        "sessions_per_week": 1,
    }
    defaults.update(overrides)
    return GuardContext(**defaults)  # type: ignore[arg-type]


def test_checkin_gate_failure_is_a_blocking_error() -> None:
    """B5(a): an unanswered check-in on a flagged area blocks an edited increase outright —
    it must appear in `blocking_errors`, never be silently dropped."""
    # A small increase, at the ceiling and within the weekly cap, so the *only* failure is
    # the check-in gate — isolating it from the (legitimate, independent) ceiling/weekly-cap
    # warnings a bigger increase would also trigger.
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=61.0))
    ctx = _ctx(
        flags=_LOWER_BACK_FLAGGED,
        current_load_kg={"barbell_back_squat": 60.0},
        history_max_kg={"barbell_back_squat": 60.0},
        checkins={},  # lower_back is flagged but unanswered: silence is not consent
    )
    errors = blocking_errors(plan, ctx)
    warnings = load_cap_warnings(plan, ctx)
    assert any("lower_back" in error for error in errors)
    assert warnings == {}  # isolated: within the cap and the ceiling


def test_kg_on_a_non_kg_loadable_exercise_is_a_blocking_error() -> None:
    """B5(a): a kg load on an exercise that can never take one (bodyweight push-up) blocks
    the save, never a warning a checkbox can wave through."""
    plan = _plan("pushup", Load(kind="kg", kg=20.0))
    ctx = _ctx()
    errors = blocking_errors(plan, ctx)
    warnings = load_cap_warnings(plan, ctx)
    assert errors  # blocked
    assert warnings == {}  # never surfaced as an overridable warning


def test_load_itself_rejects_a_non_finite_kg() -> None:
    """`Load`'s own model config (`allow_inf_nan=False`) already refuses inf/nan at
    construction — the web form-parsing layer (B1) builds every `Load` through
    `Load.model_validate`, so a bogus "inf"/"nan" string is caught there and never reaches
    this module as an in-memory object at all."""
    with pytest.raises(ValidationError):
        Load(kind="kg", kg=float("inf"))


def test_non_finite_kg_is_a_blocking_error_as_defense_in_depth() -> None:
    """`_plausibility_bound_errors` still checks `math.isfinite` itself, as defense in depth
    for a `Load` built by any path that bypasses validation (`model_construct`)."""
    bad_load = Load.model_construct(kind="kg", kg=float("inf"))
    plan = _plan("barbell_back_squat", bad_load)
    ctx = _ctx(
        current_load_kg={"barbell_back_squat": 60.0}, history_max_kg={"barbell_back_squat": 60.0}
    )
    errors = blocking_errors(plan, ctx)
    assert any("finite" in error for error in errors)


def test_implausibly_large_kg_is_a_blocking_error() -> None:
    """B5(a): the absolute plausibility bound, exactly like `guards.plausibility` — a huge
    number is a typo, never something a confirm checkbox should be able to save."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=1_000_000.0))
    ctx = _ctx(
        current_load_kg={"barbell_back_squat": 60.0}, history_max_kg={"barbell_back_squat": 60.0}
    )
    errors = blocking_errors(plan, ctx)
    assert any("plausible bound" in error for error in errors)


def test_ceiling_and_weekly_cap_are_overridable_warnings_only() -> None:
    """The ceiling and the weekly cap are the *only* overridable failures (A§9.1): they show
    up as warnings, and do not appear in `blocking_errors`."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=100.0))  # far over the ceiling
    ctx = _ctx(
        current_load_kg={"barbell_back_squat": 60.0}, history_max_kg={"barbell_back_squat": 60.0}
    )
    warnings = load_cap_warnings(plan, ctx)
    errors = blocking_errors(plan, ctx)
    assert "barbell_back_squat" in warnings
    assert not any(
        rule_hint in error for error in errors for rule_hint in ("ceiling", "weekly cap")
    )


def test_a_plan_with_no_guard_failures_has_no_errors_or_warnings() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    ctx = _ctx()
    assert blocking_errors(plan, ctx) == []
    assert load_cap_warnings(plan, ctx) == {}
