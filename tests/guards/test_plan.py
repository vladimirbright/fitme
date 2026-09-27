"""`guards.plan.validate_plan` (A§7; A§7.4: "a contraindicated exercise is rejected"; "a
non-catalog exercise is rejected"; B1: reference load is the current working load, not the
historical max)."""

from __future__ import annotations

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import (
    RED_FLAGS,
    CheckinAnswer,
    Equipment,
    HealthHoldReason,
    Location,
    ScreeningFlag,
)
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import validate_plan

_SQUAT = Exercise.model_validate(
    {
        "id": "barbell_back_squat",
        "names": {"en": "Barbell back squat"},
        "kind": "compound",
        "pattern": "squat",
        "equipment": ["barbell", "rack"],
        "locations": ["public_gym"],
        "loads_areas": ["knee", "lower_back"],
        "contraindicated_by": ["knee_injury_current"],
        "increment_kg": 2.5,
        "start": {"kind": "kg", "kg": 20.0},
        "instructions": {"en": "..."},
    }
)

_CATALOG = Catalog.model_validate({"exercise": [_SQUAT.model_dump()]})

# A "complete" screening (B4): every red flag explicitly answered "no". `validate_plan` now
# runs `screening.plan_allowed` too, which refuses outright if any red flag is missing or
# "unknown" — tests that aren't exercising that behavior need a complete baseline so it
# doesn't mask the thing they're actually checking.
_ALL_RED_FLAGS_NO = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]

_FINE_CHECKINS = {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE}


def _block(exercise_id: str, load: Load) -> Block:
    return Block(
        kind="single",
        items=[
            Prescription(
                exercise_id=exercise_id,
                sets=3,
                reps_min=5,
                reps_max=8,
                load=load,
                rest_seconds=120,
            )
        ],
    )


def _plan(exercise_id: str, load: Load) -> Plan:
    return Plan(
        name="Test plan",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[Workout(key="A", title="Full body", blocks=[_block(exercise_id, load)])],
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


def test_non_catalog_exercise_is_rejected() -> None:
    plan = _plan("not_a_real_exercise", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx())
    assert any(v.rule == "plan.catalog_id" and not v.ok for v in verdicts)


def test_catalog_exercise_passes_the_catalog_check() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx())
    assert any(v.rule == "plan.catalog_id" and v.ok for v in verdicts)


def test_location_mismatch_is_rejected() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(location=Location.APARTMENT_NO_EQUIPMENT))
    assert any(v.rule == "plan.location_fit" and not v.ok for v in verdicts)


def test_missing_equipment_is_rejected() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(equipment=frozenset()))
    assert any(v.rule == "plan.equipment_fit" and not v.ok for v in verdicts)


def test_contraindicated_exercise_is_rejected() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    flags = [
        *_ALL_RED_FLAGS_NO,
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes"),
    ]
    verdicts = validate_plan(plan, _ctx(flags=flags))
    assert any(v.rule == "screening.exercise_allowed" and not v.ok for v in verdicts)


def test_a_red_flag_missing_clearance_is_reflected_in_validate_plan() -> None:
    """B4: `validate_plan` now runs `screening.plan_allowed` too, not just per-exercise
    contraindications."""
    flags = [
        ScreeningFlagState(flag=ScreeningFlag.HEART_CONDITION, value="yes", clearance=None),
        *[f for f in _ALL_RED_FLAGS_NO if f.flag != ScreeningFlag.HEART_CONDITION],
    ]
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(flags=flags))
    assert any(v.rule == "screening.plan_allowed" and not v.ok for v in verdicts)


def test_incomplete_screening_is_reflected_in_validate_plan() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(flags=[]))
    assert any(v.rule == "screening.incomplete" and not v.ok for v in verdicts)


def test_open_hold_is_reflected_in_validate_plan() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(holds=[HealthHoldReason.STOP_WORD]))
    assert any(v.rule == "screening.plan_allowed" and not v.ok for v in verdicts)


