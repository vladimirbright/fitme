"""ADR 0017 through the real `plan_generate` / `plan_revise` pipeline: what the request says
about the plan (its activity, days, length, place) frames what the model is given, and an
understood request leaves the profile's physical data only.

The owner's report: "легкую беговую по субботам на свежем воздухе" came back as a run plus a
Romanian deadlift at 65 kg, and "bjj два раза в неделю" as three barbell days."""

from __future__ import annotations

import json

from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.domain.enums import BlockRole, Location, Measure, Quality
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.services import planning
from tests.integration.db.test_planning_service import FakeLlm, _saved_plan_with_squat_history

_RUN_REQUEST = (
    "Давай разок в неделю по субботам легкую беговую на свежем воздухе. "
    "Мне надо понять как разминаться, как бежать и все такое"
)


def _run_plan() -> Plan:
    def timed(exercise_id: str, sets: int, seconds: int, rest: int) -> Prescription:
        return Prescription(
            exercise_id=exercise_id,
            sets=sets,
            measure=Measure.TIME,
            seconds=seconds,
            load=Load(kind="bodyweight"),
            rest_seconds=rest,
        )

    return Plan(
        name="Saturday run",
        schedule=[ScheduledDay(weekday=5, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                qualities=[Quality.CONDITIONING, Quality.MOBILITY],
                title="Easy run",
                blocks=[
                    Block(
                        kind="single",
                        role=BlockRole.WARM_UP,
                        items=[timed("mobility_runners_stretch", 2, 30, 30)],
                    ),
                    Block(
                        kind="single",
                        role=BlockRole.CONDITIONING,
                        items=[timed("easy_jog", 4, 480, 120)],
                    ),
                ],
            )
        ],
    )


async def test_an_outdoor_run_request_frames_the_plan_and_drops_the_gym_profile(
    db: Database, user_id: int
) -> None:
    await _saved_plan_with_squat_history(db, user_id)  # strength focus, barbell squat history
    llm = FakeLlm([_run_plan(), _run_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id, guidance=_RUN_REQUEST)

    assert result.plan is not None
    payload = json.loads(llm.prompts[-1])
    context = payload["context"]
    brief = payload["programming_brief"]
    assert brief["activity"] == {"name": "running", "mode": "practice"}
    assert brief["weekdays"] == [5]
    assert brief["sessions_per_week"] == 1
    assert brief["setting"] == "outdoors"
    assert brief["main_qualities"] == ["conditioning", "mobility"]
    assert payload["requested_qualities"] == ["conditioning", "mobility"]
    # Physical data only: no strength focus or weight-training preference, no squat history.
    assert context["focus"] is None and context["preferences"] == []
    assert context["weight_bucket"] == "80_89"
    assert all(entry["exercise_id"] != "barbell_back_squat" for entry in context["history"])
    # Outdoors: nothing that needs a barbell, a rack or a bench.
    catalog = load_catalog()
    assert context["location"] == Location.OUTDOOR.value
    assert "easy_jog" in context["allowed_exercise_ids"]
    for exercise_id in context["allowed_exercise_ids"]:
        exercise = catalog.by_id(exercise_id)
        assert exercise is not None
        assert Location.OUTDOOR in exercise.locations and not exercise.equipment


async def test_a_request_that_names_no_goal_keeps_the_profile_frame(
    db: Database, user_id: int
) -> None:
    await _saved_plan_with_squat_history(db, user_id)
    llm = FakeLlm([_run_plan(), _run_plan()])

    await planning.propose_new_plan(db, llm.runtime(), user_id, guidance="второй план")

    payload = json.loads(llm.prompts[0])
    assert payload["context"]["focus"] == "strength"
    assert payload["context"]["preferences"] == ["weight_training"]
    assert "activity" not in payload["programming_brief"]
    assert payload["programming_brief"]["sessions_per_week"] == 2  # the profile's default
    assert any(
        entry["exercise_id"] == "barbell_back_squat" for entry in payload["context"]["history"]
    )


async def test_bjj_twice_a_week_is_two_supporting_sessions_not_the_profile_split(
    db: Database, user_id: int
) -> None:
    await _saved_plan_with_squat_history(db, user_id)
    llm = FakeLlm([_run_plan(), _run_plan()])

    await planning.propose_new_plan(db, llm.runtime(), user_id, guidance="bjj два раза в неделю")

    payload = json.loads(llm.prompts[0])
    brief = payload["programming_brief"]
    assert brief["activity"] == {"name": "grappling", "mode": "support"}
    assert brief["sessions_per_week"] == 2
    assert payload["context"]["focus"] is None
    assert "core_stability" in payload["requested_qualities"]


async def test_a_revision_that_names_no_goal_keeps_the_plans_own_objectives(
    db: Database, user_id: int
) -> None:
    """ "убери скакалку" on a running draft: the plan's declared objectives, not the profile's
    strength focus, are what the revision keeps working toward — and its own review flags
    a workout that brings strength back in."""
    await _saved_plan_with_squat_history(db, user_id)
    llm = FakeLlm([_run_plan(), _run_plan()])
    draft = await planning.propose_new_plan(db, llm.runtime(), user_id, guidance=_RUN_REQUEST)

    revised = FakeLlm([_run_plan()])
    await planning.revise_plan(
        db, revised.runtime(), user_id, planning.DraftBase(draft.decision_id), "убери скакалку"
    )

    payload = json.loads(revised.prompts[0])
    assert payload["requested_qualities"] == ["conditioning", "mobility"]
    assert payload["context"]["focus"] is None
