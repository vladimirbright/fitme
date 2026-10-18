"""Stored plan and workout bodies stay readable by `main` (the models before activity items
and plan locations, all `extra="forbid"`): `domain.models.storage_body` leaves `location`
and `activity_name` out while they are unset, and every write of a body goes through it.
`MainPlan` below is an ad-hoc copy of `main`'s field lists (6aa2bf5
`src/fitme/domain/models.py`), so any new key that leaks into a stored body fails here."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fitme.db.connection import Database
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.plans import list_plan_versions
from fitme.domain.enums import Location, Measure
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    Prescription,
    ScheduledDay,
    Workout,
    storage_body,
)
from fitme.services import planning
from tests.integration.db.test_activity_plans import outdoor_plan, seed_gym_profile
from tests.integration.db.test_planning_service import _settings

_STRICT = ConfigDict(extra="forbid")


class MainLoad(BaseModel):
    model_config = _STRICT
    kind: Literal["kg", "bodyweight", "calibration"]
    kg: Annotated[float, Field(gt=0)] | None = None


class MainPrescription(BaseModel):
    model_config = _STRICT
    exercise_id: str
    sets: Annotated[int, Field(ge=1, le=10)]
    measure: str = "reps"
    reps_min: Annotated[int, Field(ge=1, le=50)] | None = None
    reps_max: Annotated[int, Field(ge=1, le=50)] | None = None
    seconds: Annotated[int, Field(ge=5, le=3600)] | None = None  # main's MAX_SET_SECONDS
    meters: Annotated[int, Field(ge=5, le=2000)] | None = None
    rir: Annotated[int, Field(ge=0, le=5)] | None = None
    load: MainLoad
    rest_seconds: Annotated[int, Field(ge=0, le=600)]
    note: str | None = None
    declared_kg: float | None = None


class MainBlock(BaseModel):
    model_config = _STRICT
    kind: Literal["single", "superset"]
    role: str | None = None
    items: list[MainPrescription]


class MainWorkout(BaseModel):
    model_config = _STRICT
    key: str
    qualities: list[str] = Field(default_factory=list)
    title: str
    blocks: list[MainBlock]


class MainScheduledDay(BaseModel):
    model_config = _STRICT
    weekday: int
    workout_key: str


class MainPlan(BaseModel):
    model_config = _STRICT
    name: str
    schedule: list[MainScheduledDay]
    workouts: list[MainWorkout]


def _catalog_plan() -> Plan:
    """No activity item, no location: what every plan before the branch looked like."""
    return Plan(
        name="Gym",
        schedule=[ScheduledDay(weekday=0, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[
                    Block(
                        kind="single",
                        items=[
                            Prescription(
                                exercise_id="pushup",
                                sets=3,
                                reps_min=8,
                                reps_max=10,
                                load=Load(kind="bodyweight"),
                                rest_seconds=90,
                            )
                        ],
                    ),
                    Block(
                        kind="single",
                        items=[
                            Prescription(
                                exercise_id="plank",
                                sets=3,
                                measure=Measure.TIME,
                                seconds=45,
                                load=Load(kind="bodyweight"),
                                rest_seconds=60,
                            )
                        ],
                    ),
                ],
            )
        ],
    )


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for item in value.values() for k in _keys(item)}
    if isinstance(value, list):
        return {k for item in value for k in _keys(item)}
    return set()


def test_a_catalog_only_plan_is_stored_in_mains_shape() -> None:
    body: Any = storage_body(_catalog_plan())
    assert "location" not in _keys(body) and "activity_name" not in _keys(body)
    MainPlan.model_validate(body)
    # The other optional fields keep their explicit nulls.
    assert body["workouts"][0]["blocks"][0]["items"][0]["note"] is None
    assert Plan.model_validate(body) == _catalog_plan()


def test_a_plan_with_a_location_and_an_activity_keeps_both_and_round_trips() -> None:
    plan = outdoor_plan(location=Location.OUTDOOR)
    body: Any = storage_body(plan)
    assert body["location"] == "outdoor"
    items = [i for w in body["workouts"] for b in w["blocks"] for i in b["items"]]
    assert [i.get("activity_name") for i in items] == [None, None, "BJJ class"]
    assert "activity_name" not in items[0]
    assert Plan.model_validate(body) == plan


def test_a_catalog_timed_set_stays_within_mains_limit() -> None:
    """`main` caps a timed set at 3600 s. The branch holds a catalog exercise to the same
    bound (only an `activity:` item, which `main` cannot read anyway, may run longer), so a
    catalog-only plan with the longest catalog timed set still parses on `main`."""
    walk: dict[str, Any] = {
        "exercise_id": "brisk_walk",
        "sets": 1,
        "measure": "time",
        "seconds": 3600,
        "load": {"kind": "bodyweight"},
        "rest_seconds": 0,
    }
    plan = _catalog_plan()
    plan.workouts[0].blocks.append(Block(kind="single", items=[Prescription.model_validate(walk)]))
    MainPlan.model_validate(storage_body(plan))
    with pytest.raises(ValidationError, match="seconds"):
        Prescription.model_validate({**walk, "seconds": 5400})


def test_a_workout_is_stored_without_unset_activity_names() -> None:
    body = storage_body(_catalog_plan().workouts[0])
    assert "activity_name" not in _keys(body)
    MainWorkout.model_validate(body)


async def test_a_confirmed_catalog_plan_and_its_draft_are_readable_by_main(
    db: Database, user_id: int
) -> None:
    await seed_gym_profile(db, user_id)
    edit = await planning.record_edit_draft(
        db, _settings(), user_id, plan=_catalog_plan(), plan_id=None, user_report={"test": True}
    )
    assert edit.round is not None, edit.errors
    confirmed = await planning.confirm_plan(db, _settings(), user_id, edit.round.decision_id)
    assert confirmed.plan_id is not None
    async with db.read() as conn:
        versions = await list_plan_versions(conn, confirmed.plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
    MainPlan.model_validate(versions[0].body)
    draft = next(d for d in decisions if d.kind == "plan_revise")
    confirm = next(d for d in decisions if d.kind == "plan_confirm")
    for decision in (draft, confirm):
        assert decision.proposal is not None
        MainPlan.model_validate(decision.proposal["plan"])
