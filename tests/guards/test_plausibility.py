"""`guards.plausibility` (M7): an implausible parsed load is rejected before it can reach
`set_logs` and raise the historical max / the ceiling."""

from __future__ import annotations

import math

import pytest

from fitme.domain.catalog import Exercise
from fitme.domain.models import Load
from fitme.domain.results import SetResult
from fitme.guards.plausibility import check_parsed_load, check_parsed_results


def _exercise(
    exercise_id: str = "barbell_back_squat",
    *,
    load_unit: str = "total",
    kg_loadable: bool = True,
    increment_kg: float = 2.5,
) -> Exercise:
    return Exercise.model_validate(
        {
            "id": exercise_id,
            "names": {"en": exercise_id},
            "kind": "compound" if kg_loadable else "bodyweight",
            "pattern": "squat",
            "equipment": ["dumbbells"] if load_unit != "total" else ["barbell", "rack"],
            "load_unit": load_unit,
            "kg_loadable": kg_loadable,
            "locations": ["public_gym"],
            "loads_areas": ["knee"],
            "contraindicated_by": ["knee_injury_current"],
            "increment_kg": increment_kg,
            "start": {"kind": "kg", "kg": 20.0} if kg_loadable else {"kind": "bodyweight"},
            "instructions": {"en": "n/a"},
        }
    )


_KG_42_5 = Load(kind="kg", kg=42.5)


def test_no_load_reported_is_plausible() -> None:
    assert check_parsed_load(_exercise(), _KG_42_5, None).ok


def test_a_typo_ten_times_the_prescription_is_rejected() -> None:
    verdict = check_parsed_load(_exercise(), _KG_42_5, 425.0)
    assert not verdict.ok
    assert verdict.rule == "plausibility.parsed_load"


def test_more_than_twice_the_prescription_is_rejected() -> None:
    assert not check_parsed_load(_exercise(), _KG_42_5, 90.0).ok


def test_more_than_three_increments_above_is_rejected_even_under_twice() -> None:
    # 42.5 + 3 × 2.5 = 50: 52.5 is under 2× (85) but over the increment bound.
    assert not check_parsed_load(_exercise(), _KG_42_5, 52.5).ok
    assert check_parsed_load(_exercise(), _KG_42_5, 50.0).ok


def test_a_combined_dumbbell_total_on_a_per_implement_exercise_is_rejected() -> None:
    dumbbell = _exercise("dumbbell_bench_press", load_unit="per_implement", increment_kg=1.0)
    prescribed = Load(kind="kg", kg=2.0)
    # 4 kg is exactly 2× and within 2 + 3 × 1 = 5, so only the total-pattern rule catches it.
    assert not check_parsed_load(dumbbell, prescribed, 4.0).ok
    assert check_parsed_load(dumbbell, prescribed, 3.0).ok


def test_exactly_twice_on_a_total_exercise_is_still_within_the_ratio_bound() -> None:
    # A barbell load of 2× the prescription is caught by the increment rule here, not by the
    # per-implement pattern rule: 10 + 3 × 2.5 = 17.5 < 20.
    assert not check_parsed_load(_exercise(), Load(kind="kg", kg=10.0), 20.0).ok
    # ... but a total exercise with a large increment is not flagged as a "combined total".
    big_step = _exercise(increment_kg=10.0)
    assert check_parsed_load(big_step, Load(kind="kg", kg=10.0), 20.0).ok


def test_any_kg_on_a_non_kg_loadable_exercise_is_rejected() -> None:
    pushup = _exercise("pushup", kg_loadable=False)
    assert not check_parsed_load(pushup, Load(kind="bodyweight"), 10.0).ok
    assert check_parsed_load(pushup, Load(kind="bodyweight"), None).ok


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0.0, -5.0])
def test_non_finite_or_non_positive_fails_closed(value: float) -> None:
    assert not check_parsed_load(_exercise(), _KG_42_5, value).ok


def test_a_calibration_prescription_without_history_has_no_ratio_reference() -> None:
    assert check_parsed_load(_exercise(), Load(kind="calibration"), 60.0).ok
    assert check_parsed_load(_exercise(), Load(kind="calibration"), 60.0, history_max_kg=None).ok


def test_a_calibration_prescription_uses_the_historical_max_as_the_reference() -> None:
    # 425 for "42,5" on a calibration block with a 40 kg history: caught by the ratio rule.
    assert not check_parsed_load(
        _exercise(), Load(kind="calibration"), 425.0, history_max_kg=40.0
    ).ok
    # 125 for "12,5" is inside the absolute bound: the ratio rule against the history catches it.
    verdict = check_parsed_load(_exercise(), Load(kind="calibration"), 125.0, history_max_kg=40.0)
    assert not verdict.ok and "historical max" in verdict.detail
    assert not check_parsed_load(
        _exercise(), Load(kind="calibration"), 90.0, history_max_kg=40.0
    ).ok
    assert not check_parsed_load(
        _exercise(), Load(kind="calibration"), 50.0, history_max_kg=40.0
    ).ok
    assert check_parsed_load(_exercise(), Load(kind="calibration"), 47.5, history_max_kg=40.0).ok
    assert check_parsed_load(_exercise(), Load(kind="bodyweight"), 47.5, history_max_kg=40.0).ok
    # A garbage history value is ignored rather than trusted.
    assert check_parsed_load(
        _exercise(), Load(kind="calibration"), 60.0, history_max_kg=math.nan
    ).ok


@pytest.mark.parametrize(
    ("load_unit", "value", "ok"),
    [
        ("total", 300.0, True),
        ("total", 300.5, False),
        ("total", 425.0, False),
        ("per_implement", 60.0, True),
        ("per_implement", 60.5, False),
        ("single_implement", 61.0, False),
    ],
)
def test_absolute_bounds_apply_even_in_calibration(load_unit: str, value: float, ok: bool) -> None:
    exercise = _exercise("x", load_unit=load_unit, increment_kg=100.0)
    assert check_parsed_load(exercise, Load(kind="calibration"), value).ok is ok
    # ... and against a prescription that would otherwise allow the value.
    prescribed = Load(kind="kg", kg=value)
    assert check_parsed_load(exercise, prescribed, value).ok is ok
    assert not check_parsed_load(
        _exercise("pushup", kg_loadable=False), Load(kind="calibration"), 5.0
    ).ok


def test_check_parsed_results_runs_over_every_set() -> None:
    sets = [
        SetResult(set_index=1, reps=8, load_kg=42.5),
        SetResult(set_index=2, reps=8, load_kg=425.0),
        SetResult(set_index=3, skipped=True),
    ]
    verdicts = check_parsed_results(_exercise(), _KG_42_5, sets)
    assert [v.ok for v in verdicts] == [True, False, True]
