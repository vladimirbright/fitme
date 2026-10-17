"""A plan with a non-catalog activity item and its own location (A§4.5) as the bot shows it
and trains it: the plan text names the activity (never its `activity:` id) and says where
the plan is trained; `/train` starts its workout for a gym profile at the plan's outdoor
location, the activity block offers no YouTube link, and ✅ logs one timed set with no kg.
The LLM is a `FunctionModel` (an empty recap); Telegram is the `FakeSession`."""

from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import InlineKeyboardMarkup
from test_train_flow import (
    _TUESDAY_NOON_UTC,
    FakeLlm,
    _activate,
    _callback_datas,
    _click,
    _last_action,
    _last_text,
    _seed_plan,
    _sent,
    _session_status,
    _texts,
    _to_review,
)

from conftest import FakeSession
from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.plan_rendering import render_plan_text
from fitme.bot.train_rendering import block_markup, block_text
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.training import list_set_logs_for_session
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, Location, Measure
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.results import SetResult
from fitme.i18n import t
from fitme.services import profile as profile_service
from fitme.services import recap, training
from fitme.services.training import BlockView, ItemLog, ItemLogKind

_ACTIVITY = "activity:bjj_class"


def _timed(exercise_id: str, seconds: int, *, name: str | None = None) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        activity_name=name,
        sets=1,
        measure=Measure.TIME,
        seconds=seconds,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
        note="Gi class at the club" if name else None,
    )


def _plan() -> Plan:
    """A (Tuesday): an outdoor interval jog, then a 90-minute BJJ class."""
    return Plan(
        name="Run and BJJ",
        location=Location.OUTDOOR,
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Run, then class",
                blocks=[
                    Block(kind="single", items=[_timed("outdoor_interval_jog", 900)]),
                    Block(kind="single", items=[_timed(_ACTIVITY, 5400, name="BJJ class")]),
                ],
            )
        ],
    )


# --- Rendering (pure) -------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_plan_text_names_the_activity_and_says_where(lang: str) -> None:
    text = render_plan_text(_plan(), catalog=load_catalog(), lang=lang, title="Plan")
    assert "BJJ class" in text
    assert "activity:" not in text
    assert "Gi class at the club" in text
    bjj = t("plan.activity_line", lang, name="BJJ class", sets_reps="", rest="").split(":")[0]
    line = next(line for line in text.splitlines() if bjj in line)
    assert "@" not in line and t("plan.load_bodyweight", lang) not in line
    location = t("enum.location.outdoor", lang)
    assert t("plan.location_line", lang, location=location) in text


def test_the_activity_line_states_no_load() -> None:
    text = render_plan_text(_plan(), catalog=load_catalog(), lang="en", title="Plan")
    assert "• BJJ class: 1 × 90 min, rest 1 min — Gi class at the club" in text


def test_a_plan_without_its_own_location_has_no_location_line() -> None:
    plan = _plan().model_copy(update={"location": None})
    text = render_plan_text(plan, catalog=load_catalog(), lang="en", title="Plan")
    assert "Where:" not in text


def test_the_activity_block_shows_its_name_and_note_and_no_link() -> None:
    block = _plan().workouts[0].blocks[1]
    view = BlockView(session_id=1, index=1, total=2, workout_key="A", title="x", block=block)
    text = block_text(view, catalog=load_catalog(), lang="en")
    assert "BJJ class" in text and "activity:" not in text
    assert "Gi class at the club" in text
    assert t("train.calibration_hint", "en") not in text
    assert "Load:" not in text
    buttons = [
        b
        for row in block_markup(view, catalog=load_catalog(), lang="en").inline_keyboard
        for b in row
    ]
    assert not any(b.url for b in buttons)  # no YouTube search for a non-catalog activity
    assert any((b.callback_data or "").startswith("tr:done:") for b in buttons)


# --- /train end to end ------------------------------------------------------------------------


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: FakeLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC)


async def _seed_gym_profile(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="6m_2y",
            barbell_experience="some",
            preferences=["weight_training"],
            location="public_gym",
            equipment=[],
            sessions_per_week=2,
            session_minutes=90,
            focus="general_fitness",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS | AREA_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value="no", clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )
    await profile_service.set_timezone(db, user_id, "UTC")
    await profile_service.clear_progress(db, user_id)


async def test_an_outdoor_activity_plan_trains_for_a_gym_profile(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    settings: Settings,
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_gym_profile(db, user_id)
    await _seed_plan(db, user_id, _plan())

    session_id = await _to_review(dispatcher, bot, session)
    review = _last_text(session)
    assert "BJJ class" in review and "activity:" not in review
    # Start re-judges the workout at the plan's own location: the outdoor jog passes.
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))

    await _click(dispatcher, bot, _last_action(session, "done"))
    block = _last_text(session)
    assert block.startswith(t("train.block_title", "en", index=2, total=2))
    assert "BJJ class" in block and "activity:" not in block
    markup = _sent(session)[-1].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert not any(button.url for row in markup.inline_keyboard for button in row)
    assert any(d.startswith("tr:done:") for d in _callback_datas(_sent(session)[-1]))
    assert "Load:" not in block  # an activity states no load

    # A kg reported on an activity is refused like any implausible load; none is fine.
    with_kg = ItemLog(
        exercise_id=_ACTIVITY,
        kind=ItemLogKind.RESULTS,
        sets=(SetResult(set_index=1, seconds=5400, load_kg=20.0),),
    )
    problem = await training.check_item_logs(db, user_id, session_id, 1, [with_kg])
    assert problem is not None and "implausible load" in problem
    no_kg = ItemLog(
        exercise_id=_ACTIVITY,
        kind=ItemLogKind.RESULTS,
        sets=(SetResult(set_index=1, seconds=5400),),
    )
    assert await training.check_item_logs(db, user_id, session_id, 1, [no_kg]) is None

    await _click(dispatcher, bot, _last_action(session, "done"))
    assert t("train.completed", "en", logged=2, planned=2) in _texts(session)
    assert await _session_status(db, session_id) == "completed"
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    activity_rows = [row for row in rows if row.exercise_id == _ACTIVITY]
    assert len(activity_rows) == 1
    row = activity_rows[0]
    assert row.actual_seconds == 5400 and row.planned_seconds == 5400
    assert row.actual_load_kg is None and row.planned_load_kg is None

    # The recap holds the activity: no next load or target, no exception.
    view = await recap.build_recap(db, llm.runtime(settings), user_id, session_id)
    assert view is not None
    assert all(item.exercise_id != _ACTIVITY for item in view.proposal.preview)
    assert all(item.exercise_id != _ACTIVITY for item in view.proposal.targets)
