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
from fitme.guards.plan import LOAD_RULES, load_verdicts, reference_load_kg, validate_plan

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

# A "complete" screening (B4): every red flag explicitly answered "no". `validate_plan` now
# runs `screening.plan_allowed` too, which refuses outright if any red flag is missing or
# "unknown" — tests that aren't exercising that behavior need a complete baseline so it
# doesn't mask the thing they're actually checking.
_ALL_RED_FLAGS_NO = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]
# Lower back flagged: its check-in is required before an increase (A§6.5/A§7). The squat is
# then also contraindicated (A§4.4 coverage), which these tests don't mind: they look at the
# check-in verdict specifically.
_LOWER_BACK_FLAGGED = [
    *_ALL_RED_FLAGS_NO,
    ScreeningFlagState(flag=ScreeningFlag.LOWER_BACK_INJURY_CURRENT, value="yes"),
]

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
    ctx = _ctx(
        flags=_LOWER_BACK_FLAGGED,
        history_max_kg={"barbell_back_squat": 100.0},
        checkins={"knee": CheckinAnswer.FINE},
    )
    # The flagged "lower_back" has no entry: treated as unknown, blocks the increase.
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "checkins.increase_allowed" and not v.ok for v in verdicts)


def test_an_increase_on_a_flagged_area_with_a_stale_unknown_checkin_is_blocked() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=102.5))
    ctx = _ctx(
        flags=_LOWER_BACK_FLAGGED,
        history_max_kg={"barbell_back_squat": 100.0},
        checkins={"lower_back": CheckinAnswer.UNKNOWN},  # the latest, after an older "fine"
    )
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "checkins.increase_allowed" and not v.ok for v in verdicts)


def test_an_increase_on_a_flagged_area_with_the_latest_checkin_fine_passes_the_guard() -> None:
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=102.5))
    ctx = _ctx(
        flags=_LOWER_BACK_FLAGGED,
        history_max_kg={"barbell_back_squat": 100.0},
        checkins={"lower_back": CheckinAnswer.FINE},
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    verdicts = validate_plan(plan, ctx)
    assert all(v.ok for v in verdicts if v.rule == "checkins.increase_allowed")
    assert ctx.flagged_areas == frozenset({"lower_back"})


def test_an_increase_on_unflagged_areas_needs_no_checkins() -> None:
    """Nothing flagged, no check-ins at all: the check-in guard passes; the increase is still
    subject to the weekly cap and the ceiling."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=102.5))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 100.0},
        current_load_kg={"barbell_back_squat": 100.0},
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    assert all(v.ok for v in validate_plan(plan, ctx))
    capped = _ctx(
        history_max_kg={"barbell_back_squat": 100.0},
        current_load_kg={"barbell_back_squat": 100.0},
        weekly_cap_kg={"barbell_back_squat": 2.5},
        increases_7d={"barbell_back_squat": [2.5]},
    )
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in validate_plan(plan, capped))


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
    missed entirely (75 < 80, "not an increase"). A +15 kg jump breaches the weekly cap."""
    plan = _plan("barbell_back_squat", Load(kind="kg", kg=75.0))
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 80.0},
        current_load_kg={"barbell_back_squat": 60.0},
    )
    verdicts = validate_plan(plan, ctx)
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in verdicts)


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


def test_a_load_applied_this_week_is_not_counted_as_an_increase_again() -> None:
    """A§7: current 40 (last session), history max 40, and a plan_confirm at 42.5 this week
    (cap already used by it). Keeping 42.5 is no increase: no cap/check-in verdict, only the
    ceiling (42.5 <= 40 + 2.5) — so a later revision doesn't revert it."""
    ctx = _ctx(
        history_max_kg={"barbell_back_squat": 40.0},
        current_load_kg={"barbell_back_squat": 40.0},
        applied_to_kg_7d={"barbell_back_squat": 42.5},
        increases_7d={"barbell_back_squat": [2.5]},
        weekly_cap_kg={"barbell_back_squat": 2.5},
    )
    verdicts = validate_plan(_plan("barbell_back_squat", Load(kind="kg", kg=42.5)), ctx)
    assert all(v.ok for v in verdicts)
    assert not any(v.rule == "progression.weekly_cap" for v in verdicts)
    assert reference_load_kg(ctx, "barbell_back_squat") == 42.5
    # Only the part above the applied load is an increase: 45 is +2.5 on a used cap.
    verdicts = validate_plan(_plan("barbell_back_squat", Load(kind="kg", kg=45.0)), ctx)
    assert any(v.rule == "progression.weekly_cap" and not v.ok for v in verdicts)
    # An applied value below the reference is irrelevant; the ordinary reference stands.
    lower = _ctx(
        history_max_kg={"barbell_back_squat": 100.0},
        current_load_kg={"barbell_back_squat": 60.0},
        applied_to_kg_7d={"barbell_back_squat": 50.0},
    )
    assert reference_load_kg(lower, "barbell_back_squat") == 60.0


