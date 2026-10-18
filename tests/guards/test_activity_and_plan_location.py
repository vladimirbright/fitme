"""Non-catalog activity items (`activity:<slug>`) and a plan's own location
(`Plan.location`) in `guards.plan` and the `Prescription` model (A§4.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.catalog import load_catalog
from fitme.domain.enums import (
    RED_FLAGS,
    Equipment,
    HealthHoldReason,
    Location,
    Measure,
    ScreeningFlag,
)
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import (
    MAX_ACTIVITY_SECONDS,
    MAX_SET_SECONDS,
    Block,
    Load,
    Plan,
    Prescription,
    ScheduledDay,
    Workout,
    is_activity_id,
)
from fitme.domain.results import SetResult
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import (
    at_location,
    load_verdicts,
    prescription_verdicts,
    validate_plan,
    validate_workout,
)
from fitme.guards.plausibility import check_parsed_seconds

_ALL_RED_FLAGS_NO = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]


def _ctx(**overrides: object) -> GuardContext:
    defaults: dict[str, object] = {
        "catalog": load_catalog(),
        "flags": _ALL_RED_FLAGS_NO,
        "location": Location.PUBLIC_GYM,
        "equipment": frozenset(Equipment),
        "sessions_per_week": 2,
    }
    defaults.update(overrides)
    return GuardContext(**defaults)  # type: ignore[arg-type]


def _activity(**overrides: object) -> Prescription:
    fields: dict[str, object] = {
        "exercise_id": "activity:bjj_class",
        "activity_name": "BJJ class",
        "sets": 1,
        "measure": "time",
        "seconds": 5400,
        "load": {"kind": "bodyweight"},
        "rest_seconds": 0,
    }
    fields.update(overrides)
    return Prescription.model_validate(fields)


def _timed(exercise_id: str, seconds: int = 1200) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        sets=1,
        measure=Measure.TIME,
        seconds=seconds,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
    )


def _plan(*items: Prescription, location: Location | None = None) -> Plan:
    return Plan(
        name="Test plan",
        location=location,
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Session",
                blocks=[Block(kind="single", items=[item]) for item in items],
            )
        ],
    )


def _failures(verdicts: list[GuardVerdict]) -> list[GuardVerdict]:
    return [verdict for verdict in verdicts if not verdict.ok]


# --- Model ------------------------------------------------------------------------------------


def test_activity_id_namespace() -> None:
    assert is_activity_id("activity:bjj_class")
    assert is_activity_id("activity:run")
    assert not is_activity_id("bjj_class")
    assert not is_activity_id("activity:ab")  # slug too short
    assert not is_activity_id("activity:BJJ")  # upper case
    assert not is_activity_id("activity:bjj-class")
    assert not is_activity_id("activity:" + "a" * 61)


def test_no_catalog_exercise_uses_the_activity_namespace() -> None:
    """The guards skip catalog checks for an `activity:` id, so no catalog id may be one."""
    assert not [e.id for e in load_catalog().exercise if e.id.startswith("activity")]


def test_a_90_minute_class_fits_one_timed_set() -> None:
    assert MAX_ACTIVITY_SECONDS >= 5400
    assert _activity().seconds == 5400


def test_an_activity_item_is_exactly_one_set() -> None:
    """A class is one session: "BJJ class" as 10 × 90 min is refused, naming the field."""
    with pytest.raises(ValidationError, match="sets: an activity: item is exactly one set"):
        _activity(sets=2)
    with pytest.raises(ValidationError, match="sets"):
        _activity(sets=10)
    assert _activity(sets=1).sets == 1


def test_three_hours_is_for_activity_items_only() -> None:
    """A catalog exercise's timed set is at most an hour (`MAX_SET_SECONDS`); only an
    `activity:` item may run to three (`MAX_ACTIVITY_SECONDS`)."""
    assert MAX_SET_SECONDS == 3600 and MAX_ACTIVITY_SECONDS == 10800
    assert _timed("easy_jog", 3600).seconds == 3600  # an hour's run still fits
    with pytest.raises(ValidationError, match="seconds: a catalog exercise"):
        _timed("easy_jog", 3601)
    with pytest.raises(ValidationError, match="seconds"):
        _timed("plank", 10800)
    assert _activity(seconds=10800).seconds == 10800
    with pytest.raises(ValidationError, match="less than or equal to 10800"):
        _activity(seconds=10801)


def test_activity_needs_a_name() -> None:
    with pytest.raises(ValidationError, match="activity_name"):
        _activity(activity_name=None)
    with pytest.raises(ValidationError, match="activity_name"):
        _activity(activity_name="   ")


def test_activity_must_be_timed() -> None:
    with pytest.raises(ValidationError, match="time"):
        _activity(measure="reps", seconds=None, reps_min=5, reps_max=8)


def test_activity_takes_no_kg_load() -> None:
    with pytest.raises(ValidationError, match="bodyweight"):
        _activity(load={"kind": "kg", "kg": 20.0})
    with pytest.raises(ValidationError, match="bodyweight"):
        _activity(load={"kind": "calibration"})


def test_activity_takes_no_declared_kg() -> None:
    with pytest.raises(ValidationError, match="declared_kg"):
        _activity(declared_kg=40.0)


def test_a_90_minute_result_validates() -> None:
    assert SetResult(set_index=1, seconds=6000).seconds == 6000


def test_activity_takes_no_rir() -> None:
    with pytest.raises(ValidationError, match="rir"):
        _activity(rir=2)


def test_activity_name_only_on_activity_items() -> None:
    with pytest.raises(ValidationError, match="activity_name"):
        Prescription.model_validate(
            {
                "exercise_id": "easy_jog",
                "activity_name": "Jog",
                "sets": 1,
                "measure": "time",
                "seconds": 600,
                "load": {"kind": "bodyweight"},
                "rest_seconds": 0,
            }
        )


def test_activity_name_is_trimmed() -> None:
    assert _activity(activity_name="  BJJ  ").activity_name == "BJJ"
    assert len(_activity(activity_name="x" * 100).activity_name or "") == 60


def test_a_plan_body_stored_before_activities_and_location_still_parses() -> None:
    body = {
        "name": "Old plan",
        "schedule": [{"weekday": 0, "workout_key": "A"}],
        "workouts": [
            {
                "key": "A",
                "title": "Full body",
                "blocks": [
                    {
                        "kind": "single",
                        "items": [
                            {
                                "exercise_id": "barbell_back_squat",
                                "sets": 3,
                                "reps_min": 5,
                                "reps_max": 8,
                                "load": {"kind": "kg", "kg": 60.0},
                                "rest_seconds": 120,
                                "note": None,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    plan = Plan.model_validate(body)
    assert plan.location is None
    assert plan.workouts[0].blocks[0].items[0].activity_name is None


# --- Guards -----------------------------------------------------------------------------------


def test_an_activity_item_is_accepted_for_a_gym_profile() -> None:
    verdicts = validate_plan(_plan(_activity()), _ctx())
    assert not _failures(verdicts)
    activity = [v for v in verdicts if v.rule == "plan.activity"]
    assert len(activity) == 1 and activity[0].ok
    assert not any(v.rule == "plan.catalog_id" for v in verdicts)


def test_an_activity_item_is_refused_when_an_area_is_flagged() -> None:
    """AGENTS.md §2: a free activity has no contraindication data to check a flag against."""
    knee = [
        *_ALL_RED_FLAGS_NO,
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="yes"),
    ]
    verdicts = validate_plan(_plan(_activity()), _ctx(flags=knee))
    failed = [v for v in verdicts if v.rule == "plan.activity" and not v.ok]
    assert failed and "knee_injury_current" in failed[0].detail
    no_flag = [
        *_ALL_RED_FLAGS_NO,
        ScreeningFlagState(flag=ScreeningFlag.KNEE_INJURY_CURRENT, value="no"),
    ]
    assert not _failures(validate_plan(_plan(_activity()), _ctx(flags=no_flag)))


def test_an_activity_item_still_needs_the_screening_gate() -> None:
    verdicts = validate_plan(_plan(_activity()), _ctx(holds=[HealthHoldReason.STOP_WORD]))
    assert any(v.rule == "screening.plan_allowed" and not v.ok for v in verdicts)
    incomplete = validate_plan(_plan(_activity()), _ctx(flags=[]))
    assert any(v.rule == "screening.incomplete" and not v.ok for v in incomplete)


def test_an_unknown_bare_id_is_still_refused() -> None:
    verdicts = validate_plan(_plan(_timed("bjj_class")), _ctx())
    failures = _failures(verdicts)
    assert [v.rule for v in failures] == ["plan.catalog_id"]


def test_an_activity_item_is_judged_in_a_workout_too() -> None:
    verdicts = validate_workout(_plan(_activity()).workouts[0], _ctx())
    assert not _failures(verdicts)
    assert any(v.rule == "plan.activity" for v in verdicts)


def test_load_verdicts_need_a_catalog_exercise() -> None:
    """`load_verdicts` takes an `Exercise`; activity items never reach it."""
    verdicts = prescription_verdicts(_activity(), _ctx())
    assert [v.rule for v in verdicts] == ["plan.activity"]
    jog = load_catalog().by_id("easy_jog")
    assert jog is not None
    assert all(v.ok for v in load_verdicts(jog, Load(kind="bodyweight"), _ctx()))


def test_an_outdoor_only_exercise_fails_at_the_gym_without_a_plan_location() -> None:
    verdicts = validate_plan(_plan(_timed("outdoor_interval_jog")), _ctx())
    assert any(v.rule == "plan.location_fit" and not v.ok for v in verdicts)


def test_an_outdoor_plan_location_lets_an_outdoor_exercise_pass() -> None:
    plan = _plan(_timed("outdoor_interval_jog"), location=Location.OUTDOOR)
    assert not _failures(validate_plan(plan, _ctx()))


def test_a_plan_location_also_refuses_what_does_not_fit_there() -> None:
    plan = _plan(
        Prescription(
            exercise_id="barbell_back_squat",
            sets=3,
            reps_min=5,
            reps_max=8,
            load=Load(kind="calibration"),
            rest_seconds=120,
        ),
        location=Location.OUTDOOR,
    )
    assert any(v.rule == "plan.location_fit" and not v.ok for v in validate_plan(plan, _ctx()))


def test_validate_workout_uses_the_given_plan_location() -> None:
    workout = _plan(_timed("outdoor_interval_jog")).workouts[0]
    assert _failures(validate_workout(workout, _ctx()))
    assert not _failures(validate_workout(workout, _ctx(), location=Location.OUTDOOR))


# --- Equipment follows the plan's location -----------------------------------------------------


def _squat() -> Prescription:
    return Prescription(
        exercise_id="barbell_back_squat",
        sets=3,
        reps_min=5,
        reps_max=8,
        load=Load(kind="calibration"),
        rest_seconds=120,
    )


def _rules(verdicts: list[GuardVerdict], *rules: str) -> list[GuardVerdict]:
    return [verdict for verdict in verdicts if verdict.rule in rules]


def test_a_gym_plan_of_an_outdoor_profile_gets_the_gyms_equipment() -> None:
    """The bad case: an outdoor profile's kit (a pull-up bar) refused every barbell item of
    a plan whose own location is a public gym."""
    outdoor = _ctx(location=Location.OUTDOOR, equipment=frozenset({Equipment.PULL_UP_BAR}))
    profile_only = _rules(
        validate_plan(_plan(_squat()), outdoor), "plan.equipment_fit", "plan.location_fit"
    )
    assert profile_only and not all(verdict.ok for verdict in profile_only)
    at_gym = validate_plan(_plan(_squat(), location=Location.PUBLIC_GYM), outdoor)
    fit = _rules(at_gym, "plan.equipment_fit", "plan.location_fit")
    assert len(fit) == 2 and all(verdict.ok for verdict in fit)
    assert not _failures(at_gym)
    assert at_location(outdoor, Location.PUBLIC_GYM).equipment == frozenset(Equipment)


def test_an_outdoor_plan_of_a_home_gym_profile_gets_the_outdoor_kit() -> None:
    """A home profile with dumbbells: its outdoor plan has the park's kit, not the dumbbells."""
    home = _ctx(
        location=Location.HOME_EQUIPMENT,
        equipment=frozenset({Equipment.DUMBBELLS}),
        home_equipment=frozenset({Equipment.DUMBBELLS}),
    )
    outdoor = at_location(home, Location.OUTDOOR)
    assert outdoor.location == Location.OUTDOOR
    assert outdoor.equipment == frozenset({Equipment.PULL_UP_BAR})
    pull_up = Prescription(
        exercise_id="pull_up",
        sets=3,
        reps_min=3,
        reps_max=6,
        load=Load(kind="bodyweight"),
        rest_seconds=120,
    )
    catalog = load_catalog()
    exercise = catalog.by_id("pull_up")
    assert exercise is not None and Location.OUTDOOR in exercise.locations
    assert Equipment.PULL_UP_BAR in exercise.equipment
    # The home profile has no bar: refused there, fine on its outdoor plan.
    assert _rules(validate_plan(_plan(pull_up), home), "plan.equipment_fit")[0].ok is False
    outdoor_plan = validate_plan(_plan(pull_up, location=Location.OUTDOOR), home)
    assert _rules(outdoor_plan, "plan.equipment_fit")[0].ok


