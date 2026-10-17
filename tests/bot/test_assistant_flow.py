"""The free-text assistant with sessions and tools (ADR 0003, ADR 0004), end to end through
the real dispatcher and DB. The model is a scripted `FunctionModel` that calls the real tools.

Covers: plan changes go to a planning session's draft (never straight into the plan) and are
saved by the button or by asking; the session remembers the conversation; several tools in
one turn; tool answers let the model correct itself; loads above the guards are limited, not
saved as asked; a started workout is a training session (log each exercise as planned, with
its numbers or skipped, only after the checks pass; a reported safety concern halts; change
the remaining blocks, carry the changes into the plan); a stop word halts and an open hold
refuses before any model call; refusals are logged; the model sees pseudonymized data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import EditMessageText, SendMessage
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.bot import waiting
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import PlanDraft, PlanMenu, TrainAction
from fitme.catalog import load_catalog
from fitme.cli.commands import sync_search_index
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
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, Measure, RefusalCode
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, ScheduledDay, Workout
from fitme.domain.results import MAX_EXTRA_SETS
from fitme.i18n import t
from fitme.llm.agents import assistant_agent, plan_generate_agent
from fitme.services import exclusions, planning, training
from fitme.services import profile as profile_service
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime
from tests.bot.conftest import FakeSession, callback_update, make_user, message_update

OWNER_CHAT_ID = 1


@dataclass(frozen=True)
class Call:
    """One tool call the scripted model makes (its own model request)."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


def say(message: str) -> AssistantTurn:
    return AssistantTurn(message=message)


Step = Call | AssistantTurn | Refusal | Plan


@dataclass
class ScriptedLlm:
    steps: list[Step] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)
    calls: int = 0
    # Experiment (llm-first planning): outside a started workout, free text goes to the
    # `planner`. The tests in this module cover the legacy assistant's tools (`edit_plan`,
    # `rewrite_plan`, `new_plan`, ...), which stay in the code, so by default they run the
    # legacy agent under the planner's name; `test_planner_flow.py` passes `planner_agent`.
    planner_factory: Any = assistant_agent

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
                "planner": wrap(self.planner_factory),
                "plan_generate": wrap(plan_generate_agent),
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


async def _ready(
    dispatcher: Dispatcher, bot: Bot, db: Database, plan: Plan | None = None
) -> Seeded:
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
        plan = plan or _plan()
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
    return session.chat_texts()


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


async def _drafts_open(db: Database, user_id: int) -> list[int]:
    """The draft ids of the open sessions. Experiment (llm-first planning): a planner turn
    outside any session opens a new plan's planning session to remember the exchange, so
    "nothing staged" means no draft, not no session."""
    return [
        record.draft_decision_id
        for record in await _open_conversations(db, user_id)
        if record.draft_decision_id is not None
    ]


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


