"""M8b "Paste my plan" through the real dispatcher: the `/plan` menu's second entry point,
the paste prompt, the pasted program through the stop-word guard and the `plan_import` agent,
the draft with the declared hints and the "Not matched" list, Confirm (`origin = 'import'`),
Change something (the revise flow keeps the hints), Cancel, stale buttons, a provider failure's
retry message, and the same hint in `/train`'s review and block text. The LLM is a
`FunctionModel`; Telegram is the `FakeSession`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from conftest import FakeSession, callback_update, make_user, message_update
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import PlanDraft, PlanMenu, TrainAction, TrainPick
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.selectors.chat import list_chat_messages_for_user
from fitme.db.selectors.decisions import list_decisions_for_user, list_llm_calls_since
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import list_open_health_holds
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS
from fitme.domain.models import Plan, PlanImport, Refusal
from fitme.i18n import t
from fitme.llm.agents import plan_import_agent, plan_revise_agent
from fitme.services import profile as profile_service
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def user_plan_text() -> str:
    return (_FIXTURES / "user_plan.txt").read_text(encoding="utf-8")


def user_plan_import() -> PlanImport:
    return PlanImport.model_validate_json(
        (_FIXTURES / "user_plan_import.json").read_text(encoding="utf-8")
    )


@dataclass
class FakeLlm:
    """`plan_import` and `plan_revise` answer from one queue; an empty queue raises a
    provider error (A§8.5 rule 4), which the bot must show as a retry message."""

    responses: list[PlanImport | Plan | Refusal] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    calls: int = 0

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        for part in request.parts:
            if part.part_kind == "user-prompt":
                self.prompts.append(str(part.content))
        if not self.responses:
            raise ModelHTTPError(status_code=503, model_name="fake", body=None)
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
                "plan_import": lambda model: plan_import_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
                "plan_revise": lambda model: plan_revise_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
            },
        )


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: FakeLlm) -> Dispatcher:
    # These tests prove a dropped prompt no longer consumes the next text via the plain
    # "use the menu" hint; the free-text assistant (ADR 0003) has its own tests.
    settings = settings.model_copy(update={"assistant_enabled": False})
    return build_dispatcher(db, settings, llm.runtime(settings))


async def _activate(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID, language_code="ru")
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None
    return account.user_id


async def _seed_gym_profile(db: Database, user_id: int) -> None:
    """A complete public-gym profile, 3 sessions a week (the sample program's three days)."""
    async with db.transaction() as conn:
        await upsert_profile(
            conn,
            user_id=user_id,
            age_bucket="40_49",
            weight_bucket="120_129",
            experience="2y_5y",
            barbell_experience="yes",
            preferences=["weight_training"],
            location="public_gym",
            equipment=[],
            sessions_per_week=3,
            session_minutes=90,
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
    await profile_service.clear_progress(db, user_id)


async def _ready(dispatcher: Dispatcher, bot: Bot, db: Database) -> int:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_gym_profile(db, user_id)
    return user_id


async def _send(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    owner = make_user(OWNER_CHAT_ID, language_code="ru")
    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=text))


async def _click(dispatcher: Dispatcher, bot: Bot, data: Any) -> None:
    owner = make_user(OWNER_CHAT_ID, language_code="ru")
    await dispatcher.feed_update(bot, callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=data))


def _sent(session: FakeSession) -> list[SendMessage]:
    return [m for m in session.sent if isinstance(m, SendMessage)]


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


def _last_draft_id(session: FakeSession) -> int:
    for message in reversed(_sent(session)):
        for data in _callback_datas(message):
            if data.startswith("pd:confirm:"):
                return PlanDraft.unpack(data).decision_id
    raise AssertionError("no draft keyboard was sent")


def _last_action(session: FakeSession, prefix: str) -> Any:
    for message in reversed(_sent(session)):
        for data in _callback_datas(message):
            if data.startswith(prefix):
                return (
                    TrainAction.unpack(data) if prefix.startswith("tr:") else TrainPick.unpack(data)
                )
    raise AssertionError(f"no {prefix!r} button was sent")


async def _paste(dispatcher: Dispatcher, bot: Bot, session: FakeSession, text: str) -> None:
    await _click(dispatcher, bot, PlanMenu(action="paste", plan_id=0))
    assert _texts(session)[-1] == t("plan.paste_prompt", "ru")
    await _send(dispatcher, bot, text)


_HINT_SQUAT = "калибровка — в вашем плане 80 кг; начните с этого веса или легче"
_HINT_PRESS = "калибровка — в вашем плане 20 кг каждая; начните с этого веса или легче"


# --- Tests -----------------------------------------------------------------------------------