def test_a_home_plan_of_a_gym_profile_gets_the_users_home_selection() -> None:
    gym = _ctx(home_equipment=frozenset({Equipment.DUMBBELLS}))
    home = at_location(gym, Location.HOME_EQUIPMENT)
    assert home.equipment == frozenset({Equipment.DUMBBELLS})
    squat = validate_plan(_plan(_squat(), location=Location.HOME_EQUIPMENT), gym)
    missing = _rules(squat, "plan.equipment_fit")
    assert missing and not missing[0].ok and "barbell" in missing[0].detail
    # No home selection at all: nothing beyond bodyweight (fail closed).
    assert at_location(_ctx(), Location.HOME_EQUIPMENT).equipment == frozenset()


def test_a_parsed_catalog_time_above_an_hour_is_implausible() -> None:
    """A logged result may be a 90-minute class (`SetResult` allows the activity maximum),
    but a catalog exercise's logged set is held to `MAX_SET_SECONDS`."""
    jog = load_catalog().by_id("easy_jog")
    assert jog is not None
    hour = check_parsed_seconds(jog, [SetResult(set_index=1, seconds=3600)])
    assert len(hour) == 1 and hour[0].ok
    over = check_parsed_seconds(
        jog, [SetResult(set_index=1, seconds=3601), SetResult(set_index=2, skipped=True)]
    )
    assert len(over) == 1 and not over[0].ok
    assert over[0].rule == "plausibility.parsed_seconds" and "3600" in over[0].detail
    assert check_parsed_seconds(jog, [SetResult(set_index=1, reps=10)]) == []
