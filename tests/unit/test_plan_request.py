"""ADR 0017: reading a plan request (`services.plan_request`) and the review finding for a
workout whose objective the request didn't ask for. The texts are the owner's own."""

from __future__ import annotations

import pytest

from fitme.catalog import load_catalog
from fitme.domain.enums import Activity, Location, Quality
from fitme.domain.models import Block, Plan, Prescription, ScheduledDay, Workout
from fitme.services import plan_request
from fitme.services.plan_quality import ReviewContext, review

_CATALOG = load_catalog()

_RUN = (
    "Давай разок в недель по субботам легкую беговую на свежем воздухе. "
    "Мне надо понять как разминаться, как бежать и все такое"
)
_RUN_REVISION = (
    "ну че за глупость? как румынка для беговой тренировки? откуда ты взял? я хочу выйти на "
    "улицу, размятся, побегать часок, замятся и пойти домой"
)


def test_the_saturday_run_is_a_weekly_outdoor_run_not_a_mobility_and_strength_plan() -> None:
    request = plan_request.read(_RUN)
    assert request.activity == Activity.RUNNING
    assert request.practice  # "разминаться" is the warm-up, not another goal
    assert request.qualities == (Quality.CONDITIONING, Quality.MOBILITY)
    assert request.sessions == 1
    assert request.weekdays == (5,)
    assert request.outdoor
    assert request.understood
    assert "easy_jog" in request.exercises


def test_an_hour_of_running_outdoors() -> None:
    request = plan_request.read(_RUN_REVISION)
    assert request.activity == Activity.RUNNING and request.practice
    assert request.session_minutes == 60
    assert request.outdoor


def test_bjj_twice_a_week_is_grappling_support_two_sessions() -> None:
    request = plan_request.read("bjj два раза в неделю")
    assert request.activity == Activity.GRAPPLING
    assert not request.practice
    assert request.sessions == 2
    assert request.qualities[:3] == (
        Quality.STRENGTH,
        Quality.MUSCULAR_ENDURANCE,
        Quality.CORE_STABILITY,
    )
    assert request.exercises == ()  # the catalog holds no BJJ practice


def test_strength_for_a_runner_is_support_with_the_named_goal_first() -> None:
    request = plan_request.read("силовая для бегуна 3 раза в неделю по 45 мин")
    assert request.activity == Activity.RUNNING and not request.practice
    assert request.qualities[0] == Quality.STRENGTH
    assert request.sessions == 3 and request.session_minutes == 45


@pytest.mark.parametrize(
    "text",
    ["убери скакалку", "второй план", "часто устаю, средний темп", "box jumps please"],
)
def test_text_naming_no_activity_or_goal_is_not_understood(text: str) -> None:
    request = plan_request.read(text)
    assert request.activity is None
    assert request.sessions is None and request.session_minutes is None
    assert not request.weekdays and not request.outdoor


def test_a_negated_place_or_activity_does_not_count() -> None:
    request = plan_request.read("не на улице, без бега")
    assert not request.outdoor and request.activity is None


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("полчаса", 30), ("полтора часа", 90), ("1,5 часа", 90), ("40 минут", 40), ("часок", 60)],
)
def test_session_length(text: str, minutes: int) -> None:
    assert plan_request.read(f"бег {text}").session_minutes == minutes


def test_the_activity_exercises_exist_and_can_be_done_outdoors() -> None:
    for ids in plan_request.ACTIVITY_EXERCISES.values():
        for exercise_id in ids:
            exercise = _CATALOG.by_id(exercise_id)
            assert exercise is not None, exercise_id
            assert Location.OUTDOOR in exercise.locations


def test_a_revision_falls_back_to_the_plans_own_objectives_main_first() -> None:
    declared = [[Quality.CONDITIONING, Quality.MOBILITY], [Quality.STRENGTH]]
    assert plan_request.plan_qualities(declared) == (
        Quality.CONDITIONING,
        Quality.STRENGTH,
        Quality.MOBILITY,
    )


def _timed(exercise_id: str, seconds: int) -> Prescription:
    return Prescription.model_validate(
        {
            "exercise_id": exercise_id,
            "sets": 3,
            "measure": "time",
            "seconds": seconds,
            "load": {"kind": "bodyweight"},
            "rest_seconds": 60,
        }
    )


def _run_plan(qualities: list[Quality]) -> Plan:
    return Plan(
        name="Run",
        schedule=[ScheduledDay(weekday=5, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Run",
                qualities=qualities,
                blocks=[
                    Block(kind="single", items=[_timed("mobility_runners_stretch", 30)]),
                    Block(kind="single", items=[_timed("easy_jog", 480)]),
                ],
            )
        ],
    )


def test_a_workout_stating_an_objective_the_request_did_not_ask_for_is_reported() -> None:
    requested = (Quality.CONDITIONING, Quality.MOBILITY)
    ctx = ReviewContext(catalog=_CATALOG, requested=requested, only_requested=True)

    findings = review(_run_plan([Quality.CONDITIONING, Quality.STRENGTH]), ctx)
    assert [f.rule for f in findings if f.rule == "quality.unrequested_objective"] == [
        "quality.unrequested_objective"
    ]
    clean = review(_run_plan([Quality.CONDITIONING, Quality.MOBILITY]), ctx)
    assert all(f.rule != "quality.unrequested_objective" for f in clean)


def test_an_objective_that_supports_a_requested_one_is_fine() -> None:
    # Strength supports power (ADR 0011): a power request may have a strength session.
    ctx = ReviewContext(catalog=_CATALOG, requested=(Quality.POWER,), only_requested=True)
    findings = review(_run_plan([Quality.STRENGTH]), ctx)
    assert all(f.rule != "quality.unrequested_objective" for f in findings)
