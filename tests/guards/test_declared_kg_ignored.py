"""M8b: `Prescription.declared_kg` is a display-only hint from a pasted plan. The guards must
never read it — a huge declared value changes no verdict — and no guard or load-engine source
so much as mentions it (a scan, so a future rule can't quietly start trusting it)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import RED_FLAGS, CheckinAnswer, Equipment, Location
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import load_verdicts, prescription_verdicts, validate_plan

_SRC = Path(__file__).resolve().parents[2] / "src" / "fitme"

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
_CATALOG = Catalog.model_validate({"exercise": [_SQUAT.model_dump()]})
_FLAGS = [ScreeningFlagState(flag=flag, value="no") for flag in RED_FLAGS]


def _ctx(**overrides: object) -> GuardContext:
    base: dict[str, object] = {
        "catalog": _CATALOG,
        "flags": _FLAGS,
        "equipment": frozenset({Equipment.BARBELL, Equipment.RACK}),
        "location": Location.PUBLIC_GYM,
        "sessions_per_week": 1,
        "checkins": {"knee": CheckinAnswer.FINE, "lower_back": CheckinAnswer.FINE},
    }
    base.update(overrides)
    return GuardContext(**base)  # type: ignore[arg-type]


def _plan(load: Load, declared_kg: float | None) -> Plan:
    return Plan(
        name="P",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="A",
                blocks=[
                    Block(
                        kind="single",
                        items=[
                            Prescription(
                                exercise_id=_SQUAT.id,
                                sets=3,
                                reps_min=5,
                                reps_max=5,
                                load=load,
                                rest_seconds=90,
                                declared_kg=declared_kg,
                            )
                        ],
                    )
                ],
            )
        ],
    )


_CONTEXTS = {
    "no_history": _ctx(),
    "history_40": _ctx(history_max_kg={_SQUAT.id: 40.0}, current_load_kg={_SQUAT.id: 40.0}),
    "cap_used": _ctx(
        history_max_kg={_SQUAT.id: 40.0},
        current_load_kg={_SQUAT.id: 40.0},
        increases_7d={_SQUAT.id: [2.5]},
    ),
    "checkin_unknown": _ctx(
        history_max_kg={_SQUAT.id: 40.0},
        current_load_kg={_SQUAT.id: 40.0},
        checkins={"knee": CheckinAnswer.UNKNOWN},
    ),
}
_LOADS = [
    Load(kind="calibration"),
    Load(kind="bodyweight"),
    Load(kind="kg", kg=40.0),
    Load(kind="kg", kg=42.5),
    Load(kind="kg", kg=80.0),
]


@pytest.mark.parametrize("ctx_name", sorted(_CONTEXTS))
@pytest.mark.parametrize("load", _LOADS, ids=lambda load: f"{load.kind}:{load.kg}")
@pytest.mark.parametrize("declared_kg", [0.5, 42.5, 299.0])
def test_declared_kg_changes_no_verdict(ctx_name: str, load: Load, declared_kg: float) -> None:
    ctx = _CONTEXTS[ctx_name]
    plain = _plan(load, None)
    hinted = _plan(load, declared_kg)
    assert validate_plan(plain, ctx) == validate_plan(hinted, ctx)
    plain_item = plain.workouts[0].blocks[0].items[0]
    hinted_item = hinted.workouts[0].blocks[0].items[0]
    assert prescription_verdicts(plain_item, ctx) == prescription_verdicts(hinted_item, ctx)
    assert load_verdicts(_SQUAT, hinted_item.load, ctx) == load_verdicts(_SQUAT, load, ctx)


def test_no_history_rejects_a_kg_load_whatever_the_declared_hint_says() -> None:
    """The bad case (AGENTS.md §2): a declared 80 kg is not history. The ceiling still blocks
    a kg prescription without a logged max, and a calibration load still passes."""
    ctx = _CONTEXTS["no_history"]
    verdicts = validate_plan(_plan(Load(kind="kg", kg=80.0), 80.0), ctx)
    assert any(v.rule == "ceiling.historical_max" and not v.ok for v in verdicts)
    verdicts = validate_plan(_plan(Load(kind="calibration"), 80.0), ctx)
    assert all(v.ok for v in verdicts)


def test_guards_and_load_engine_sources_never_mention_declared_loads() -> None:
    files = sorted(_SRC.glob("guards/**/*.py")) + [_SRC / "services" / "loads.py"]
    assert files
    for path in files:
        assert "declared" not in path.read_text(encoding="utf-8").casefold(), path