def test_a_non_finite_applied_load_fails_closed() -> None:
    for bad in (float("nan"), float("inf"), 0.0, -5.0):
        ctx = _ctx(
            history_max_kg={"barbell_back_squat": 100.0},
            applied_to_kg_7d={"barbell_back_squat": bad},
        )
        verdicts = validate_plan(_plan("barbell_back_squat", Load(kind="kg", kg=90.0)), ctx)
        assert any(v.rule == "plan.reference_load" and not v.ok for v in verdicts), bad


def test_load_rules_cover_every_rule_load_verdicts_can_emit() -> None:
    """A§7.3: `services/` repairs a prescription only when every failing verdict is a load
    rule, so `LOAD_RULES` must name exactly what `load_verdicts` emits — including the
    increase-only checks (weekly cap, check-ins) and the fail-closed reference-load verdict."""
    plans = [
        (Load(kind="kg", kg=200.0), _ctx(history_max_kg={"barbell_back_squat": 100.0})),
        (Load(kind="kg", kg=50.0), _ctx()),
        (
            Load(kind="kg", kg=75.0),
            _ctx(
                history_max_kg={"barbell_back_squat": 100.0},
                current_load_kg={"barbell_back_squat": 60.0},
                increases_7d={"barbell_back_squat": [2.5]},
            ),
        ),
        (
            Load(kind="kg", kg=62.5),
            _ctx(
                history_max_kg={"barbell_back_squat": 100.0},
                current_load_kg={"barbell_back_squat": float("nan")},
            ),
        ),
    ]
    seen: set[str] = set()
    for load, ctx in plans:
        for verdict in load_verdicts(_SQUAT, load, ctx):
            seen.add(verdict.rule)
            assert verdict.rule in LOAD_RULES, verdict.rule
    for verdict in load_verdicts(_PUSHUP, Load(kind="kg", kg=20.0), _ctx()):
        seen.add(verdict.rule)
        assert verdict.rule in LOAD_RULES, verdict.rule
    assert seen == LOAD_RULES
    # And none of the structural rules is a load rule.
    structural = {
        v.rule
        for v in validate_plan(_plan("unicorn_press", Load(kind="calibration")), _ctx())
        if not v.ok
    }
    assert structural and not structural & LOAD_RULES


def test_load_verdicts_match_validate_plan_for_the_same_prescription() -> None:
    load = Load(kind="kg", kg=200.0)
    ctx = _ctx(history_max_kg={"barbell_back_squat": 100.0})
    per_prescription = load_verdicts(_SQUAT, load, ctx)
    from_plan = [
        v for v in validate_plan(_plan("barbell_back_squat", load), ctx) if v.rule in LOAD_RULES
    ]
    assert per_prescription == from_plan


def test_a_kg_load_on_a_non_kg_loadable_exercise_is_a_load_violation() -> None:
    """A§4.4 "non-kg exercises": rejected by `plan.kg_loadable` (a load rule, so the service
    substitutes the engine's bodyweight/calibration rather than spending a retry)."""
    assert _PUSHUP.kg_loadable is False
    verdicts = validate_plan(_plan("pushup", Load(kind="kg", kg=20.0)), _ctx())
    failing = [v for v in verdicts if not v.ok]
    assert [v.rule for v in failing] == ["plan.kg_loadable"]
    assert all(v.ok for v in validate_plan(_plan("pushup", Load(kind="bodyweight")), _ctx()))
