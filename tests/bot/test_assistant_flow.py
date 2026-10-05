"""The free-text assistant with sessions and tools (ADR 0003, ADR 0004), end to end through
the real dispatcher and DB. The model is a scripted `FunctionModel` that calls the real tools.

Covers: plan changes go to a planning session's draft (never straight into the plan) and are
saved by the button or by asking; the session remembers the conversation; several tools in
one turn; tool answers let the model correct itself; loads above the guards are limited, not
saved as asked; a started workout is a training session (log, enter results verbatim, skip,
change the remaining blocks, carry the changes into the plan); a stop word halts and an open
hold refuses before any model call; refusals are logged; the model sees pseudonymized data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from conftest import FakeSession, callback_update, make_user, message_update
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import PlanDraft, TrainAction
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan, insert_plan_version
from fitme.db.controllers.profile import (
    delete_setup_progress,
    upsert_profile,
    upsert_screening_flag,
)
from fitme.db.controllers.training import (
    finish_workout_session,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.records import ConversationRecord
from fitme.db.selectors.conversations import list_open_conversations
from fitme.db.selectors.decisions import get_decision, list_decisions_for_user
from fitme.db.selectors.plans import list_plan_versions
from fitme.db.selectors.training import list_set_logs_for_session
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.assistant import AssistantTurn
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, RefusalCode
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, ScheduledDay, Workout
from fitme.domain.results import ParsedResults, SetResult
from fitme.i18n import t
from fitme.llm.agents import assistant_agent, plan_generate_agent, result_parse_agent
from fitme.services import profile as profile_service
from fitme.services import training
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1


@dataclass(frozen=True)
class Call:
    """One tool call the scripted model makes (its own model request)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


def say(message: str) -> AssistantTurn:
    return AssistantTurn(message=message)


Step = Call | AssistantTurn | Refusal | Plan | ParsedResults


@dataclass
class ScriptedLlm:
    steps: list[Step] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)
    calls: int = 0

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Call):
            return ModelResponse(parts=[ToolCallPart(tool_name=step.name, args=step.args)])
        names = [tool.name for tool in info.output_tools]
        wanted = f"final_result_{type(step).__name__}" if len(names) > 1 else names[0]
        assert wanted in names, (wanted, names)
        return ModelResponse(
            parts=[ToolCallPart(tool_name=wanted, args=step.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings) -> LlmRuntime:
        def wrap(factory: Any) -> Any:
            return lambda model: factory(FunctionModel(self._respond, model_name=str(model)))

        return LlmRuntime(
            settings=settings,
            prices={},
            agent_factories={
                "assistant": wrap(assistant_agent),
                "plan_generate": wrap(plan_generate_agent),
                "result_parse": wrap(result_parse_agent),
            },
        )

    def prompts(self) -> list[dict[str, Any]]:
        """Every user prompt sent (one per agent run), parsed."""
        return [
            json.loads(str(part.content))
            for request in self.requests
            for part in request.parts
            if part.part_kind == "user-prompt"
        ]

    def returns(self, tool: str) -> list[Any]:
        """Every answer `tool` gave, in order."""
        return [
            part.content
            for request in self.requests
            for part in request.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == tool
        ]


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: ScriptedLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


def _item(exercise_id: str, load: Load) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=3, reps_min=5, reps_max=5, load=load, rest_seconds=120
    )


def _plan() -> Plan:
    return Plan(
        name="Home plan",
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
                        items=[_item("barbell_back_squat", Load(kind="kg", kg=60))],
                    ),
                    Block(kind="single", items=[_item("pushup", Load(kind="bodyweight"))]),
                    Block(
                        kind="single",
                        items=[_item("dumbbell_bench_press", Load(kind="calibration"))],
                    ),
                ],
            )
        ],
    )


@dataclass
class Seeded:
    user_id: int
    plan_id: int
    session_id: int


