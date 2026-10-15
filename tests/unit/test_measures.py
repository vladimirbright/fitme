"""ADR 0009: prescription measures — the model's per-measure target rules, and the
deterministic repair of a prescription in a measure its exercise doesn't accept."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fitme.catalog import load_catalog
from fitme.domain.enums import Measure
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.results import SetResult
from fitme.services.measures import (
    MEASURE_FIT_RULE,
    MEASURE_SUBSTITUTED_RULE,
    repair_measures,
)


def _reps(exercise_id: str, reps: tuple[int, int] = (1, 1), **extra: object) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        sets=3,
        reps_min=reps[0],
        reps_max=reps[1],
        load=Load(kind="bodyweight"),
        rest_seconds=60,
        **extra,  # type: ignore[arg-type]
    )


def _plan(*items: Prescription) -> Plan:
    return Plan(
        name="P",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(key="A", title="A", blocks=[Block(kind="single", items=[i]) for i in items])
        ],
    )


def test_a_prescription_carries_exactly_its_measures_target() -> None:
    timed = Prescription(
        exercise_id="plank",
        sets=3,
        measure=Measure.TIME,
        seconds=40,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
    )
    assert timed.reps_min is None and timed.seconds == 40
    with pytest.raises(ValidationError):  # time without seconds
        Prescription.model_validate({**timed.model_dump(), "seconds": None})
    with pytest.raises(ValidationError):  # time with reps as well
        Prescription.model_validate({**timed.model_dump(), "reps_min": 1, "reps_max": 1})
    with pytest.raises(ValidationError):  # distance without meters
        Prescription.model_validate({**timed.model_dump(), "measure": "distance"})
    # A body stored before ADR 0009 has no measure: it is a reps prescription.
    legacy = {
        "exercise_id": "barbell_bench_press",
        "sets": 3,
        "reps_min": 6,
        "reps_max": 8,
        "load": {"kind": "calibration"},
        "rest_seconds": 120,
    }
    assert Prescription.model_validate(legacy).measure == Measure.REPS


def test_holds_intervals_and_carries_in_reps_are_moved_to_their_measure() -> None:
    """The observed failures: "Plank 3 × 1", "Battle ropes 4 × 1", "Farmer carry 3 × 1"."""
    plan = _plan(
        _reps("plank"),
        _reps("battle_rope_alternating_waves"),
        Prescription(
            exercise_id="dumbbell_farmer_carry",
            sets=3,
            reps_min=1,
            reps_max=1,
            load=Load(kind="calibration"),
            rest_seconds=90,
        ),
        _reps("machine_rower"),
        _reps("pushup", (8, 12)),
    )

    fired = repair_measures(plan, load_catalog())

    items = [block.items[0] for block in plan.workouts[0].blocks]
    assert (items[0].measure, items[0].seconds) == (Measure.TIME, 30)
    assert (items[1].measure, items[1].seconds) == (Measure.TIME, 30)
    assert (items[2].measure, items[2].meters) == (Measure.DISTANCE, 20)
    assert (items[3].measure, items[3].seconds) == (Measure.TIME, 600)  # a cardio machine
    assert (items[4].measure, items[4].reps_min, items[4].reps_max) == (Measure.REPS, 8, 12)
    assert all(item.reps_min is None for item in items[:4])
    rules = [verdict.rule for verdict in fired]
    assert rules.count(MEASURE_FIT_RULE) == 4 and rules.count(MEASURE_SUBSTITUTED_RULE) == 4
    for item in items:  # every repaired prescription is still a valid one
        Prescription.model_validate(item.model_dump())


def test_a_fitting_plan_is_left_alone() -> None:
    plan = _plan(_reps("barbell_bench_press", (6, 8)))
    before = plan.model_copy(deep=True)
    assert repair_measures(plan, load_catalog()) == []
    assert plan == before


def test_a_set_result_reports_exactly_one_measure() -> None:
    assert SetResult(set_index=1, seconds=40).seconds == 40
    assert SetResult(set_index=1, meters=30, load_kg=20.0).meters == 30
    with pytest.raises(ValidationError):
        SetResult(set_index=1)  # nothing reported
    with pytest.raises(ValidationError):
        SetResult(set_index=1, reps=8, seconds=40)
    with pytest.raises(ValidationError):
        SetResult(set_index=1, skipped=True, seconds=40)