def test_schedule_length_must_match_sessions_per_week() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx(sessions_per_week=3))
    assert any(v.rule == "plan.schedule_length" and not v.ok for v in verdicts)


def test_schedule_referencing_a_missing_workout_key_is_rejected() -> None:
    plan = Plan(
        name="Test plan",
        schedule=[ScheduledDay(weekday=0, workout_key="Z")],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[_block("barbell_back_squat", Load(kind="calibration"))],
            )
        ],
    )
    verdicts = validate_plan(plan, _ctx())
    assert any(v.rule == "plan.schedule_workout_keys" and not v.ok for v in verdicts)


def test_a_valid_plan_has_no_failing_verdicts() -> None:
    plan = _plan("barbell_back_squat", Load(kind="calibration"))
    verdicts = validate_plan(plan, _ctx())
    assert all(v.ok for v in verdicts)


def test_ceiling_violation_in_a_prescription_is_rejected() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=200.0))
    ctx = _ctx(history_max_kg={"barbell_back_squat": 100.0})
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "ceiling.historical_max" and not v.ok for v in verdicts)


def test_kg_proposal_with_no_history_is_rejected_by_the_ceiling_guard() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=50.0))
    verdicts = validate_plan(plan, _ctx())
    assert any(v.rule == "ceiling.historical_max" and not v.ok for v in verdicts)


def test_an_increase_blocked_by_an_unknown_checkin() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=102.5))
    ctx = _ctx(history_max_kg={"barbell_back_squat": 100.0}, checkins={"knee": CheckinAnswer.FINE})
    # "lower_back" has no entry: treated as unknown, blocks the increase.
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "checkins.increase_allowed" and not v.ok for v in verdicts)


def test_an_increase_within_every_check_passes() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=102.5))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 100.0},
        current_load_kg={"barbell_back_squat": 100.0},
        checkins=_FINE_CHECKINS,
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    verdicts = validate_plan(plan, ctx)
    assert all(v.ok for v in verdicts)


def test_a_load_at_or_below_history_skips_the_increase_only_checks() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=90.0))
    ctx = _ctx(history_max_kg={"barbell_back_squat": 100.0})
    verdicts = validate_plan(plan, ctx)
    assert all(v.ok for v in verdicts)
    assert not any(v.rule == "checkins.increase_allowed" for v in verdicts)


# --- B1 reproduced cases: reference load is the *current* working load, not the historical
# max (history max 80 in each, per the reviewer's probe). ---


def test_b1_increase_from_current_60_to_75_with_no_checkins_is_blocked() -> None:
    """Historical max 80, but the current working load is 60: proposing 75 is a real +15 kg
    increase relative to *current*, which the old (wrong) history-max reference would have
    missed entirely (75 < 80, "not an increase"). No check-ins at all blocks it regardless."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=75.0))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 80.0},
        current_load_kg={"barbell_back_squat": 60.0},
    )
    verdicts = validate_plan(plan, ctx)
    assert any(not v.ok for v in verdicts)


def test_b1_increase_to_80_with_the_weekly_cap_already_used_is_blocked() -> None:
    """Current working load 77.5 (below the historical max of 80); proposing 80 is a +2.5 kg
    increase, but the trailing-7-day cap is already fully used."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=80.0))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 80.0},
        current_load_kg={"barbell_back_squat": 77.5},
        checkins=_FINE_CHECKINS,
        increases_7d={"barbell_back_squat": [2.5]},
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in verdicts)


def test_b1_increase_to_82_5_with_fine_checkins_and_cap_already_used_is_blocked() -> None:
    """Current working load 80 (same as the historical max); proposing 82.5 is a +2.5 kg
    increase, fine check-ins, but the cap is already used this week."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=82.5))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 80.0},
        current_load_kg={"barbell_back_squat": 80.0},
        checkins=_FINE_CHECKINS,
        increases_7d={"barbell_back_squat": [2.5]},
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in verdicts)