async def _ready(dispatcher: Dispatcher, bot: Bot, db: Database) -> Seeded:
    code = await issue_activation_code(db, rebind=False)
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(
        bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=f"/activate {code}")
    )
    async with db.read() as conn:
        account = await get_telegram_account_by_telegram_user_id(conn, OWNER_CHAT_ID)
    assert account is not None
    user_id = account.user_id
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
        await delete_setup_progress(conn, user_id)  # what finishing setup does
        plan = _plan()
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_confirm",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version=content_version(),
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )
        plan_id = await insert_plan(
            conn, user_id=user_id, name=plan.name, is_default=True, status="active"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body=plan.model_dump(mode="json"),
            origin="llm",
            decision_id=decision_id,
        )
        # One finished session: squat 3 × 5 @ 60 kg, so 60 is the logged max and reference.
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="A",
            status="in_progress",
        )
        for set_index in (1, 2, 3):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id="barbell_back_squat",
                set_index=set_index,
                planned_load_kg=60.0,
                planned_reps_min=5,
                planned_reps_max=5,
                actual_load_kg=60.0,
                actual_reps=5,
                rpe=None,
                source="button",
            )
        await finish_workout_session(conn, session_id, status="completed")
    return Seeded(user_id=user_id, plan_id=plan_id, session_id=session_id)


async def _send(dispatcher: Dispatcher, bot: Bot, text: str) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, message_update(user=owner, chat_id=OWNER_CHAT_ID, text=text))


async def _click(dispatcher: Dispatcher, bot: Bot, data: Any) -> None:
    owner = make_user(OWNER_CHAT_ID)
    await dispatcher.feed_update(bot, callback_update(user=owner, chat_id=OWNER_CHAT_ID, data=data))


def _messages(session: FakeSession) -> list[SendMessage]:
    return [m for m in session.sent if isinstance(m, SendMessage)]


def _texts(session: FakeSession) -> list[str]:
    return [m.text or "" for m in _messages(session)]


def _last_text(session: FakeSession) -> str:
    return _texts(session)[-1]


def _datas(message: SendMessage) -> list[str]:
    markup = message.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _last_data(session: FakeSession, prefix: str) -> str:
    for message in reversed(_messages(session)):
        for data in _datas(message):
            if data.startswith(prefix):
                return data
    raise AssertionError(f"no button {prefix!r} was sent")


async def _lang(db: Database, user_id: int) -> str:
    return (await profile_service.get_snapshot(db, user_id)).language


async def _bodies(db: Database, plan_id: int) -> list[Plan]:
    async with db.read() as conn:
        return [Plan.model_validate(v.body) for v in await list_plan_versions(conn, plan_id)]


def _kg(plan: Plan, index: int = 0) -> float | None:
    return plan.workouts[0].blocks[index].items[0].load.kg


def _edit(plan_id: int | None, *ops: dict[str, Any]) -> Call:
    return Call("edit_plan", {"plan_id": plan_id, "ops": list(ops)})


def _squat_kg(kg: float) -> dict[str, Any]:
    return {
        "op": "set_prescription",
        "workout_key": "A",
        "exercise_id": "barbell_back_squat",
        "load": {"kind": "kg", "kg": kg},
    }


async def _open_conversations(db: Database, user_id: int) -> list[ConversationRecord]:
    async with db.read() as conn:
        return await list_open_conversations(conn, user_id)


# --- Planning sessions --------------------------------------------------------------------------


async def test_a_plan_edit_goes_to_a_draft_and_is_saved_with_the_button(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [
        Call("get_plan", {"plan_id": seeded.plan_id}),
        _edit(seeded.plan_id, _squat_kg(62.5)),
        say("Поставил присед 62,5 в черновик."),
    ]
    await _send(dispatcher, bot, "squat 62.5 from now on")

    assert "barbell_back_squat" in json.dumps(llm.returns("get_plan")[0])
    assert llm.returns("edit_plan")[0]["ok"] is True
    # Nothing saved yet: a planning session with a draft round is open.
    assert len(await _bodies(db, seeded.plan_id)) == 1
    open_ = await _open_conversations(db, seeded.user_id)
    assert [(c.kind, c.plan_id) for c in open_] == [("planning", seeded.plan_id)]
    draft_id = open_[0].draft_decision_id
    assert draft_id is not None
    async with db.read() as conn:
        draft = await get_decision(conn, draft_id)
    assert draft is not None and draft.kind == "plan_revise"
    assert draft.prompt_template == "assistant" and draft.load_changes == []
    texts = _texts(session)
    assert texts[-2].startswith("Поставил присед 62,5 в черновик.")
    assert t("assistant.reply_footer", lang) in texts[-2]
    assert texts[-1].startswith(t("assistant.draft_updated", lang, name="Home plan"))
    assert "62.5 kg" in texts[-1]

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=draft_id).pack())
    assert [_kg(b) for b in await _bodies(db, seeded.plan_id)] == [60.0, 62.5]
    assert await _open_conversations(db, seeded.user_id) == []


