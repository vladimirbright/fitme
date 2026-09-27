"""The `/train` workout loop (A§6.5, A§6.6, IMPLEMENTATION_PLAN M7) end to end through the
real dispatcher: workout selection by timezone and rotation, the precheck, the review with
engine loads, adjust (with the weekly cap and Save to plan), block-by-block delivery with
`set_logs` rows, the four buttons, free-text results through the stop-word guard and
`result_parse`, the plausibility guard, halts, holds, resume after a restart, abort and
stale/double-tapped buttons. The LLM is a `FunctionModel`; Telegram is the `FakeSession`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from conftest import FakeSession, callback_update, make_user, message_update
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import HoldClear, PlanMenu, TrainAction, TrainPick
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.controllers.training import insert_set_log, insert_workout_session
from fitme.db.selectors.chat import list_chat_messages_for_user
from fitme.db.selectors.decisions import (
    list_decisions_for_user,
    recent_increase_deltas,
)
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import (
    historical_max_kg,
    list_open_health_holds,
    list_set_logs_for_session,
    list_workout_sessions_for_user,
    recent_session_outcomes,
)
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, ScheduledDay, Workout
from fitme.domain.results import ParsedResults, Recap, SetResult
from fitme.i18n import t
from fitme.llm.agents import recap_agent, result_parse_agent, session_adjust_agent
from fitme.services import profile as profile_service
from fitme.services import training
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1
_SQUAT = "barbell_back_squat"
_BENCH = "dumbbell_bench_press"
_PUSHUP = "pushup"
# Tuesday, 12:00 UTC: workout A is scheduled on Tuesdays (weekday 1) in `make_plan`.
_TUESDAY_NOON_UTC = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# --- The fake LLM ------------------------------------------------------------------------


@dataclass
class FakeLlm:
    """Answers `session_adjust` and `result_parse` calls from their own queues, records
    every prompt it received, and counts calls."""

    adjust_responses: list[Workout | Refusal] = field(default_factory=list)
    parse_responses: list[ParsedResults] = field(default_factory=list)
    # `recap` answers: a `Recap`, or an exception to raise (a provider failure). An empty
    # queue answers with an empty recap, so workouts can complete in any test.
    recap_responses: list[Recap | Exception] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    def _respond(self, agent: str, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                self.prompts.append(str(part.content))
        output: Workout | Refusal | ParsedResults | Recap
        if agent == "recap":
            queued = self.recap_responses.pop(0) if self.recap_responses else Recap(text="")
            if isinstance(queued, Exception):
                raise queued
            output = queued
            tool = info.output_tools[0].name  # `Recap` is the agent's one output type
        elif agent == "session_adjust":
            output = self.adjust_responses.pop(0)
            wanted = "Refusal" if isinstance(output, Refusal) else "Workout"
            tool = next(
                tool.name
                for tool in info.output_tools
                if wanted in tool.name and (wanted == "Refusal" or "Refusal" not in tool.name)
            )
        else:
            output = self.parse_responses.pop(0)
            tool = info.output_tools[0].name
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=output.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings) -> LlmRuntime:
        return LlmRuntime(
            settings=settings,
            prices={},
            agent_factories={
                "session_adjust": lambda model: session_adjust_agent(
                    FunctionModel(
                        lambda m, i: self._respond("session_adjust", m, i), model_name=str(model)
                    )
                ),
                "result_parse": lambda model: result_parse_agent(
                    FunctionModel(
                        lambda m, i: self._respond("result_parse", m, i), model_name=str(model)
                    )
                ),
                "recap": lambda model: recap_agent(
                    FunctionModel(lambda m, i: self._respond("recap", m, i), model_name=str(model))
                ),
            },
        )


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: FakeLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC)


# --- Plans, profiles, history ------------------------------------------------------------


def _prescription(exercise_id: str, load: Load, *, sets: int = 3) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=sets, reps_min=8, reps_max=10, load=load, rest_seconds=90
    )


def make_plan(name: str = "Home plan") -> Plan:
    """A: squat, then a bench/push-up superset. B: push-ups only. A on Tuesday, B on Friday."""
    return Plan(
        name=name,
        schedule=[
            ScheduledDay(weekday=1, workout_key="A"),
            ScheduledDay(weekday=4, workout_key="B"),
        ],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[
                    Block(kind="single", items=[_prescription(_SQUAT, Load(kind="calibration"))]),
                    Block(
                        kind="superset",
                        items=[
                            _prescription(_BENCH, Load(kind="calibration")),
                            _prescription(_PUSHUP, Load(kind="bodyweight")),
                        ],
                    ),
                ],
            ),
            Workout(
                key="B",
                title="Push",
                blocks=[
                    Block(kind="single", items=[_prescription(_PUSHUP, Load(kind="bodyweight"))])
                ],
            ),
        ],
    )


async def _activate(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None
    return account.user_id


async def _seed_profile(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="6m_2y",
            barbell_experience="some",
            preferences=["weight_training"],
            location="home_equipment",
            equipment=["barbell", "rack", "dumbbells", "bench"],
            sessions_per_week=2,
            session_minutes=60,
            focus="strength",
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
    # `/activate` started the setup questionnaire; the profile above replaces it.
    await profile_service.clear_progress(db, user_id)


async def _seed_plan(
    db: Database, user_id: int, plan: Plan | None = None, *, is_default: bool = True
) -> tuple[int, int]:
    """Store `plan` as version 1 of an active plan; returns `(plan_id, plan_version_id)`."""
    plan = plan or make_plan()
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_confirm",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )
        plan_id = await insert_plan(
            conn, user_id=user_id, name=plan.name, is_default=is_default, status="active"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body=plan.model_dump(mode="json"),
            origin="llm",
            decision_id=decision_id,
        )
    return plan_id, version_id


async def _seed_completed_session(
    db: Database,
    user_id: int,
    plan_version_id: int,
    *,
    workout_key: str = "A",
    loads: dict[str, float | None],
    reps: int = 10,
) -> int:
    """A completed session with 3 sets per exercise in `loads` (planned = actual), all at
    `reps` (10 = `reps_max` in `make_plan`, so the engine sees a success)."""
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key=workout_key,
            status="completed",
        )
        for exercise_id, kg in loads.items():
            for index in (1, 2, 3):
                await insert_set_log(
                    conn,
                    session_id=session_id,
                    exercise_id=exercise_id,
                    set_index=index,
                    planned_load_kg=kg,
                    planned_reps_min=8,
                    planned_reps_max=10,
                    actual_load_kg=kg,
                    actual_reps=reps,
                    rpe=None,
                    source="button",
                )
    return session_id


async def _ready(dispatcher: Dispatcher, bot: Bot, db: Database) -> tuple[int, int, int]:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    plan_id, version_id = await _seed_plan(db, user_id)
    return user_id, plan_id, version_id


# --- Driving the bot -----------------------------------------------------------------------


async def _send(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=text))


async def _click(dispatcher: Dispatcher, bot: Bot, data: Any) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=data))


def _sent(session: FakeSession) -> list[SendMessage]:
    return [m for m in session.sent if isinstance(m, SendMessage)]


def _last_text(session: FakeSession) -> str:
    return _sent(session)[-1].text or ""


def _texts(session: FakeSession) -> list[str]:
    return [m.text or "" for m in _sent(session)]


def _toasts(session: FakeSession) -> list[str]:
    return [m.text or "" for m in session.sent if isinstance(m, AnswerCallbackQuery)]


def _callback_datas(message: SendMessage) -> list[str]:
    markup = message.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def _last_action(session: FakeSession, action: str) -> TrainAction:
    """The newest `TrainAction` button with `action` in any sent keyboard."""
    for message in reversed(_sent(session)):
        for data in _callback_datas(message):
            if data.startswith(f"tr:{action}:"):
                return TrainAction.unpack(data)
    raise AssertionError(f"no {action!r} button was sent")


def _last_pick(session: FakeSession, kind: str) -> TrainPick:
    for message in reversed(_sent(session)):
        for data in _callback_datas(message):
            if data.startswith(f"tp:{kind}:"):
                return TrainPick.unpack(data)
    raise AssertionError(f"no {kind!r} pick button was sent")


async def _to_review(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> int:
    """`/train` → Start this workout → precheck No → the review. Returns the session id."""
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_pick(session, "workout"))
    no = _last_action(session, "precheck_no")
    await _click(dispatcher, bot, no)
    assert _last_action(session, "start").session_id == no.session_id
    return no.session_id


async def _to_first_block(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> int:
    session_id = await _to_review(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    return session_id


async def _holds(db: Database, user_id: int) -> list[str]:
    async with db.read() as conn:
        return [hold.reason for hold in await list_open_health_holds(conn, user_id)]


async def _session_status(db: Database, session_id: int) -> str:
    async with db.read() as conn:
        sessions = {s.id: s for s in await list_workout_sessions_for_user(conn, 1)}
    return sessions[session_id].status


async def _rows(
    db: Database, session_id: int
) -> list[tuple[str, int, float | None, int | None, bool]]:
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    return [(r.exercise_id, r.set_index, r.actual_load_kg, r.actual_reps, r.skipped) for r in rows]


def _parsed(
    *sets: tuple[int, int | None, float | None], safety: bool = False, unclear: bool = False
) -> ParsedResults:
    return ParsedResults(
        sets=[
            SetResult(set_index=index, skipped=True)
            if reps is None
            else SetResult(set_index=index, reps=reps, load_kg=kg)
            for index, reps, kg in sets
        ],
        safety_signal=safety,
        unclear=unclear,
    )


# --- Selection: plan, timezone, rotation ------------------------------------------------------


async def test_train_without_a_plan_points_to_plan(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("train.no_plan", "en")


async def test_today_is_taken_in_the_users_timezone_and_rotation_follows_the_last_completed(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, _plan_id, version_id = await _ready(dispatcher, bot, db)
    # Monday 22:00 UTC: nothing scheduled in UTC, but already Tuesday 12:00 in UTC+14.
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 28, 22, 0, tzinfo=UTC))

    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t(
        "train.suggested_next", "en", plan="Home plan", workout="A — Full body"
    )
    await profile_service.set_timezone(db, user_id, "Pacific/Kiritimati")
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t(
        "train.suggested_today", "en", plan="Home plan", workout="A — Full body"
    )

    # Back in UTC (nothing scheduled): after a completed A, the rotation suggests B.
    await profile_service.set_timezone(db, user_id, "UTC")
    await _seed_completed_session(db, user_id, version_id, loads={_PUSHUP: None})
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t(
        "train.suggested_next", "en", plan="Home plan", workout="B — Push"
    )
    # "Pick another" lists every workout.
    await _click(dispatcher, bot, _last_pick(session, "workouts"))
    datas = _callback_datas(_sent(session)[-1])
    assert TrainPick(kind="workout", plan_id=_plan_id, key="A").pack() in datas
    assert TrainPick(kind="workout", plan_id=_plan_id, key="B").pack() in datas


async def test_several_plans_without_a_default_are_offered_as_buttons(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    one, _ = await _seed_plan(db, user_id, make_plan("One"), is_default=False)
    two, _ = await _seed_plan(db, user_id, make_plan("Two"), is_default=False)
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("train.choose_plan", "en")
    datas = _callback_datas(_sent(session)[-1])
    assert TrainPick(kind="plan", plan_id=one).pack() in datas
    assert TrainPick(kind="plan", plan_id=two).pack() in datas
    await _click(dispatcher, bot, TrainPick(kind="plan", plan_id=two))
    assert "Two" in _last_text(session)


# --- Precheck -----------------------------------------------------------------------------


async def test_precheck_yes_halts_and_train_and_plan_refuse_afterwards(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_pick(session, "workout"))
    assert _last_text(session) == t("precheck.question", "en")
    yes = _last_action(session, "precheck_yes")

    await _click(dispatcher, bot, yes)

    assert _last_text(session) == t("halt.message", "en")
    assert await _holds(db, user_id) == ["precheck_yes"]
    assert await _session_status(db, yes.session_id) == "halted"
    async with db.read() as conn:
        (hold,) = await list_open_health_holds(conn, user_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert hold.source_session_id == yes.session_id
    halt_decision = next(d for d in decisions if d.kind == "session_halt")
    assert halt_decision.user_report is not None
    assert halt_decision.user_report["session_id"] == yes.session_id

    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("refusal.open_health_hold", "en")
    await _send(dispatcher, bot, "/plan")
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    assert _last_text(session) == t("refusal.open_health_hold", "en")
    assert llm.calls == 0

    # The hold can't be cleared the same day.
    await _click(dispatcher, bot, HoldClear(hold_id=hold.id))
    assert await _holds(db, user_id) == ["precheck_yes"]


async def test_without_an_explicit_no_the_session_never_starts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_pick(session, "workout"))
    session_id = _last_action(session, "precheck_no").session_id

    # No answer: a Start button (forged or stale) is rejected; nothing is logged.
    await _click(dispatcher, bot, TrainAction(action="start", session_id=session_id))
    assert _toasts(session)[-1] == t("train.stale", "en")
    await _click(dispatcher, bot, TrainAction(action="done", session_id=session_id, block=0))
    assert _toasts(session)[-1] == t("train.stale", "en")
    assert await _session_status(db, session_id) == "draft"
    assert await _rows(db, session_id) == []

    # /train again re-asks the precheck for the same session (state is in the DB).
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("precheck.question", "en")
    assert _last_action(session, "precheck_no").session_id == session_id


# --- Review: engine loads -----------------------------------------------------------------


async def test_review_shows_engine_loads_calibration_without_history_and_each_for_dumbbells(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)

    await _to_review(dispatcher, bot, session)
    review = _last_text(session)
    assert review.startswith(t("train.review_title", "en", workout_key="A", title="Full body"))
    assert "Barbell back squat: 3 × 8–10 @ calibration: start with 20 kg" in review
    assert "Dumbbell bench press: 3 × 8–10 @ calibration: start with 2 kg each" in review
    assert "Push-up: 3 × 8–10 @ bodyweight" in review
    assert t("disclosure.ai", "en") in review
    await _click(dispatcher, bot, _last_action(session, "abort"))

    # With a successful session behind them, the engine proposes one increment.
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0, _BENCH: 10.0})
    await _to_review(dispatcher, bot, session)
    review = _last_text(session)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in review
    assert "Dumbbell bench press: 3 × 8–10 @ 11 kg each" in review


# --- Happy path ---------------------------------------------------------------------------


async def test_according_to_plan_logs_every_prescribed_set_and_completes(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)

    block = _last_text(session)
    assert "Barbell back squat" in block
    assert block.count("Set 1: 8–10 reps @ 42.5 kg") == 1 and "Set 3: 8–10 reps @ 42.5 kg" in block
    assert "Set the rack's safety arms" in block  # the vetted catalog instructions
    assert t("train.rest_line", "en", rest=90) in block
    datas = _callback_datas(_sent(session)[-1])
    assert TrainAction(action="pain", session_id=session_id, block=0).pack() in datas
    assert TrainAction(action="skip", session_id=session_id, block=0).pack() in datas
    assert TrainAction(action="enter", session_id=session_id, block=0).pack() in datas
    # One row per prescribed set, actuals empty, created when the block was sent.
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]

    await _click(dispatcher, bot, _last_action(session, "done"))
    block2 = _last_text(session)
    assert block2.startswith(t("train.block_title", "en", index=2, total=2))
    assert "Set 1: 8–10 reps @ calibration: start with 2 kg each" in block2
    assert "Set 1: 8–10 reps @ bodyweight" in block2
    assert t("train.calibration_hint", "en") in block2

    await _click(dispatcher, bot, _last_action(session, "done"))
    assert t("train.completed", "en", logged=9, planned=9) in _texts(session)  # then the recap (M8)
    assert await _session_status(db, session_id) == "completed"
    rows = await _rows(db, session_id)
    assert rows[:3] == [(_SQUAT, i, 42.5, 10, False) for i in (1, 2, 3)]
    assert rows[3:6] == [(_BENCH, i, None, 10, False) for i in (1, 2, 3)]
    assert rows[6:] == [(_PUSHUP, i, None, 10, False) for i in (1, 2, 3)]

    # The applied session-start decision recorded the engine's increase exactly once.
    async with db.read() as conn:
        deltas = await recent_increase_deltas(
            conn, user_id, _SQUAT, since="2026-09-22T00:00:00.000000Z"
        )
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT)
    assert deltas == [2.5]
    assert outcomes[0].planned_load_kg == 42.5 and outcomes[0].hit_reps_max

    # Next review: the increase applied this week holds; no second increase.
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)


async def test_skip_marks_the_blocks_sets_skipped(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)

    await _click(dispatcher, bot, _last_action(session, "skip"))
    assert _toasts(session)[-1] == t("train.block_skipped", "en")
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, None, None, True) for i in (1, 2, 3)]
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert t("train.completed", "en", logged=6, planned=9) in _texts(session)  # then the recap (M8)

    # A session with a skipped set is never a success for the engine (A§7.3).
    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT)
    assert not outcomes[0].hit_reps_max


async def test_stale_block_buttons_are_rejected_and_double_taps_are_idempotent(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    start = _last_action(session, "start")
    first_done = _last_action(session, "done")

    # Start twice: the same block again, no duplicate rows.
    await _click(dispatcher, bot, start)
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    assert len(await _rows(db, session_id)) == 3

    await _click(dispatcher, bot, first_done)
    assert len(await _rows(db, session_id)) == 9
    await _click(dispatcher, bot, first_done)  # double tap on the old block
    assert _toasts(session)[-1] == t("train.stale", "en")
    assert await _session_status(db, session_id) == "in_progress"
    assert len(await _rows(db, session_id)) == 9
    # A button for a block ahead of the current one is stale too.
    await _click(dispatcher, bot, TrainAction(action="done", session_id=session_id, block=5))
    assert _toasts(session)[-1] == t("train.stale", "en")
    # Another user's / unknown session id.
    await _click(dispatcher, bot, TrainAction(action="done", session_id=999, block=0))
    assert _toasts(session)[-1] == t("train.stale", "en")


# --- Halts during the workout ----------------------------------------------------------------


async def test_pain_button_halts_the_session_with_a_hold(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)

    await _click(dispatcher, bot, _last_action(session, "pain"))

    assert _last_text(session) == t("halt.message", "en")
    assert await _holds(db, user_id) == ["pain_button"]
    assert await _session_status(db, session_id) == "halted"
    # The rows stay, unlogged: a halted session is never a success for the engine.
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT)
        (hold,) = await list_open_health_holds(conn, user_id)
    assert outcomes[0].planned_load_kg == 40.0  # the seeded session, not the halted one
    assert hold.source_session_id == session_id

    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("refusal.open_health_hold", "en")
    await _click(dispatcher, bot, HoldClear(hold_id=hold.id))
    assert await _holds(db, user_id) == ["pain_button"]
    assert llm.calls == 0


async def test_stop_word_in_results_text_halts_without_an_llm_call(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)
    llm.parse_responses.append(_parsed((1, 8, 40.0), (2, 8, 40.0), (3, 8, 40.0)))
    await _click(dispatcher, bot, _last_action(session, "enter"))
    assert _last_text(session).startswith("Send the results for Barbell back squat")

    await _send(dispatcher, bot, "8, 8, 6 at 40 but a sharp pain in my knee on the last one")

    assert _last_text(session) == t("halt.message", "en")
    assert llm.calls == 0
    assert await _holds(db, user_id) == ["stop_word"]
    assert await _session_status(db, session_id) == "halted"
    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
    assert messages[0].session_id == session_id  # tied to the session, purged with it

    # Later text isn't sent to the model either: the pending prompt died with the halt.
    await _send(dispatcher, bot, "8, 8, 8")
    assert llm.calls == 0


async def test_stop_word_in_adjust_text_halts_without_an_llm_call(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    llm.adjust_responses.append(make_plan().workouts[0])
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    assert _last_text(session) == t("train.adjust_prompt", "en")

    await _send(dispatcher, bot, "make it lighter, I feel dizzy")

    assert _last_text(session) == t("halt.message", "en")
    assert llm.calls == 0
    assert await _holds(db, user_id) == ["stop_word"]
    assert await _session_status(db, session_id) == "halted"


async def test_llm_safety_signal_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)
    llm.parse_responses.append(_parsed((1, 8, 40.0), (2, 8, 40.0), (3, 8, 40.0), safety=True))
    await _click(dispatcher, bot, _last_action(session, "enter"))

    await _send(dispatcher, bot, "8 8 8 at 40, my arm went weird")

    assert llm.calls == 1
    assert _last_text(session) == t("halt.message", "en")
    assert await _holds(db, user_id) == ["llm_safety_signal"]
    assert await _session_status(db, session_id) == "halted"
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]


# --- Results: parse, plausibility, confirm --------------------------------------------------


async def test_confirmed_parse_writes_actuals_and_the_llm_input_is_verbatim(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    llm.parse_responses.append(_parsed((1, 10, 42.5), (2, 9, 42.5), (3, None, None)))
    await _click(dispatcher, bot, _last_action(session, "enter"))

    await _send(dispatcher, bot, "10, 9 at 42.5, skipped the third")

    assert llm.calls == 1
    table = _last_text(session)
    assert table.startswith(t("train.parsed_title", "en", name="Barbell back squat"))
    assert "Set 1: 10 reps @ 42.5 kg" in table and "Set 3: skipped" in table
    # Nothing is stored until Correct.
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    # The prompt held only the planned block, the text and the load units (A§8.2).
    payload = json.loads(llm.prompts[0])
    assert set(payload) == {"language", "planned_block", "result_text", "load_units"}
    assert payload["load_units"] == {_SQUAT: "total"}
    assert "user_id" not in llm.prompts[0]
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    parse_decision = next(d for d in decisions if d.kind == "result_parse")
    assert parse_decision.llm_input == payload
    assert parse_decision.model == "anthropic:claude-haiku-4-5"

    await _click(dispatcher, bot, _last_action(session, "parse_ok"))

    assert (await _rows(db, session_id))[:3] == [
        (_SQUAT, 1, 42.5, 10, False),
        (_SQUAT, 2, 42.5, 9, False),
        (_SQUAT, 3, None, None, True),
    ]
    assert _last_text(session).startswith(t("train.block_title", "en", index=2, total=2))
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    assert {row.source for row in rows[:3]} == {"free_text"}


async def test_superset_results_are_entered_one_exercise_at_a_time(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "done"))  # squat block
    await _click(dispatcher, bot, _last_action(session, "enter"))
    assert _last_text(session).startswith("Send the results for Dumbbell bench press")

    llm.parse_responses.append(_parsed((1, 10, 8.0), (2, 10, 8.0), (3, 8, 8.0)))
    await _send(dispatcher, bot, "10 10 8 with 8s")
    await _click(dispatcher, bot, _last_action(session, "parse_ok"))
    assert _last_text(session).startswith("Send the results for Push-up")

    llm.parse_responses.append(_parsed((1, 12, None), (2, 12, None), (3, 12, None)))
    await _send(dispatcher, bot, "12 12 12")
    assert "Set 1: 12 reps" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "parse_ok"))

    assert t("train.completed", "en", logged=9, planned=9) in _texts(session)  # then the recap (M8)
    rows = await _rows(db, session_id)
    assert rows[3:6] == [(_BENCH, i, 8.0, r, False) for i, r in ((1, 10), (2, 10), (3, 8))]
    assert rows[6:] == [(_PUSHUP, i, None, 12, False) for i in (1, 2, 3)]


async def test_an_implausible_parse_re_asks_and_stores_nothing(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0, _BENCH: 10.0})
    session_id = await _to_first_block(dispatcher, bot, session)

    # "425" for 42.5 on the squat.
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 425.0), (2, 10, 42.5), (3, 10, 42.5)))
    await _send(dispatcher, bot, "10 10 10 at 425")
    assert _last_text(session) == t(
        "train.results_implausible", "en", name="Barbell back squat", load="42.5 kg"
    )
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    parse_decision = next(d for d in decisions if d.kind == "result_parse")
    assert (
        parse_decision.proposal is not None and parse_decision.proposal["verdict"] == "implausible"
    )
    assert any(
        g["rule"] == "plausibility.parsed_load" and not g["ok"] for g in parse_decision.guards_fired
    )

    # The prompt is still pending: a plausible re-entry is shown for confirmation.
    llm.parse_responses.append(_parsed((1, 10, 42.5), (2, 10, 42.5), (3, 10, 42.5)))
    await _send(dispatcher, bot, "10 10 10 at 42.5")
    assert _last_text(session).startswith(t("train.parsed_title", "en", name="Barbell back squat"))
    await _click(dispatcher, bot, _last_action(session, "parse_ok"))

    # A combined two-dumbbell total (22 for "11 each") on the bench press.
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 22.0), (2, 10, 22.0), (3, 10, 22.0)))
    await _send(dispatcher, bot, "10 10 10 with 22")
    assert _last_text(session) == t(
        "train.results_implausible", "en", name="Dumbbell bench press", load="11 kg each"
    )
    assert (await _rows(db, session_id))[3:6] == [(_BENCH, i, None, None, False) for i in (1, 2, 3)]
    # Confirming now finds no shown parse: nothing is written.
    await _click(
        dispatcher, bot, TrainAction(action="parse_ok", session_id=session_id, block=1, item=0)
    )
    assert _toasts(session)[-1] == t("train.stale", "en")
    assert (await _rows(db, session_id))[3:6] == [(_BENCH, i, None, None, False) for i in (1, 2, 3)]


async def test_an_unclear_parse_re_asks(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    _user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 8, None), unclear=True))

    await _send(dispatcher, bot, "went ok I guess")

    assert _last_text(session) == t("train.results_unclear", "en", name="Barbell back squat")
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    # Still pending: the next text goes to the model again.
    llm.parse_responses.append(_parsed((1, 8, 20.0), (2, 8, 20.0), (3, 8, 20.0)))
    await _send(dispatcher, bot, "8 8 8 at 20")
    assert llm.calls == 2
    assert "Set 1: 8 reps @ 20 kg" in _last_text(session)
    # "Fix" re-asks instead of storing.
    await _click(dispatcher, bot, _last_action(session, "parse_fix"))
    assert _last_text(session).startswith("Send the results for Barbell back squat")
    assert await _rows(db, session_id) == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]


# --- Adjust, the weekly cap, Save to plan -----------------------------------------------------


def _adjusted_workout(squat_kg: float, *, sets: int = 3) -> Workout:
    base = make_plan().workouts[0]
    return Workout(
        key="A",
        title="Full body, adjusted",
        blocks=[
            Block(
                kind="single",
                items=[_prescription(_SQUAT, Load(kind="kg", kg=squat_kg), sets=sets)],
            ),
            base.blocks[1],
        ],
    )


async def test_adjust_within_the_cap_is_applied_once_at_start_and_beyond_it_is_substituted(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_review(dispatcher, bot, session)
    since = "2026-09-22T00:00:00.000000Z"

    # 45 kg is 5 kg over the 40 kg reference: beyond the 2.5 kg weekly cap. The load rule
    # fails alone, so the engine's value (42.5) is substituted and no retry is spent.
    llm.adjust_responses.append(_adjusted_workout(45.0, sets=2))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "only 2 sets of squats today, at 45")
    assert llm.calls == 1
    assert "only 2 sets of squats today, at 45" in llm.prompts[0]
    review = _last_text(session)
    assert t("train.review_adjusted", "en") in review
    assert "Barbell back squat: 2 × 8–10 @ 42.5 kg" in review
    assert TrainAction(action="save_plan", session_id=session_id).pack() in _callback_datas(
        _sent(session)[-1]
    )
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
        assert await recent_increase_deltas(conn, user_id, _SQUAT, since=since) == []
    adjust_decision = next(d for d in decisions if d.kind == "session_adjust")
    assert adjust_decision.load_changes == []  # a draft applies nothing
    assert adjust_decision.llm_input == json.loads(llm.prompts[0])
    assert adjust_decision.model == "anthropic:claude-sonnet-5"
    assert any(g["rule"] == "loads.substituted" for g in adjust_decision.guards_fired)
    assert any(
        g["rule"] == "progression.weekly_cap" and not g["ok"] for g in adjust_decision.guards_fired
    )

    # Start applies the session-only increase once: the start decision carries it.
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert "Set 2: 8–10 reps @ 42.5 kg" in _last_text(session)
    async with db.read() as conn:
        deltas = await recent_increase_deltas(conn, user_id, _SQUAT, since=since)
        decisions = await list_decisions_for_user(conn, user_id)
    assert deltas == [2.5]
    start_decision = decisions[0]
    assert start_decision.kind == "session_adjust"
    assert start_decision.user_report is not None and start_decision.user_report["event"] == "start"
    assert start_decision.user_report["adjusted"] is True
    assert start_decision.load_changes == [{"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}]
    assert await _rows(db, session_id) == [
        (_SQUAT, 1, None, None, False),
        (_SQUAT, 2, None, None, False),
    ]


async def test_save_to_plan_creates_a_new_version_and_the_start_counts_nothing_twice(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, plan_id, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_review(dispatcher, bot, session)
    since = "2026-09-22T00:00:00.000000Z"

    # Nothing adjusted yet: nothing to save.
    await _click(dispatcher, bot, TrainAction(action="save_plan", session_id=session_id))
    assert _toasts(session)[-1] == t("train.nothing_to_save", "en")

    llm.adjust_responses.append(_adjusted_workout(42.5))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "42.5 on squats, title it adjusted")
    await _click(dispatcher, bot, _last_action(session, "save_plan"))
    assert _last_text(session) == t("train.saved_to_plan", "en", name="Home plan", version=2)
    async with db.read() as conn:
        versions = await list_plan_versions(conn, plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
        deltas = await recent_increase_deltas(conn, user_id, _SQUAT, since=since)
    assert [v.version for v in versions] == [1, 2]
    saved = Plan.model_validate(versions[-1].body)
    assert saved.workouts[0].title == "Full body, adjusted"
    assert saved.workouts[0].blocks[0].items[0].load.kg == 42.5
    assert saved.workouts[1].title == "Push"  # the other workout is untouched
    confirm = next(d for d in decisions if d.kind == "plan_confirm")
    assert confirm.load_changes == [{"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}]
    assert deltas == [2.5]
    # A second tap saves nothing new.
    await _click(dispatcher, bot, _last_action(session, "save_plan"))
    assert _toasts(session)[-1] == t("train.already_saved_to_plan", "en")
    async with db.read() as conn:
        assert len(await list_plan_versions(conn, plan_id)) == 2

    # Start: the increase was applied by the plan version, so the start applies none.
    await _click(dispatcher, bot, _last_action(session, "start"))
    async with db.read() as conn:
        deltas = await recent_increase_deltas(conn, user_id, _SQUAT, since=since)
        decisions = await list_decisions_for_user(conn, user_id)
    assert deltas == [2.5]
    assert decisions[0].kind == "session_adjust" and decisions[0].load_changes == []


async def test_adjust_refusal_from_the_model_keeps_the_review(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    llm.adjust_responses.append(Refusal(code="out_of_scope", message="model text, never shown"))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "what should I eat before this")
    texts = [m.text or "" for m in _sent(session)]
    assert t("refusal.out_of_scope", "en") in texts
    assert _last_action(session, "start").session_id == session_id
    assert await _session_status(db, session_id) == "confirmed"


# --- Resume, abort, /cancel -------------------------------------------------------------------


async def test_a_restart_mid_workout_resumes_at_the_same_block(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    settings: Settings,
    llm: FakeLlm,
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=2, total=2))
    rows_before = await _rows(db, session_id)
    assert len(rows_before) == 9

    # A "restart": a fresh dispatcher (fresh in-memory state) over the same database.
    restarted = build_dispatcher(db, settings, llm.runtime(settings))
    await _send(restarted, bot, "/train")
    assert _last_text(session) == t(
        "train.active_prompt", "en", workout="A — Full body", block=2, total=2
    )
    await _click(restarted, bot, _last_action(session, "resume"))

    block = _last_text(session)
    assert block.startswith(t("train.block_title", "en", index=2, total=2))
    assert "Dumbbell bench press" in block and "Push-up" in block
    assert await _rows(db, session_id) == rows_before  # no duplicated rows
    await _click(restarted, bot, _last_action(session, "done"))
    assert t("train.completed", "en", logged=9, planned=9) in _texts(session)  # then the recap (M8)


async def test_a_restart_in_review_shows_the_review_again(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    settings: Settings,
    llm: FakeLlm,
) -> None:
    await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    restarted = build_dispatcher(db, settings, llm.runtime(settings))
    await _send(restarted, bot, "/train")
    assert _last_text(session).startswith(
        t("train.review_title", "en", workout_key="A", title="Full body")
    )
    assert _last_action(session, "start").session_id == session_id


async def test_abort_keeps_logged_sets(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "done"))

    # /cancel keeps an in-progress workout (A§6.2) and offers Abort.
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("train.kept_after_cancel", "en")
    assert await _session_status(db, session_id) == "in_progress"
    await _click(dispatcher, bot, _last_action(session, "abort"))
    assert _last_text(session) == t("train.aborted", "en")
    assert await _session_status(db, session_id) == "aborted"
    rows = await _rows(db, session_id)
    assert rows[:3] == [(_SQUAT, i, 42.5, 10, False) for i in (1, 2, 3)]
    # An aborted session is not a success for the engine, and the next /train starts fresh.
    async with db.read() as conn:
        outcomes = await recent_session_outcomes(conn, user_id, _SQUAT)
    assert outcomes[0].planned_load_kg == 40.0
    await _send(dispatcher, bot, "/train")
    assert _last_text(session).startswith("Today's workout")
    # And /cancel with nothing active is the ordinary message.
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("cancel.nothing_active", "en")


async def test_cancel_aborts_an_unstarted_session(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("train.aborted", "en")
    assert await _session_status(db, session_id) == "aborted"
    assert await _rows(db, session_id) == []


async def test_choosing_a_workout_while_one_is_in_progress_offers_resume(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    _user_id, plan_id, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, TrainPick(kind="workout", plan_id=plan_id, key="B"))
    assert _last_text(session) == t(
        "train.active_prompt", "en", workout="A — Full body", block=1, total=2
    )
    assert _last_action(session, "resume").session_id == session_id
    async with db.read() as conn:
        assert len(await list_workout_sessions_for_user(conn, 1)) == 1


async def test_hold_can_be_cleared_the_next_day_after_a_workout_halt(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "pain"))
    async with db.read() as conn:
        (hold,) = await list_open_health_holds(conn, user_id)
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(days=1))
    await _send(dispatcher, bot, "/start")
    await _click(dispatcher, bot, HoldClear(hold_id=hold.id))
    assert await _holds(db, user_id) == []
    await _send(dispatcher, bot, "/train")
    assert _last_text(session).startswith("Nothing is scheduled for today")
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id)


# --- Review fixes (M7 round 2) ----------------------------------------------------------------


def _bad_workout() -> Workout:
    """An adjustment that fails a structural guard (a non-catalog exercise id)."""
    base = make_plan().workouts[0]
    return Workout(
        key="A",
        title="Bad",
        blocks=[
            Block(
                kind="single",
                items=[_prescription("made_up_exercise", Load(kind="calibration"))],
            ),
            base.blocks[1],
        ],
    )


async def _accepted_d1(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, llm: FakeLlm, db: Database, user_id: int
) -> int:
    """An accepted adjustment D1 (2 sets of squats at 42.5); returns its decision id."""
    llm.adjust_responses.append(_adjusted_workout(42.5, sets=2))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "only 2 sets of squats")
    assert "Barbell back squat: 2 × 8–10 @ 42.5 kg" in _last_text(session)
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    d1 = decisions[0]
    assert d1.kind == "session_adjust" and d1.user_report is not None
    assert d1.user_report["event"] == "adjust"
    return d1.id


async def test_b1_a_guard_rejected_adjustment_never_replaces_the_accepted_draft(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_review(dispatcher, bot, session)
    d1 = await _accepted_d1(dispatcher, bot, session, llm, db, user_id)

    # D2..D4: three structurally failing attempts (normal tier, retry, large tier).
    llm.adjust_responses.extend([_bad_workout(), _bad_workout(), _bad_workout()])
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "add something for calves")
    assert llm.calls == 4
    texts = [m.text or "" for m in _sent(session)]
    assert t("refusal.no_safe_plan", "en") in texts
    review = _last_text(session)
    assert "made_up_exercise" not in review and "Bad" not in review
    assert "Barbell back squat: 2 × 8–10 @ 42.5 kg" in review
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    rejected = [
        d
        for d in decisions
        if d.kind == "session_adjust"
        and d.user_report is not None
        and d.user_report["event"] == "adjust_rejected"
    ]
    assert len(rejected) == 3
    assert all(d.load_changes == [] for d in rejected)

    # Start trains D1, and the start decision points at it.
    await _click(dispatcher, bot, _last_action(session, "start"))
    block = _last_text(session)
    assert block.startswith(t("train.block_title", "en", index=1, total=2))
    assert "Set 2: 8–10 reps @ 42.5 kg" in block and "Set 3" not in block
    assert await _session_status(db, session_id) == "in_progress"
    async with db.read() as conn:
        start_decision = (await list_decisions_for_user(conn, user_id))[0]
    assert start_decision.user_report is not None
    assert start_decision.user_report["event"] == "start"
    assert start_decision.user_report["draft_decision_id"] == d1


async def test_b1_a_model_refusal_keeps_the_accepted_draft_and_says_so(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    await _to_review(dispatcher, bot, session)
    d1 = await _accepted_d1(dispatcher, bot, session, llm, db, user_id)

    llm.adjust_responses.append(Refusal(code="out_of_scope", message="model text, never shown"))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "what should I eat")

    texts = [m.text or "" for m in _sent(session)]
    assert t("refusal.out_of_scope", "en") in texts  # the change couldn't be made
    review = _last_text(session)
    assert t("train.review_adjusted", "en") in review
    assert "Barbell back squat: 2 × 8–10 @ 42.5 kg" in review
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    assert decisions[0].user_report is not None
    assert decisions[0].user_report["event"] == "adjust_rejected"
    assert decisions[0].proposal is not None and "refusal" in decisions[0].proposal
    await _click(dispatcher, bot, _last_action(session, "start"))
    async with db.read() as conn:
        start_decision = (await list_decisions_for_user(conn, user_id))[0]
    assert start_decision.user_report is not None
    assert start_decision.user_report["draft_decision_id"] == d1


async def test_b2_a_calibration_typo_is_re_asked_and_never_reaches_the_history_max(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)  # squat is calibration
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 425.0), (2, 10, 425.0), (3, 10, 425.0)))

    await _send(dispatcher, bot, "10 10 10 at 42,5")

    assert _last_text(session) == t(
        "train.results_implausible",
        "en",
        name="Barbell back squat",
        load="calibration: start with 20 kg or lighter, log what you used",
    )
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    # No shown parse exists: a Correct button can't write anything.
    await _click(
        dispatcher, bot, TrainAction(action="parse_ok", session_id=session_id, block=0, item=0)
    )
    assert _toasts(session)[-1] == t("train.stale", "en")
    # The plausible re-entry goes through; the session completes at 42.5.
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 42.5), (2, 10, 42.5), (3, 10, 42.5)))
    await _send(dispatcher, bot, "10 10 10 at 42.5")
    await _click(dispatcher, bot, _last_action(session, "parse_ok"))
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert await _session_status(db, session_id) == "completed"
    async with db.read() as conn:
        assert await historical_max_kg(conn, user_id, _SQUAT) == 42.5
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)


async def test_b2_a_calibration_parse_is_checked_against_the_historical_max(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    """Calibration again after a session where nothing usable was logged, but a 40 kg
    historical max exists: 125 ("12,5") is inside the absolute bound and caught by the
    history reference; the next review is still calibration."""
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: None})
    session_id = await _to_first_block(dispatcher, bot, session)
    assert "calibration" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 125.0), (2, 10, 125.0), (3, 10, 125.0)))
    await _send(dispatcher, bot, "10 10 10 at 12,5")
    assert _last_text(session).startswith("One of those numbers doesn't look right")
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    await _click(dispatcher, bot, _last_action(session, "skip"))
    await _click(dispatcher, bot, _last_action(session, "done"))
    async with db.read() as conn:
        assert await historical_max_kg(conn, user_id, _SQUAT) == 40.0
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ calibration" in _last_text(session)


async def test_correct_on_an_older_parse_table_is_stale(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "enter"))
    llm.parse_responses.append(_parsed((1, 10, 42.5), (2, 10, 42.5), (3, 10, 42.5)))
    await _send(dispatcher, bot, "10 10 10 at 42.5")
    first_ok = _last_action(session, "parse_ok")
    llm.parse_responses.append(_parsed((1, 8, 40.0), (2, 8, 40.0), (3, 8, 40.0)))
    await _click(dispatcher, bot, _last_action(session, "enter"))
    await _send(dispatcher, bot, "8 8 8 at 40")
    second_ok = _last_action(session, "parse_ok")
    assert first_ok.decision_id and first_ok.decision_id != second_ok.decision_id

    await _click(dispatcher, bot, first_ok)
    assert _toasts(session)[-1] == t("train.stale", "en")
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, None, None, False) for i in (1, 2, 3)]
    await _click(dispatcher, bot, second_ok)
    assert (await _rows(db, session_id))[:3] == [(_SQUAT, i, 40.0, 8, False) for i in (1, 2, 3)]


async def test_adjust_service_re_scans_for_stop_words_before_the_llm(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    settings: Settings,
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    llm.adjust_responses.append(make_plan().workouts[0])

    result = await training.adjust(
        db, llm.runtime(settings), user_id, session_id, "lighter please, my chest hurts"
    )

    assert result.halt is not None and result.review is None
    assert llm.calls == 0
    assert await _holds(db, user_id) == ["stop_word"]
    assert await _session_status(db, session_id) == "halted"


async def test_start_judges_todays_workout_not_the_stored_schedule(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    # The user now trains 3x/week; the confirmed plan's schedule still has 2 days.
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="30_39",
            weight_bucket="80_89",
            experience="6m_2y",
            barbell_experience="some",
            preferences=["weight_training"],
            location="home_equipment",
            equipment=["barbell", "rack", "dumbbells", "bench"],
            sessions_per_week=3,
            session_minutes=60,
            focus="strength",
            completed_at=clock.now(),
        )
    session_id = await _to_review(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    assert await _session_status(db, session_id) == "in_progress"


async def test_start_refuses_a_workout_that_fails_a_per_prescription_guard(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_review(dispatcher, bot, session)
    # A knee injury flagged after the plan was confirmed: the squat is now contraindicated.
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn, user_id=user_id, flag="knee_injury_current", value="yes", clearance=None
        )
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session) == t("refusal.no_safe_workout", "en")
    assert "/plan" in _last_text(session)
    assert await _session_status(db, session_id) == "confirmed"
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    refusal = decisions[0]
    assert refusal.kind == "refusal"
    assert any(
        g["rule"] == "screening.exercise_allowed" and not g["ok"] for g in refusal.guards_fired
    )
    assert not any(g["rule"].startswith("plan.schedule") for g in refusal.guards_fired)


async def test_precheck_yes_halts_the_owners_session_but_ignores_an_unknown_id(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_pick(session, "workout"))
    session_id = _last_action(session, "precheck_yes").session_id

    await _click(dispatcher, bot, TrainAction(action="precheck_yes", session_id=999))
    assert await _holds(db, user_id) == []
    assert _last_text(session) == t("train.stale", "en")

    result = await training.precheck_yes(db, user_id, session_id)
    assert result.halt is not None and result.halt.session_id == session_id
    assert await _session_status(db, session_id) == "halted"


async def test_a_calibration_block_hides_the_all_sets_button(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)  # squat: calibration
    datas = _callback_datas(_sent(session)[-1])
    assert not any(d.startswith("tr:done:") for d in datas)
    assert TrainAction(action="enter", session_id=session_id, block=0).pack() in datas
    assert t("train.calibration_hint", "en") in _last_text(session)
    assert t("train.according_hint", "en") not in _last_text(session)
    # A forged ✅ still logs nothing usable... but the button isn't offered, so skip instead.
    await _click(dispatcher, bot, _last_action(session, "skip"))
    block2 = _last_text(session)  # bench calibration + push-up bodyweight: ✅ stays
    assert t("train.according_hint", "en") in block2
    assert any(d.startswith("tr:done:") for d in _callback_datas(_sent(session)[-1]))
    assert t("workout.according_to_plan_button", "en") == "✅ All sets at the top of the range"
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})


async def test_save_to_plan_after_start_says_already_started(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_review(dispatcher, bot, session)
    llm.adjust_responses.append(_adjusted_workout(42.5))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "42.5 on squats")
    save = _last_action(session, "save_plan")
    await _click(dispatcher, bot, _last_action(session, "start"))
    await _click(dispatcher, bot, save)
    assert _toasts(session)[-1] == t("train.already_started", "en")
    assert await _session_status(db, session_id) == "in_progress"


async def test_a_safety_signal_after_the_pain_button_opens_no_second_hold(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, settings: Settings
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # ⚠ is pressed while the model is still running.
        await training.pain_button(db, user_id, session_id, 0)
        parsed = _parsed((1, 8, 20.0), (2, 8, 20.0), (3, 8, 20.0), safety=True)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name, args=parsed.model_dump(mode="json")
                )
            ]
        )

    runtime = LlmRuntime(
        settings=settings,
        prices={},
        agent_factories={
            "result_parse": lambda model: result_parse_agent(
                FunctionModel(respond, model_name=str(model))
            )
        },
    )
    result = await training.parse_results(db, runtime, user_id, session_id, 0, 0, "8 8 8 at 20")
    assert result.status == training.ParseStatus.HALTED and result.halt is None
    assert await _holds(db, user_id) == ["pain_button"]
    assert await _session_status(db, session_id) == "halted"