async def test_plan_menu_offers_paste_next_to_new_plan(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "/plan")
    last = _sent(session)[-1]
    datas = _callback_datas(last)
    assert PlanMenu(action="new", plan_id=0).pack() in datas
    assert PlanMenu(action="paste", plan_id=0).pack() in datas


async def test_pasted_program_imports_with_hints_and_unmatched_and_confirm_saves_origin_import(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    expected = user_plan_import()
    llm.responses.append(expected)

    await _paste(dispatcher, bot, session, user_plan_text())

    assert llm.calls == 1
    assert json.loads(llm.prompts[0])["imported_text"] == user_plan_text()
    texts = _texts(session)
    assert t("plan.importing", "ru") in texts
    # The pasted text was saved to chat_messages (A§6.3) before anything else.
    async with db.read() as conn:
        messages = await list_chat_messages_for_user(conn, user_id)
    assert any(m.text == user_plan_text() for m in messages)
    draft = _sent(session)[-1]
    shown = "\n".join(texts[texts.index(t("plan.importing", "ru")) + 1 :])
    assert shown.startswith(t("plan.import_title", "ru"))
    assert "Понедельник: A — Присед + жим сидя" in shown
    assert "Среда: B — Румынская тяга + жим лёжа" in shown
    assert "Пятница: C — Жим штанги + выпады" in shown
    assert f"Приседания со штангой на спине: 3 × 5–5 @ {_HINT_SQUAT}" in shown
    assert _HINT_PRESS in shown
    assert "Планка: 3 × 30–45 @ собственный вес" in shown
    assert t("plan.unmatched_title", "ru") in shown
    for name in expected.unmatched:
        assert f"• {name}" in shown
    assert t("disclosure.ai", "ru") in shown
    decision_id = _last_draft_id(session)
    datas = _callback_datas(draft)
    assert PlanDraft(action="confirm", decision_id=decision_id).pack() in datas
    assert PlanDraft(action="change", decision_id=decision_id).pack() in datas
    assert PlanDraft(action="cancel", decision_id=decision_id).pack() in datas

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=decision_id))

    saved = _texts(session)[-1]
    assert saved.startswith(
        t("plan.saved", "ru", name="Сентябрьский блок, финальная неделя", version=1)
    )
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, plans[0].id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert len(plans) == 1 and plans[0].is_default
    assert [v.origin for v in versions] == ["import"]
    kinds = [d.kind for d in reversed(decisions)]
    assert kinds == ["plan_import", "plan_confirm"]
    draft_decision = next(d for d in decisions if d.kind == "plan_import")
    assert draft_decision.load_changes == []
    assert draft_decision.proposal is not None
    assert draft_decision.proposal["unmatched"] == expected.unmatched
    assert draft_decision.proposal["declared_loads"]["barbell_back_squat"] == 80.0

    # The saved plan shows the same hint, and /train's review and first block do too.
    await _click(dispatcher, bot, PlanMenu(action="view", plan_id=plans[0].id))
    assert _HINT_SQUAT in "\n".join(_texts(session)[-3:])
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_action(session, "tp:workout:"))
    await _click(dispatcher, bot, _last_action(session, "tr:precheck_no:"))
    review = "\n".join(_texts(session)[-3:])
    assert t("train.review_title", "ru", workout_key="A", title="Присед + жим сидя") in review
    assert f"Приседания со штангой на спине: 3 × 5–5 @ {_HINT_SQUAT}" in review
    await _click(dispatcher, bot, _last_action(session, "tr:start:"))
    block = "\n".join(_texts(session)[-3:])
    assert t("train.block_title", "ru", index=1, total=5) in block
    assert f"Подход 1: 5–5 повторений @ {_HINT_SQUAT}" in block
    assert t("train.calibration_hint", "ru") in block


async def test_change_something_on_an_import_draft_revises_and_keeps_the_hints(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(user_plan_import())
    await _paste(dispatcher, bot, session, user_plan_text())
    first_id = _last_draft_id(session)

    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=first_id))
    assert _texts(session)[-1] == t("plan.change_prompt", "ru")
    revised = user_plan_import().plan
    revised.name = "Переименованный"
    for workout in revised.workouts:
        for block in workout.blocks:
            for item in block.items:
                item.declared_kg = None  # a model wouldn't know the hints; the base restores them
                if item.load.kind == "kg":
                    item.load = item.load.model_copy(update={"kind": "calibration", "kg": None})
    llm.responses.append(revised)
    await _send(dispatcher, bot, "переименуй план")

    assert llm.calls == 2
    assert "переименуй план" in llm.prompts[1]
    second_id = _last_draft_id(session)
    assert second_id != first_id
    texts = _texts(session)
    shown = "\n".join(texts[texts.index(t("plan.revising", "ru")) + 1 :])
    assert shown.startswith(t("plan.draft_title", "ru"))
    assert shown.count(_HINT_SQUAT) >= 1 and _HINT_PRESS in shown
    assert t("plan.unmatched_title", "ru") not in shown  # a revision reports nothing unmatched

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=first_id))
    assert _toasts(session)[-1] == t("plan.stale_draft", "ru")
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=second_id))
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, plans[0].id)
    assert [p.name for p in plans] == ["Переименованный"]
    assert [v.origin for v in versions] == ["llm"]
    stored = Plan.model_validate(versions[0].body)
    assert stored.workouts[0].blocks[0].items[0].declared_kg == 80.0