async def test_the_session_remembers_the_conversation_and_saves_by_asking(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [_edit(seeded.plan_id, _squat_kg(62.5)), say("Готово, присед 62,5.")]
    await _send(dispatcher, bot, "присед 62.5")
    # The next turn names no plan: the open session's draft. Then it saves on request.
    pushups = {"op": "set_prescription", "workout_key": "A", "exercise_id": "pushup", "sets": 4}
    llm.steps = [_edit(None, pushups), Call("save_draft"), say("Сохранил.")]
    await _send(dispatcher, bot, "и отжиманий 4 подхода, сохраняй")

    payload = llm.prompts()[-1]
    assert payload["history"] == [
        {"role": "user", "text": "присед 62.5"},
        {"role": "assistant", "text": "Готово, присед 62,5."},
    ]
    assert payload["state"]["planning_session"]["plan_id"] == seeded.plan_id
    assert payload["state"]["planning_session"]["draft"] is not None
    bodies = await _bodies(db, seeded.plan_id)
    assert len(bodies) == 2
    assert _kg(bodies[-1]) == 62.5 and bodies[-1].workouts[0].blocks[1].items[0].sets == 4
    assert await _open_conversations(db, seeded.user_id) == []


async def test_several_tools_in_one_turn(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    schedule = {
        "op": "set_schedule",
        "days": [{"weekday": 0, "workout_key": "A"}, {"weekday": 3, "workout_key": "A"}],
    }
    llm.steps = [
        _edit(seeded.plan_id, _squat_kg(62.5)),
        _edit(seeded.plan_id, schedule),
        Call("rename_plan", {"plan_id": seeded.plan_id, "name": "Основной"}),
        say("Сделал три изменения."),
    ]
    await _send(dispatcher, bot, "присед 62.5, пн и чт, и назови план Основной")
    texts = _texts(session)
    assert t("assistant.renamed", lang, name="Основной") in texts
    title = t("assistant.draft_updated", lang, name="Home plan")
    draft_text = next(text for text in texts if text.startswith(title))
    assert "62.5 kg" in draft_text and "📅" in draft_text


async def test_a_load_above_the_guards_is_limited_in_the_draft_not_kept(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """AGENTS.md §2: the weekly cap and the ceiling hold in a draft — 80 kg becomes what the
    guards allow, and both the model and the user are told."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [_edit(seeded.plan_id, _squat_kg(80)), say("Ок.")]
    await _send(dispatcher, bot, "squat 80 kg")

    assert llm.returns("edit_plan")[0]["notes"]
    draft_id = (await _open_conversations(db, seeded.user_id))[0].draft_decision_id
    assert draft_id is not None
    async with db.read() as conn:
        draft = await get_decision(conn, draft_id)
    assert draft is not None and draft.proposal is not None
    drafted = _kg(Plan.model_validate(draft.proposal["plan"]))
    assert drafted is not None and drafted <= 62.5
    assert "⚠️" in _last_text(session)


async def test_a_tool_error_lets_the_model_correct_itself_and_nothing_is_staged(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    swap = {
        "op": "swap_exercise",
        "workout_key": "A",
        "exercise_id": "barbell_back_squat",
        "new_exercise_id": "cable_row_made_up",
    }
    llm.steps = [_edit(seeded.plan_id, swap), say("Такого упражнения нет в доступных.")]
    await _send(dispatcher, bot, "замени присед на тягу блока")
    assert llm.returns("edit_plan")[0]["ok"] is False
    assert await _open_conversations(db, seeded.user_id) == []
    assert _last_text(session).startswith("Такого упражнения нет в доступных.")


async def test_close_without_saving(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [_edit(seeded.plan_id, _squat_kg(62.5)), say("Ок.")]
    await _send(dispatcher, bot, "присед 62.5")
    llm.steps = [Call("discard_draft"), say("Закрыл.")]
    await _send(dispatcher, bot, "нет, оставь как было")
    assert _last_text(session) == t("assistant.draft_discarded", lang)
    assert await _open_conversations(db, seeded.user_id) == []
    assert len(await _bodies(db, seeded.plan_id)) == 1


# --- Safety and data ----------------------------------------------------------------------------


async def test_a_stop_word_halts_before_any_model_call_and_a_hold_then_refuses(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    await _send(dispatcher, bot, "sharp pain in my knee, change squats")
    assert llm.calls == 0
    assert _last_text(session) == t("halt.message", lang)

    await _send(dispatcher, bot, "show my plan")
    assert llm.calls == 0
    assert _last_text(session) == t(f"refusal.{RefusalCode.OPEN_HEALTH_HOLD.value}", lang)


async def test_the_model_sees_only_pseudonymized_data(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [Call("list_plans"), say("У вас один план.")]
    await _send(dispatcher, bot, "сколько у меня планов? пишите на a@b.example")
    payload = llm.prompts()[0]
    prompt = json.dumps(payload, ensure_ascii=False)
    assert payload["context"]["user_id"] == seeded.user_id
    assert set(payload) == {"context", "user_request", "state", "history"}
    assert "a@b.example" not in prompt
    assert "telegram" not in prompt.lower() and "chat_id" not in prompt
    assert "telegram" not in json.dumps(llm.returns("list_plans"))


async def test_a_refusal_is_logged_shows_the_model_reason_and_changes_nothing(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [Refusal(code=RefusalCode.OUT_OF_SCOPE, message="Питание вне рамок.")]
    await _send(dispatcher, bot, "что мне есть?")
    assert _last_text(session) == "Питание вне рамок."
    async with db.read() as conn:
        refusals = [
            d for d in await list_decisions_for_user(conn, seeded.user_id) if d.kind == "refusal"
        ]
    assert len(refusals) == 1 and refusals[0].prompt_template == "assistant"
    llm.steps = [Refusal(code=RefusalCode.NEEDS_CLEARANCE, message="free-form model text")]
    await _send(dispatcher, bot, "anything")
    assert _last_text(session) == t("refusal.needs_clearance", lang)


async def test_a_logged_set_can_be_corrected_and_undone(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)

    async def third_set() -> tuple[int | None, float | None]:
        async with db.read() as conn:
            rows = await list_set_logs_for_session(conn, seeded.session_id)
        return rows[2].actual_reps, rows[2].actual_load_kg

    fix = {"exercise_id": "barbell_back_squat", "set_number": 3, "reps": 3}
    llm.steps = [
        Call("get_session", {"session_id": seeded.session_id}),
        Call("fix_logged_sets", {"session_id": seeded.session_id, "fixes": [fix]}),
        say("Исправил."),
    ]
    await _send(dispatcher, bot, "в последней тренировке третий подход приседа был на 3")
    assert await third_set() == (3, 60.0)
    await _click(dispatcher, bot, _last_data(session, "au:"))
    assert await third_set() == (5, 60.0)


async def test_disabled_assistant_falls_back_to_the_menu_hint(
    db: Database, settings: Settings, bot: Bot, session: FakeSession, llm: ScriptedLlm
) -> None:
    off = settings.model_copy(update={"assistant_enabled": False})
    dispatcher = build_dispatcher(db, off, llm.runtime(off))
    seeded = await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "squat 62.5")
    assert llm.calls == 0
    assert _last_text(session) == t("unknown.free_text_hint", await _lang(db, seeded.user_id))


# --- Training sessions --------------------------------------------------------------------------


async def _start_workout(db: Database, settings: Settings, seeded: Seeded) -> int:
    created = await training.create_session(db, seeded.user_id, seeded.plan_id, "A")
    assert isinstance(created, training.SessionCreated)
    session_id = created.session.id
    await training.precheck_no(db, settings, seeded.user_id, session_id)
    started = await training.start(db, settings, seeded.user_id, session_id)
    assert started.status == training.Status.OK
    return session_id


async def _logged(db: Database, session_id: int) -> list[tuple[str, int | None, bool]]:
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    return [(r.exercise_id, r.actual_reps, r.skipped) for r in rows]


async def test_during_a_workout_as_planned_logs_the_block_and_the_session_remembers(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    session_id = await _start_workout(db, settings, seeded)

    llm.steps = [Call("log_current_block", {"skip": False}), say("Записал.")]
    await _send(dispatcher, bot, "по плану")
    state = llm.prompts()[-1]["state"]
    assert state["session"] == "training" and state["current_block"]["block"] == 1
    assert (await _logged(db, session_id))[:3] == [("barbell_back_squat", 5, False)] * 3
    assert t("train.block_logged", lang) in _texts(session)
    assert "Push-up" in _last_text(session)  # the next block

    llm.steps = [Call("log_current_block", {"skip": True}), say("Пропустил.")]
    await _send(dispatcher, bot, "отжимания пропускаю")
    history = llm.prompts()[-1]["history"]
    assert {"role": "user", "text": "по плану"} in history
    assert {"role": "assistant", "text": "Записал."} in history
    assert t("train.block_skipped", lang) in _texts(session)


async def test_during_a_workout_reported_results_go_to_the_parser_verbatim(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    await _start_workout(db, settings, seeded)
    parsed = ParsedResults(
        sets=[SetResult(set_index=i, reps=4, load_kg=60.0) for i in (1, 2, 3)],
        safety_signal=False,
        unclear=False,
    )
    llm.steps = [Call("enter_current_block_results"), say("Отправил на разбор."), parsed]
    await _send(dispatcher, bot, "сделал 4, 4, 4 по 60")
    assert llm.prompts()[-1]["result_text"] == "сделал 4, 4, 4 по 60"
    assert _last_data(session, "tr:parse_ok:")


async def test_the_remaining_blocks_can_change_today_and_be_carried_into_the_plan(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    session_id = await _start_workout(db, settings, seeded)

    # The current block (squat) can't change any more; a later one can.
    squat = {"op": "set_prescription", "exercise_id": "barbell_back_squat", "sets": 5}
    pushup = {"op": "set_prescription", "exercise_id": "pushup", "sets": 2}
    llm.steps = [
        Call("edit_today", {"ops": [squat]}),
        Call("edit_today", {"ops": [pushup]}),
        say("Отжимания сегодня в 2 подхода."),
    ]
    await _send(dispatcher, bot, "отжиманий сегодня только 2 подхода")
    assert [answer["ok"] for answer in llm.returns("edit_today")] == [False, True]
    assert t("assistant.today_changed", lang) in "\n".join(_texts(session))
    async with db.read() as conn:
        started = await training.started_workout(conn, session_id)
    assert started is not None
    assert started.blocks[0].items[0].sets == 3 and started.blocks[1].items[0].sets == 2
    assert await training.changed_mid_session(db, seeded.user_id, session_id)
    assert len(await _bodies(db, seeded.plan_id)) == 1  # today only

    await _click(dispatcher, bot, TrainAction(action="to_plan", session_id=session_id).pack())
    assert _last_text(session).startswith(t("assistant.draft_updated", lang, name="Home plan"))
    draft_id = PlanDraft.unpack(_last_data(session, "pd:confirm:")).decision_id
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=draft_id).pack())
    bodies = await _bodies(db, seeded.plan_id)
    assert len(bodies) == 2 and bodies[-1].workouts[0].blocks[1].items[0].sets == 2


async def test_a_message_with_no_open_block_gets_the_tool_answer(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    await _ready(dispatcher, bot, db)
    llm.steps = [Call("log_current_block"), say("Сейчас нет открытой тренировки.")]
    await _send(dispatcher, bot, "done")
    assert llm.returns("log_current_block")[0]["ok"] is False
    assert "current_block" not in llm.prompts()[0]["state"]
    assert _last_text(session).startswith("Сейчас нет открытой тренировки.")


async def test_a_load_set_during_the_workout_still_passes_the_guards(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """AGENTS.md §2 during a workout: no history for the bench press, so 100 kg is not
    trained — the first session of an exercise is data collection (calibration)."""
    seeded = await _ready(dispatcher, bot, db)
    session_id = await _start_workout(db, settings, seeded)
    bench = {
        "op": "set_prescription",
        "exercise_id": "dumbbell_bench_press",
        "load": {"kind": "kg", "kg": 30},
    }
    llm.steps = [Call("edit_today", {"ops": [bench]}), say("Ок.")]
    await _send(dispatcher, bot, "жим гантелей сегодня 30")
    assert llm.returns("edit_today")[0]["notes"]
    async with db.read() as conn:
        started = await training.started_workout(conn, session_id)
    assert started is not None
    assert started.blocks[2].items[0].load.kind == "calibration"
    assert "⚠️" in _last_text(session)
