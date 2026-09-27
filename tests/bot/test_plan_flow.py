"""The `/plan` bot flow (A§6.2, A§6.4) end to end through the real dispatcher: list / New
plan, the draft with explicit loads and Confirm / Change something / Cancel, the revise loop
over free text (stop-word guard first), stale buttons, and View / Set default / Archive. The
LLM is a `FunctionModel`; Telegram is the `FakeSession`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from conftest import FakeSession, callback_update, make_user, message_update
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import PlanDraft, PlanMenu
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import list_open_health_holds
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, ScheduledDay, Workout
from fitme.i18n import t
from fitme.llm.agents import plan_generate_agent, plan_revise_agent
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1


@dataclass
class FakeLlm:
    responses: list[Plan | Refusal]
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                self.prompts.append(str(part.content))
        output = self.responses.pop(0)
        wanted = "Refusal" if isinstance(output, Refusal) else "Plan"
        tool = next(
            tool.name
            for tool in info.output_tools
            if wanted in tool.name and (wanted == "Refusal" or "Refusal" not in tool.name)
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=output.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings) -> LlmRuntime:
        return LlmRuntime(
            settings=settings,
            prices={},
            agent_factories={
                "plan_generate": lambda model: plan_generate_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
                "plan_revise": lambda model: plan_revise_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
            },
        )


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm([])


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: FakeLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


def _prescription(exercise_id: str, load: Load) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=3, reps_min=8, reps_max=10, load=load, rest_seconds=90
    )


def make_plan(name: str = "Home plan") -> Plan:
    return Plan(
        name=name,
        schedule=[
            ScheduledDay(weekday=1, workout_key="A"),
            ScheduledDay(weekday=4, workout_key="A"),
        ],
        workouts=[
            Workout(
                key="A",
                title="Full body",
                blocks=[
                    Block(
                        kind="single",
                        items=[_prescription("barbell_back_squat", Load(kind="calibration"))],
                    ),
                    Block(
                        kind="superset",
                        items=[
                            _prescription("dumbbell_bench_press", Load(kind="calibration")),
                            _prescription("pushup", Load(kind="bodyweight")),
                        ],
                    ),
                ],
            )
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


async def _ready(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    return user_id


async def _send(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=text))


async def _click(dispatcher: Dispatcher, bot: Bot, data: Any) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=data))


def _sent_messages(session: FakeSession) -> list[SendMessage]:
    return [m for m in session.sent if isinstance(m, SendMessage)]


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


def _last_draft_id(session: FakeSession) -> int:
    """The decision id on the newest Confirm button sent."""
    for message in reversed(_sent_messages(session)):
        for data in _callback_datas(message):
            if data.startswith("pd:confirm:"):
                return PlanDraft.unpack(data).decision_id
    raise AssertionError("no draft keyboard was sent")


async def _new_draft(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> int:
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    return _last_draft_id(session)


# --- Tests -----------------------------------------------------------------------------------


async def test_plan_without_plans_offers_a_new_plan(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "/plan")
    last = _sent_messages(session)[-1]
    assert last.text == t("plan.none_yet", "en")
    assert PlanMenu(action="new", plan_id=0).pack() in _callback_datas(last)


async def test_new_plan_shows_a_draft_with_explicit_loads_and_confirm_saves_it(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(make_plan())

    decision_id = await _new_draft(dispatcher, bot, session)

    texts = [m.text or "" for m in _sent_messages(session)]
    assert t("plan.generating", "en") in texts
    draft = _sent_messages(session)[-1]
    assert draft.text is not None
    assert draft.text.startswith(t("plan.draft_title", "en"))
    assert "Tuesday: A — Full body" in draft.text and "Friday: A — Full body" in draft.text
    assert "Barbell back squat: 3 × 8–10 @ calibration: start with 20 kg" in draft.text
    assert "Dumbbell bench press: 3 × 8–10 @ calibration: start with 2 kg each" in draft.text
    assert "Push-up: 3 × 8–10 @ bodyweight" in draft.text
    assert t("disclosure.ai", "en") in draft.text
    datas = _callback_datas(draft)
    assert PlanDraft(action="confirm", decision_id=decision_id).pack() in datas
    assert PlanDraft(action="change", decision_id=decision_id).pack() in datas
    assert PlanDraft(action="cancel", decision_id=decision_id).pack() in datas
    assert llm.calls == 1

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=decision_id))

    saved = _sent_messages(session)[-1].text or ""
    assert saved.startswith(t("plan.saved", "en", name="Home plan", version=1))
    assert t("plan.saved_default", "en") in saved
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, plans[0].id)
    assert len(plans) == 1 and plans[0].is_default
    assert [v.version for v in versions] == [1]

    # /plan now lists it, with the default marker and View action.
    await _send(dispatcher, bot, "/plan")
    listing = _sent_messages(session)[-1]
    assert listing.text is not None
    assert "Home plan" in listing.text and t("plan.default_marker", "en") in listing.text
    assert PlanMenu(action="view", plan_id=plans[0].id).pack() in _callback_datas(listing)

    # A second tap on the same Confirm is a no-op toast, not a second plan.
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=decision_id))
    assert _toasts(session)[-1] == t("plan.already_saved", "en")
    async with db.read() as conn:
        assert len(await list_plans_for_user(conn, user_id)) == 1


async def test_change_something_revises_over_free_text_and_confirm_saves_the_revision(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(make_plan())
    first_id = await _new_draft(dispatcher, bot, session)

    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=first_id))
    assert _sent_messages(session)[-1].text == t("plan.change_prompt", "en")

    llm.responses.append(make_plan(name="Revised plan"))
    await _send(dispatcher, bot, "no superset please")

    assert llm.calls == 2
    assert "no superset please" in llm.prompts[1]
    texts = [m.text or "" for m in _sent_messages(session)]
    assert t("plan.revising", "en") in texts
    second_id = _last_draft_id(session)
    assert second_id != first_id

    # The old draft's Confirm is stale now; the new one saves.
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=first_id))
    assert _toasts(session)[-1] == t("plan.stale_draft", "en")
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=second_id))
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
    assert [p.name for p in plans] == ["Revised plan"]

    # Free text with no pending revision falls through to the ordinary hint (no LLM call).
    await _send(dispatcher, bot, "hello")
    assert _sent_messages(session)[-1].text == t("unknown.free_text_hint", "en")
    assert llm.calls == 2


async def test_stop_word_in_revise_text_halts_with_no_llm_call(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(make_plan())
    draft_id = await _new_draft(dispatcher, bot, session)
    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=draft_id))
    llm.responses.append(make_plan(name="never used"))

    await _send(dispatcher, bot, "make it lighter, my knee hurts with sharp pain")

    assert _sent_messages(session)[-1].text == t("halt.message", "en")
    assert llm.calls == 1  # the revise model was never invoked
    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
    assert len(holds) == 1 and holds[0].reason == "stop_word"

    # The pending revision was dropped: later text is not sent to the LLM either.
    await _send(dispatcher, bot, "ok now revise it")
    assert llm.calls == 1
    assert _sent_messages(session)[-1].text == t("unknown.free_text_hint", "en")

    # And while the hold is open, New plan refuses without a model call.
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    assert _sent_messages(session)[-1].text == t("refusal.open_health_hold", "en")
    assert llm.calls == 1


async def test_cancel_discards_the_draft_and_llm_refusals_are_shown_by_code(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(make_plan())
    draft_id = await _new_draft(dispatcher, bot, session)

    await _click(dispatcher, bot, PlanDraft(action="cancel", decision_id=draft_id))
    assert _sent_messages(session)[-1].text == t("plan.change_cancelled", "en")
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []

    llm.responses.append(Refusal(code="out_of_scope", message="model text, never shown"))
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    assert _sent_messages(session)[-1].text == t("refusal.out_of_scope", "en")

    # A confirm for a draft that was superseded by a refusal round is stale.
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=draft_id))
    assert _toasts(session)[-1] == t("plan.stale_draft", "en")


async def test_view_set_default_revise_and_archive_from_the_list(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    for name in ("One", "Two"):
        llm.responses.append(make_plan(name=name))
        draft_id = await _new_draft(dispatcher, bot, session)
        await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=draft_id))
    async with db.read() as conn:
        plans = {p.name: p for p in await list_plans_for_user(conn, user_id)}
    one, two = plans["One"], plans["Two"]
    assert one.is_default and not two.is_default

    await _click(dispatcher, bot, PlanMenu(action="view", plan_id=two.id))
    view = _sent_messages(session)[-1]
    assert view.text is not None and view.text.startswith("Two (version 1, active)")
    assert "@ calibration" in view.text
    datas = _callback_datas(view)
    assert PlanMenu(action="default", plan_id=two.id).pack() in datas
    assert PlanMenu(action="revise", plan_id=two.id).pack() in datas
    assert PlanMenu(action="archive", plan_id=two.id).pack() in datas

    await _click(dispatcher, bot, PlanMenu(action="default", plan_id=two.id))
    assert _toasts(session)[-1] == t("plan.default_set", "en", name="Two")

    await _click(dispatcher, bot, PlanMenu(action="revise", plan_id=two.id))
    assert _sent_messages(session)[-1].text == t("plan.change_prompt", "en")
    llm.responses.append(make_plan(name="Two b"))
    await _send(dispatcher, bot, "swap the push-ups")
    revised_id = _last_draft_id(session)
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=revised_id))
    saved = _sent_messages(session)[-1].text or ""
    assert saved.startswith(t("plan.saved", "en", name="Two", version=2))
    async with db.read() as conn:
        assert [v.version for v in await list_plan_versions(conn, two.id)] == [1, 2]

    await _click(dispatcher, bot, PlanMenu(action="archive", plan_id=two.id))
    assert _toasts(session)[-1] == t("plan.archived", "en", name="Two")
    async with db.read() as conn:
        plans = {p.name: p for p in await list_plans_for_user(conn, user_id)}
    assert plans["Two"].status == "archived" and not plans["Two"].is_default
    assert plans["One"].is_default

    # A button for a plan that doesn't exist is a stale toast.
    await _click(dispatcher, bot, PlanMenu(action="view", plan_id=999))
    assert _toasts(session)[-1] == t("errors.stale_callback", "en")


async def test_long_plan_is_split_into_several_messages(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    await _ready(dispatcher, bot, db)
    base = make_plan()
    big = Plan(
        name="Big",
        schedule=base.schedule,
        workouts=[Workout(key="A", title="Full body", blocks=base.workouts[0].blocks * 40)],
    )
    llm.responses.append(big)
    before = len(_sent_messages(session))

    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))

    chunks = _sent_messages(session)[before + 1 :]  # after the "generating" message
    assert len(chunks) > 1
    assert all(len(m.text or "") <= 4096 for m in chunks)
    assert all(not _callback_datas(m) for m in chunks[:-1])
    assert _callback_datas(chunks[-1])  # the buttons ride on the last chunk