async def test_stop_word_in_the_pasted_text_halts_with_no_llm_call(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(user_plan_import())

    await _paste(
        dispatcher,
        bot,
        session,
        "Сессия 1 (Пн)\nПрисед — 80 × 5 × 3\nвчера колено болит и щёлкнуло",
    )

    assert _texts(session)[-1] == t("halt.message", "ru")
    assert llm.calls == 0
    async with db.read() as conn:
        holds = await list_open_health_holds(conn, user_id)
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert len(holds) == 1 and holds[0].reason == "stop_word"
    assert calls == []
    # The pending paste was dropped: the next text is not treated as a program.
    await _send(dispatcher, bot, "Сессия 1 (Пн)\nПрисед — 80 × 5 × 3")
    assert llm.calls == 0
    assert _texts(session)[-1] == t("unknown.free_text_hint", "ru")
    # And while the hold is open, Paste my plan refuses without a model call.
    await _paste(dispatcher, bot, session, "Сессия 1 (Пн)\nПрисед — 80 × 5 × 3")
    assert _texts(session)[-1] == t("refusal.open_health_hold", "ru")
    assert llm.calls == 0


async def test_provider_failure_shows_the_retry_message_and_cancel_drops_the_prompt(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)

    await _paste(dispatcher, bot, session, user_plan_text())  # an empty queue: 503

    assert llm.calls == 1
    assert _texts(session)[-1] == t("refusal.llm_unavailable", "ru")
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []
        decisions = await list_decisions_for_user(conn, user_id)
    assert [d.kind for d in decisions] == ["plan_import"]
    assert decisions[0].proposal is not None and decisions[0].proposal["cause"] == "provider_error"

    # Cancel under the paste prompt drops it; the next text is ordinary free text.
    await _click(dispatcher, bot, PlanMenu(action="paste", plan_id=0))
    await _click(dispatcher, bot, PlanDraft(action="cancel", decision_id=0))
    assert _texts(session)[-1] == t("plan.paste_cancelled", "ru")
    await _send(dispatcher, bot, user_plan_text())
    assert llm.calls == 1
    assert _texts(session)[-1] == t("unknown.free_text_hint", "ru")


async def test_import_draft_buttons_go_stale_after_a_newer_round(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id = await _ready(dispatcher, bot, db)
    llm.responses.append(user_plan_import())
    await _paste(dispatcher, bot, session, user_plan_text())
    first_id = _last_draft_id(session)
    llm.responses.append(user_plan_import())
    await _paste(dispatcher, bot, session, user_plan_text())
    second_id = _last_draft_id(session)
    assert second_id != first_id

    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=first_id))
    assert _toasts(session)[-1] == t("plan.stale_draft", "ru")
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=first_id))
    assert _toasts(session)[-1] == t("plan.stale_draft", "ru")
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=second_id))
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
    assert len(plans) == 1
    # A second tap is idempotent; a third paste after confirming starts a fresh round.
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=second_id))
    assert _toasts(session)[-1] == t("plan.already_saved", "ru")


async def test_cancel_command_drops_a_pending_paste_or_revision(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    await _ready(dispatcher, bot, db)

    await _click(dispatcher, bot, PlanMenu(action="paste", plan_id=0))
    await _send(dispatcher, bot, "/cancel")
    assert _texts(session)[-1] == t("plan.paste_cancelled", "ru")
    await _send(dispatcher, bot, user_plan_text())
    assert llm.calls == 0
    assert _texts(session)[-1] == t("unknown.free_text_hint", "ru")

    llm.responses.append(user_plan_import())
    await _paste(dispatcher, bot, session, user_plan_text())
    draft_id = _last_draft_id(session)
    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=draft_id))
    assert _texts(session)[-1] == t("plan.change_prompt", "ru")
    await _send(dispatcher, bot, "/cancel")
    assert _texts(session)[-1] == t("plan.change_cancelled", "ru")
    await _send(dispatcher, bot, "убери планку")
    assert llm.calls == 1  # no revise call: the prompt was dropped
    assert _texts(session)[-1] == t("unknown.free_text_hint", "ru")
    # /cancel with nothing pending is the ordinary message.
    await _send(dispatcher, bot, "/cancel")
    assert _texts(session)[-1] == t("cancel.nothing_active", "ru")