async def test_the_reply_is_written_into_the_waiting_placeholder(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    await _ready(dispatcher, bot, db)
    before = len(session.sent)
    llm.steps = [say("Ок.")]
    await _send(dispatcher, bot, "hello")

    turn = session.sent[before:]
    placeholder, reply = turn[0], turn[-1]
    assert isinstance(placeholder, SendMessage)
    assert placeholder.text in {waiting.frames(runner)[0] for runner in waiting.RUNNERS}
    assert isinstance(reply, EditMessageText) and reply.text.startswith("Ок.")
    assert [m for m in turn if isinstance(m, SendMessage)] == [placeholder]


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


async def test_a_redesign_while_a_new_plans_draft_is_open_never_targets_a_saved_plan(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    new_plan = _plan().model_copy(update={"name": "Boxing"}, deep=True)
    # ADR 0009: a generated plan warms up before its heavy squat (else a repair round runs).
    warm_up = _item("mobility_cat_cow", Load(kind="bodyweight"))
    new_plan.workouts[0].blocks.insert(0, Block(kind="single", items=[warm_up]))
    llm.steps = [Call("new_plan", {"guidance": "второй план"}), say("Составляю."), new_plan]
    await _send(dispatcher, bot, "план для бокса")
    [conversation] = await _open_conversations(db, seeded.user_id)
    assert conversation.plan_id is None and conversation.draft_decision_id is not None

    rewrite = {"plan_id": seeded.plan_id, "request": "меньше плеч, добавь скакалку"}
    llm.steps = [Call("rewrite_plan", rewrite), say("Уточните.")]
    await _send(dispatcher, bot, "меньше плеч, добавь скакалку")

    answer = llm.returns("rewrite_plan")[0]
    assert answer["ok"] is False and "plan_id null" in answer["error"]
    assert len(await _bodies(db, seeded.plan_id)) == 1  # the saved plan is untouched


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
    assert await _drafts_open(db, seeded.user_id) == []
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


async def _logged_kg(db: Database, session_id: int) -> list[tuple[str, int | None, float | None]]:
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    return [
        (r.exercise_id, r.actual_reps, r.actual_load_kg) for r in rows if r.actual_reps is not None
    ]


async def test_during_a_workout_as_planned_logs_the_exercise_and_the_session_remembers(
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

    squat = {"exercise_id": "barbell_back_squat"}
    llm.steps = [Call("log_exercise_as_planned", squat), say("Записал.")]
    await _send(dispatcher, bot, "по плану")
    state = llm.prompts()[-1]["state"]
    assert state["session"] == "training" and state["current_block"]["block"] == 1
    assert (await _logged(db, session_id))[:3] == [("barbell_back_squat", 5, False)] * 3
    # ADR 0013: the sets written are always listed, one compact line, after the reply.
    texts = _texts(session)
    reply = next(i for i, text in enumerate(texts) if text.startswith("Записал."))
    assert texts[reply + 1] == "✓ Barbell back squat: 3 × 5 @ 62.5 kg"
    assert t("train.block_logged", lang) not in "\n".join(texts)
    assert "Push-up" in _last_text(session)  # the next block

    llm.steps = [Call("skip_exercise", {"exercise_id": "pushup"}), say("Пропустил.")]
    await _send(dispatcher, bot, "отжимания пропускаю")
    history = llm.prompts()[-1]["history"]
    assert {"role": "user", "text": "по плану"} in history
    assert {"role": "assistant", "text": "Записал."} in history
    assert [r for r in await _logged(db, session_id) if r[0] == "pushup"] == [
        ("pushup", None, True)
    ] * 3


async def test_during_a_workout_reported_results_are_logged_without_a_confirm_step(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """ADR 0005: the assistant's numbers are written at once (no Correct / Fix), as a
    `result_parse` decision with the assistant's prompt and the plausibility verdicts."""
    seeded = await _ready(dispatcher, bot, db)
    session_id = await _start_workout(db, settings, seeded)
    sets = [
        {"set_index": 1, "reps": 5, "load_kg": 60},
        {"set_index": 2, "reps": 4, "load_kg": 60},
        {"set_index": 3, "skipped": True},
    ]
    llm.steps = [
        Call("log_exercise_results", {"exercise_id": "barbell_back_squat", "sets": sets}),
        say("Записал."),
    ]
    await _send(dispatcher, bot, "5 и 4 по 60, третий не делал")
    assert llm.returns("log_exercise_results")[0]["ok"] is True
    assert await _logged_kg(db, session_id) == [
        ("barbell_back_squat", 5, 60.0),
        ("barbell_back_squat", 4, 60.0),
    ]
    assert not any(d.startswith("tr:parse_ok:") for m in _messages(session) for d in _datas(m))
    assert "Push-up" in _last_text(session)
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, seeded.user_id)
    logged = [d for d in decisions if (d.user_report or {}).get("event") == "assistant_log"]
    assert len(logged) == 1
    assert logged[0].kind == "result_parse" and logged[0].prompt_template == "assistant"
    assert logged[0].user_report is not None
    assert logged[0].user_report["logged_as"] == "results"
    assert logged[0].guards_fired and all(v["ok"] for v in logged[0].guards_fired)


async def test_the_sets_written_are_listed_when_the_reply_is_dropped(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """With no reply of the assistant's own (here: removed by the wording check), the bot
    still says what was written."""
    seeded = await _ready(dispatcher, bot, db)
    await _start_workout(db, settings, seeded)
    llm.steps = [
        Call("log_exercise_as_planned", {"exercise_id": "barbell_back_squat"}),
        say("Logged, says your coach."),
    ]
    await _send(dispatcher, bot, "по плану")
    texts = "\n".join(_texts(session))
    assert "coach" not in texts
    assert "✓ Barbell back squat: 3 × 5 @ 62.5 kg" in texts


async def test_a_turn_that_only_logged_shows_just_what_was_written(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """assistant.v10: the model leaves its message empty after a plain log, so the stored
    sets are said once, by the app — no "Записал …" repeating them, no AI footer."""
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    await _start_workout(db, settings, seeded)
    before = len(_texts(session))
    llm.steps = [Call("log_exercise_as_planned", {"exercise_id": "barbell_back_squat"}), say("")]
    await _send(dispatcher, bot, "по плану")

    new = _texts(session)[before:]
    assert new[0] == "✓ Barbell back squat: 3 × 5 @ 62.5 kg"
    assert not any(t("assistant.reply_footer", lang) in text for text in new)
    assert t("assistant.not_understood", lang) not in new
    assert "Push-up" in new[-1]  # the next block


async def test_an_implausible_or_incomplete_report_is_refused_by_the_tool(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """Nothing is written unless every set is named and every load passes
    `guards.plausibility` (there is no confirmation step)."""
    seeded = await _ready(dispatcher, bot, db)
    session_id = await _start_workout(db, settings, seeded)
    typo = [{"set_index": i, "reps": 5, "load_kg": 200} for i in (1, 2, 3)]
    partial = [{"set_index": 1, "reps": 5, "load_kg": 60}]
    llm.steps = [
        Call("log_exercise_results", {"exercise_id": "barbell_back_squat", "sets": typo}),
        Call("log_exercise_results", {"exercise_id": "barbell_back_squat", "sets": partial}),
        Call("log_exercise_as_planned", {"exercise_id": "pushup"}),  # not on screen yet
        say("Проверьте, пожалуйста, вес: 200 кг?"),
    ]
    await _send(dispatcher, bot, "3 по 5 с 200")
    answers = llm.returns("log_exercise_results") + llm.returns("log_exercise_as_planned")
    assert [answer["ok"] for answer in answers] == [False, False, False]
    assert "implausible" in answers[0]["error"]
    assert "exactly once" in answers[1]["error"]
    assert "not an open exercise" in answers[2]["error"]
    assert await _logged_kg(db, session_id) == []
    assert _last_text(session).startswith("Проверьте, пожалуйста, вес")


async def test_sets_done_beyond_the_prescription_are_logged_as_reported(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """ADR 0012: "сделал пять подходов" of three planned is five sets in the log, not "as
    planned"; the extra rows don't shift which rows belong to the next block."""
    seeded = await _ready(dispatcher, bot, db)
    session_id = await _start_workout(db, settings, seeded)
    five = [{"set_index": i, "reps": 5} for i in range(1, 6)]
    skipped_extra = [*five[:3], {"set_index": 4, "skipped": True}]
    too_many = [{"set_index": i, "reps": 5} for i in range(1, 3 + MAX_EXTRA_SETS + 2)]
    gap = [*five[:3], {"set_index": 5, "reps": 5}]
    squat = {"exercise_id": "barbell_back_squat"}
    llm.steps = [
        Call("log_exercise_results", {**squat, "sets": skipped_extra}),
        Call("log_exercise_results", {**squat, "sets": too_many}),
        Call("log_exercise_results", {**squat, "sets": gap}),
        Call("log_exercise_results", {**squat, "sets": five}),
        say("Записал пять подходов."),
    ]
    await _send(dispatcher, bot, "сделал пять подходов")

    assert [a["ok"] for a in llm.returns("log_exercise_results")] == [False, False, False, True]
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    squats = [row for row in rows if row.exercise_id == "barbell_back_squat"]
    assert [(row.set_index, row.actual_reps, row.extra) for row in squats] == [
        (1, 5, False), (2, 5, False), (3, 5, False), (4, 5, True), (5, 5, True),
    ]  # fmt: skip
    extra = squats[3]
    assert (extra.extra_for_block, extra.extra_for_item) == (0, 0)
    assert extra.planned_reps_max is None
    assert extra.planned_load_kg == extra.actual_load_kg == squats[0].planned_load_kg
    assert "Push-up" in _last_text(session)  # the block advanced
    assert any(row.exercise_id == "pushup" for row in rows)  # the next block's rows exist

    active = await training.active_session(db, seeded.user_id)
    assert active is not None
    assigned = training.assign_rows(rows, active.workout)
    assert [row.set_index for row in assigned[0][0]] == [1, 2, 3, 4, 5]
    assert all(len(item_rows) == 3 for item_rows in assigned[1])  # push-ups: as prescribed


async def test_a_calibration_load_needs_the_weight_used(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    plan = _plan()
    plan.workouts[0].blocks.reverse()  # the calibration bench press first
    seeded = await _ready(dispatcher, bot, db, plan)
    session_id = await _start_workout(db, settings, seeded)
    bench = {"exercise_id": "dumbbell_bench_press"}
    no_kg = [{"set_index": i, "reps": 8} for i in (1, 2, 3)]
    with_kg = [{"set_index": i, "reps": 8, "load_kg": 12} for i in (1, 2, 3)]
    llm.steps = [
        Call("log_exercise_as_planned", bench),
        Call("log_exercise_results", {**bench, "sets": no_kg}),
        Call("log_exercise_results", {**bench, "sets": with_kg}),
        say("Записал."),
    ]
    await _send(dispatcher, bot, "жим 3 по 8 с 12")
    assert [a["ok"] for a in llm.returns("log_exercise_as_planned")] == [False]
    assert [a["ok"] for a in llm.returns("log_exercise_results")] == [False, True]
    assert [r for r in await _logged_kg(db, session_id) if r[0] == "dumbbell_bench_press"] == [
        ("dumbbell_bench_press", 8, 12.0)
    ] * 3


async def test_a_superset_is_logged_one_exercise_at_a_time(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    plan = _plan()
    first, second = plan.workouts[0].blocks[0], plan.workouts[0].blocks[1]
    plan.workouts[0].blocks[:2] = [Block(kind="superset", items=[*first.items, *second.items])]
    seeded = await _ready(dispatcher, bot, db, plan)
    lang = await _lang(db, seeded.user_id)
    session_id = await _start_workout(db, settings, seeded)

    llm.steps = [Call("log_exercise_as_planned", {"exercise_id": "pushup"}), say("Записал.")]
    await _send(dispatcher, bot, "отжимания сделал")
    assert [r[0] for r in await _logged_kg(db, session_id)] == ["pushup"] * 3
    assert t("train.block_title", lang, index=1, total=2) in _last_text(session)  # still open

    llm.steps = [
        Call("log_exercise_as_planned", {"exercise_id": "pushup"}),  # already logged
        Call("log_exercise_as_planned", {"exercise_id": "barbell_back_squat"}),
        say("Записал."),
    ]
    await _send(dispatcher, bot, "и присед")
    assert [a["ok"] for a in llm.returns("log_exercise_as_planned")] == [True, False, True]
    assert t("train.block_title", lang, index=2, total=2) in _last_text(session)


async def test_a_reported_safety_concern_halts_and_applies_nothing_else(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """A§7.2: the model can add a halt (no stop word matches "колено ведёт себя
    странно"), never remove one; what else it staged in the same turn is dropped."""
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    session_id = await _start_workout(db, settings, seeded)
    llm.steps = [
        Call("log_exercise_as_planned", {"exercise_id": "barbell_back_squat"}),
        Call("report_safety_concern"),
        say("Записал."),
    ]
    await _send(dispatcher, bot, "присед сделал, колено ведёт себя странно, как будто нестабильно")
    assert _last_text(session) == t("halt.message", lang)
    assert await _logged_kg(db, session_id) == []
    assert await training.active_session(db, seeded.user_id) is None
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, seeded.user_id)
    halts = [d for d in decisions if d.kind == "session_halt"]
    assert len(halts) == 1 and halts[0].user_report is not None
    assert halts[0].user_report["trigger"] == "llm_safety_signal"


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
    llm.steps = [
        Call("log_exercise_as_planned", {"exercise_id": "barbell_back_squat"}),
        Call("report_safety_concern"),
        say("Сейчас нет открытой тренировки."),
    ]
    await _send(dispatcher, bot, "done")
    assert llm.returns("log_exercise_as_planned")[0]["ok"] is False
    assert llm.returns("report_safety_concern")[0]["ok"] is False
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


async def test_the_assistant_finds_exercises_and_ranked_substitutes(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    settings: Settings,
) -> None:
    """ADR 0007: `find_exercises` is hybrid search over the allowed set, and
    `find_substitutes` ranks allowed replacements; neither stages anything."""
    seeded = await _ready(dispatcher, bot, db)
    await sync_search_index(db)
    llm.steps = [
        Call("find_exercises", {"query": "жим гантелей"}),
        Call(
            "find_substitutes",
            {"exercise_id": "barbell_back_squat", "avoid_areas": ["lower_back"]},
        ),
        Call("find_substitutes", {"exercise_id": "no_such_id", "avoid_areas": []}),
        Call("find_substitutes", {"exercise_id": "barbell_back_squat", "avoid_areas": ["toe"]}),
        say("Вот варианты."),
    ]
    await _send(dispatcher, bot, "чем заменить присед, чтобы не грузить поясницу?")

    found = llm.returns("find_exercises")[0]
    assert found and found[0]["exercise_id"] == "dumbbell_bench_press"
    substitutes, unknown, bad_area = llm.returns("find_substitutes")
    catalog = load_catalog()
    assert substitutes and substitutes[0]["close"] is True
    for item in substitutes:
        exercise = catalog.by_id(item["exercise_id"])
        assert exercise is not None and "lower_back" not in exercise.loads_areas
        assert item["exercise_id"] != "barbell_back_squat"
    assert "error" in unknown and "error" in bad_area
    # ADR 0007: every result is something this user may already do (location, equipment,
    # screening) — retrieval only orders the allowed set.
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, seeded.user_id)
    allowed = set(
        planning.build_inputs(
            catalog, snapshot, settings, seeded.user_id
        ).user_context.allowed_exercise_ids
    )
    assert {item["exercise_id"] for item in found} <= allowed
    assert {item["exercise_id"] for item in substitutes} <= allowed
    assert await _drafts_open(db, seeded.user_id) == []  # nothing staged


async def test_a_timed_and_a_distance_set_are_shown_and_logged_in_their_measure(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """ADR 0009: "Plank 3 × 40 s" and "Farmer carry 3 × 30 m", logged as seconds and meters
    by Done as planned — never "3 × 1" with the duration in a note."""
    plan = _plan()
    plank = Prescription(
        exercise_id="plank",
        sets=3,
        measure=Measure.TIME,
        seconds=40,
        load=Load(kind="bodyweight"),
        rest_seconds=45,
    )
    carry = Prescription(
        exercise_id="dumbbell_farmer_carry",
        sets=3,
        measure=Measure.DISTANCE,
        meters=30,
        load=Load(kind="calibration"),
        rest_seconds=60,
    )
    plan.workouts[0].blocks = [
        Block(kind="single", items=[plank]),
        Block(kind="single", items=[carry]),
    ]
    seeded = await _ready(dispatcher, bot, db, plan)
    session_id = await _start_workout(db, settings, seeded)

    await training.complete_block_as_planned(db, seeded.user_id, session_id, 0)
    await training.complete_block_as_planned(db, seeded.user_id, session_id, 1)

    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    assert [(r.exercise_id, r.planned_seconds, r.actual_seconds) for r in rows[:3]] == [
        ("plank", 40, 40)
    ] * 3
    assert [(r.planned_meters, r.actual_meters, r.actual_reps) for r in rows[3:]] == [
        (30, 30, None)
    ] * 3
    assert all(row.performed for row in rows)


async def test_a_timed_exercise_done_with_an_extra_set_is_logged_in_seconds(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """ADR 0012, the reported case: "сделал три подхода" of a timed exercise. The model
    sees the block's seconds target, so it can give the extra set the planned time."""
    plan = _plan()
    shadow = Prescription(
        exercise_id="plank",
        sets=2,
        measure=Measure.TIME,
        seconds=120,
        load=Load(kind="bodyweight"),
        rest_seconds=45,
    )
    plan.workouts[0].blocks = [Block(kind="single", items=[shadow]), *plan.workouts[0].blocks]
    seeded = await _ready(dispatcher, bot, db, plan)
    session_id = await _start_workout(db, settings, seeded)
    three = [{"set_index": i, "seconds": 120} for i in (1, 2, 3)]
    llm.steps = [
        Call("log_exercise_results", {"exercise_id": "plank", "sets": three}),
        say("Записал три подхода."),
    ]
    await _send(dispatcher, bot, "сделал три подхода")

    shown = llm.prompts()[-1]["state"]["current_block"]["exercises"][0]
    assert (shown["sets"], shown["measure"], shown["seconds"]) == (2, "time", 120)
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    planks = [row for row in rows if row.exercise_id == "plank"]
    assert [(r.set_index, r.planned_seconds, r.actual_seconds, r.extra) for r in planks] == [
        (1, 120, 120, False), (2, 120, 120, False), (3, None, 120, True),
    ]  # fmt: skip


async def test_never_squats_is_stored_for_every_plan_and_shown_with_what_it_covers(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """ADR 0009/0010: an explicit "never" is an exclusion for every plan: stored after the
    turn, reported with examples, listed in the next turn's state, removable from /profile."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [
        Call("add_exclusion", {"kind": "name", "value": "squat", "for_all_plans": True}),
        Call("add_exclusion", {"kind": "pattern", "value": "legs", "for_all_plans": True}),
        say("Больше никаких приседаний."),
    ]
    await _send(dispatcher, bot, "никогда не предлагай приседания")

    added, invalid = llm.returns("add_exclusion")
    assert added["ok"] is True and added["excludes"] > 10
    assert added["applies_to"] == "every plan"
    assert invalid["ok"] is False
    stored = await exclusions.list_for_user(db, seeded.user_id)
    assert [(row.kind, row.value, row.scope) for row in stored] == [("name", "squat", "all")]
    lang = await _lang(db, seeded.user_id)
    label = exclusions.label("name", "squat", load_catalog(), lang)
    assert t("assistant.exclusion_added", lang, name=label) in "\n".join(_texts(session))

    llm.steps = [say("ok")]
    await _send(dispatcher, bot, "что исключено?")
    state = llm.prompts()[-1]["state"]
    assert state["exclusions"] == [
        {"exclusion_id": stored[0].id, "kind": "name", "value": "squat", "scope": "all"}
    ]

    await _send(dispatcher, bot, "/profile")
    remove = _last_data(session, "xr:")
    await _click(dispatcher, bot, remove)
    assert await exclusions.list_for_user(db, seeded.user_id) == []


async def test_no_squats_while_working_on_a_plan_applies_to_that_plan_only(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """ADR 0010, the observed failure: "убери приседания" in a planning session became a
    profile-wide exclusion. Now it belongs to the session's plan: /profile doesn't list it,
    the plan's next draft can't keep a squat, and another plan is untouched."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [_edit(seeded.plan_id, _squat_kg(62.5)), say("Ок.")]
    await _send(dispatcher, bot, "присед 62,5")  # opens a planning session for the plan

    llm.steps = [Call("add_exclusion", {"kind": "name", "value": "squat"}), say("Убрал.")]
    await _send(dispatcher, bot, "убери приседания")
    added = llm.returns("add_exclusion")[0]
    assert added["ok"] is True and added["applies_to"] == "this plan only"
    stored = await exclusions.list_for_user(db, seeded.user_id)
    assert [(row.scope, row.plan_id) for row in stored] == [("plan", seeded.plan_id)]
    lang = await _lang(db, seeded.user_id)
    label = exclusions.label("name", "squat", load_catalog(), lang)
    assert t("assistant.exclusion_added_plan", lang, name=label) in "\n".join(_texts(session))
    assert await exclusions.list_for_all_plans(db, seeded.user_id) == []

    # The plan still has its squat: a draft that keeps it fails `plan.excluded` and isn't
    # written; the session's draft stays the previous one.
    before = (await _open_conversations(db, seeded.user_id))[0].draft_decision_id
    llm.steps = [_edit(seeded.plan_id, _squat_kg(65)), say("Ок.")]
    await _send(dispatcher, bot, "присед 65")
    refused = llm.returns("edit_plan")[-1]
    assert refused["ok"] is False and "excluded" in refused["error"]
    assert (await _open_conversations(db, seeded.user_id))[0].draft_decision_id == before
    async with db.read() as conn:
        scoped = await planning.read_snapshot(conn, seeded.user_id, plan_id=seeded.plan_id)
    assert [row.value for row in scoped.exclusions] == ["squat"]

    # Another plan (a new one) is not affected.
    async with db.read() as conn:
        new_plan = await planning.read_snapshot(conn, seeded.user_id, new_plan=True)
    assert new_plan.exclusions == []


async def test_an_exclusion_with_no_plan_in_work_and_no_never_is_not_stored(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [
        Call("add_exclusion", {"kind": "name", "value": "squat"}),
        say("Для какого плана?"),
    ]
    await _send(dispatcher, bot, "без приседаний")
    answer = llm.returns("add_exclusion")[0]
    assert answer["ok"] is False and "for_all_plans" in answer["error"]
    assert await exclusions.list_for_user(db, seeded.user_id) == []


def _boxing_plan() -> Plan:
    plan = _plan().model_copy(update={"name": "Boxing"}, deep=True)
    # ADR 0009: a generated plan warms up before its heavy squat (else a repair round runs).
    warm_up = _item("mobility_cat_cow", Load(kind="bodyweight"))
    plan.workouts[0].blocks.insert(0, Block(kind="single", items=[warm_up]))
    return plan


async def test_a_new_plans_exclusion_reaches_its_generation_and_moves_to_it_when_saved(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """ADR 0010: "new plan, no lunges" excludes for the new plan's draft (the generation
    sees it), and saving the draft makes it that plan's exclusion; nothing for all plans."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [
        Call("new_plan", {"guidance": "второй план"}),
        Call("add_exclusion", {"kind": "name", "value": "lunge"}),
        say("Составляю без выпадов."),
        _boxing_plan(),
    ]
    await _send(dispatcher, bot, "новый план для бокса, без выпадов")
    assert llm.returns("add_exclusion")[0]["applies_to"] == "this plan only"
    [row] = await exclusions.list_for_user(db, seeded.user_id)
    assert (row.scope, row.plan_id) == ("new_plan", None)
    assert "name:lunge" in llm.prompts()[-1]["context"]["exclusions"]

    llm.steps = [Call("save_draft"), say("Сохранил.")]
    await _send(dispatcher, bot, "сохрани")
    [row] = await exclusions.list_for_user(db, seeded.user_id)
    assert row.scope == "plan" and row.plan_id not in (None, seeded.plan_id)
    assert await exclusions.list_for_all_plans(db, seeded.user_id) == []


async def test_a_new_plans_exclusion_goes_with_its_draft_when_closed_unsaved(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [
        Call("new_plan", {"guidance": "второй план"}),
        Call("add_exclusion", {"kind": "name", "value": "lunge"}),
        say("Составляю."),
        _boxing_plan(),
    ]
    await _send(dispatcher, bot, "новый план для бокса, без выпадов")
    assert len(await exclusions.list_for_user(db, seeded.user_id)) == 1

    llm.steps = [Call("discard_draft"), say("Закрыл.")]
    await _send(dispatcher, bot, "не надо")
    assert await exclusions.list_for_user(db, seeded.user_id) == []


async def test_the_new_plans_description_goes_to_the_assistant_which_may_ask_first(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """ADR 0017: the answer to "what should the new plan be?" reaches the assistant in the
    new plan's session, with the question in its history. It can ask one clarifying
    question; the user's answer then generates, framed by the request it passes on."""
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    question = _last_text(session)
    assert question == t("plan.new_guidance_prompt", lang, names="“Home plan”")

    clarify = "BJJ здесь — сами тренировки или ОФП под них?"
    llm.steps = [say(clarify)]
    await _send(dispatcher, bot, "bjj два раза в неделю")
    seen = llm.prompts()[-1]
    assert seen["user_request"] == "bjj два раза в неделю"
    assert {"role": "assistant", "text": question} in seen["history"]
    assert clarify in _last_text(session)
    assert not [p for p in llm.prompts() if "programming_brief" in p]  # nothing generated

    guidance = "ОФП под BJJ, два раза в неделю, BJJ тренирую отдельно"
    llm.steps = [Call("new_plan", {"guidance": guidance}), say("Составляю."), _plan(), _plan()]
    await _send(dispatcher, bot, "ОФП, борьбу тренирую отдельно")
    assert {"role": "assistant", "text": clarify} in llm.prompts()[1]["history"]
    generated = next(p for p in llm.prompts() if "programming_brief" in p)
    assert generated["user_request"] == guidance
    assert generated["programming_brief"]["activity"] == {"name": "grappling", "mode": "support"}
    assert generated["programming_brief"]["sessions_per_week"] == 2
    assert generated["context"]["focus"] is None
    [conversation] = await _open_conversations(db, seeded.user_id)
    assert conversation.plan_id is None and conversation.draft_decision_id is not None
