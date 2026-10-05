"""The free-text assistant (ADR 0003) end to end through the real dispatcher and DB: an
unprompted message → `assistant` agent (a scripted `FunctionModel`, including tool calls) →
edits applied immediately through the website editor's guards, shown as a diff with Undo.

Covers the AGENTS.md §2 invariants on this new path: the progression cap and the
historical-max ceiling refuse (no confirmation checkbox in chat); a stop word halts before any
model call; an open health hold refuses before any model call; an implausible logged load is
refused; and the LLM only ever sees pseudonymized data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
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
from fitme.bot.callback_data import AssistantUndo
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
from fitme.db.selectors.decisions import get_decision, list_decisions_for_user
from fitme.db.selectors.plans import list_plan_versions
from fitme.db.selectors.training import list_set_logs_for_session
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.assistant import (
    AssistantAction,
    AssistantEdits,
    AssistantReply,
    FixLoggedSet,
    SetPrescription,
    SwapExercise,
)
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, RefusalCode
from fitme.domain.models import Block, Load, Plan, Prescription, Refusal, ScheduledDay, Workout
from fitme.domain.results import ParsedResults, SetResult
from fitme.i18n import t
from fitme.llm.agents import assistant_agent, plan_generate_agent, result_parse_agent
from fitme.services import training
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1


@dataclass(frozen=True)
class ToolStep:
    name: str
    args: dict[str, Any]


Step = ToolStep | AssistantEdits | AssistantAction | AssistantReply | Refusal | Plan | ParsedResults


@dataclass
class ScriptedLlm:
    """Each model request pops one step: a read-only tool call, or the final output."""

    steps: list[Step] = field(default_factory=list)
    requests: list[ModelRequest] = field(default_factory=list)
    calls: int = 0

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, ToolStep):
            return ModelResponse(parts=[ToolCallPart(tool_name=step.name, args=step.args)])
        names = [tool.name for tool in info.output_tools]
        # A union output has one tool per member; a single output type has just one.
        wanted = f"final_result_{type(step).__name__}" if len(names) > 1 else names[0]
        assert wanted in names
        return ModelResponse(
            parts=[ToolCallPart(tool_name=wanted, args=step.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings) -> LlmRuntime:
        return LlmRuntime(
            settings=settings,
            prices={},
            agent_factories={
                "assistant": lambda model: assistant_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
                "plan_generate": lambda model: plan_generate_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
                "result_parse": lambda model: result_parse_agent(
                    FunctionModel(self._respond, model_name=str(model))
                ),
            },
        )

    def first_prompt(self) -> str:
        for part in self.requests[0].parts:
            if part.part_kind == "user-prompt":
                return str(part.content)
        raise AssertionError("no user prompt")

    def tool_returns(self) -> list[ToolReturnPart]:
        return [p for r in self.requests for p in r.parts if isinstance(p, ToolReturnPart)]


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: ScriptedLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


def _plan() -> Plan:
    def item(exercise_id: str, load: Load) -> Prescription:
        return Prescription(
            exercise_id=exercise_id, sets=3, reps_min=5, reps_max=5, load=load, rest_seconds=120
        )

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
                        kind="single", items=[item("barbell_back_squat", Load(kind="kg", kg=60))]
                    ),
                    Block(kind="single", items=[item("pushup", Load(kind="bodyweight"))]),
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
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="in_progress"
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


def _last_text(session: FakeSession) -> str:
    return _messages(session)[-1].text


def _undo_id(message: SendMessage) -> int:
    markup = message.reply_markup
    assert markup is not None and hasattr(markup, "inline_keyboard")
    data = markup.inline_keyboard[0][0].callback_data
    assert data is not None
    return AssistantUndo.unpack(data).decision_id


async def _lang(db: Database, user_id: int) -> str:
    from fitme.services import profile as profile_service

    return (await profile_service.get_snapshot(db, user_id)).language


async def _bodies(db: Database, plan_id: int) -> list[dict[str, object]]:
    async with db.read() as conn:
        return [v.body for v in await list_plan_versions(conn, plan_id)]


def _squat_kg(body: dict[str, object]) -> float:
    plan = Plan.model_validate(body)
    return plan.workouts[0].blocks[0].items[0].load.kg or 0.0


def _set_squat(plan_id: int, kg: float) -> SetPrescription:
    return SetPrescription(
        op="set_prescription",
        plan_id=plan_id,
        workout_key="A",
        exercise_id="barbell_back_squat",
        load=Load(kind="kg", kg=kg),
    )


# --- Tests -----------------------------------------------------------------------------------


async def test_an_edit_is_applied_immediately_shown_as_a_diff_and_can_be_undone(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [
        ToolStep("get_plan", {"plan_id": seeded.plan_id}),
        AssistantEdits(ops=[_set_squat(seeded.plan_id, 62.5)]),
    ]
    await _send(dispatcher, bot, "squat 62.5 from now on")

    # The read-only tool answered with the plan.
    returns = llm.tool_returns()
    assert len(returns) == 1 and "barbell_back_squat" in json.dumps(returns[0].content)
    # Saved as version 2 through save_edit, logged with the assistant's prompt and model.
    bodies = await _bodies(db, seeded.plan_id)
    assert [_squat_kg(b) for b in bodies] == [60.0, 62.5]
    reply = _messages(session)[-1]
    assert t("assistant.saved_title", lang, name="Home plan") in reply.text
    decision_id = _undo_id(reply)
    async with db.read() as conn:
        decision = await get_decision(conn, decision_id)
    assert decision is not None and decision.kind == "user_edit"
    assert decision.prompt_template == "assistant" and decision.model
    assert decision.user_report is not None and decision.user_report["source"] == "assistant"

    await _click(dispatcher, bot, AssistantUndo(decision_id=decision_id).pack())
    bodies = await _bodies(db, seeded.plan_id)
    assert [_squat_kg(b) for b in bodies] == [60.0, 62.5, 60.0]
    assert _last_text(session) == t("assistant.undo_done", lang)

    # A second tap is stale: version 3 is not the edit's version any more.
    await _click(dispatcher, bot, AssistantUndo(decision_id=decision_id).pack())
    toasts = [m.text for m in session.sent if isinstance(m, AnswerCallbackQuery)]
    assert toasts[-1] == t("assistant.undo_stale", lang)
    assert len(await _bodies(db, seeded.plan_id)) == 3


async def test_an_increase_above_the_cap_and_ceiling_is_refused_not_saved(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """AGENTS.md §2 progression cap / ceiling: no checkbox in chat, so nothing is saved."""
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [AssistantEdits(ops=[_set_squat(seeded.plan_id, 80)])]
    await _send(dispatcher, bot, "squat 80 kg")
    assert len(await _bodies(db, seeded.plan_id)) == 1
    assert _last_text(session).startswith(t("assistant.over_cap", lang))


async def test_a_disallowed_exercise_is_refused(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    swap = SwapExercise(
        op="swap_exercise",
        plan_id=seeded.plan_id,
        workout_key="A",
        exercise_id="barbell_back_squat",
        new_exercise_id="cable_row_made_up",
    )
    llm.steps = [AssistantEdits(ops=[swap])]
    await _send(dispatcher, bot, "swap squats for cable rows")
    assert len(await _bodies(db, seeded.plan_id)) == 1
    assert _last_text(session).startswith(t("assistant.op_exercise_not_allowed", lang))


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
    llm.steps = [ToolStep("list_plans", {}), AssistantReply(message="You have one plan.")]
    await _send(dispatcher, bot, "how many plans do I have? mail me at a@b.example")
    prompt = llm.first_prompt()
    payload = json.loads(prompt)
    assert payload["context"]["user_id"] == seeded.user_id
    assert "a@b.example" not in prompt  # scrubbed
    assert set(payload) == {"context", "user_request", "state"}
    assert "telegram" not in prompt.lower() and "chat_id" not in prompt
    for part in llm.tool_returns():
        assert "telegram" not in json.dumps(part.content)
    lang = await _lang(db, seeded.user_id)
    assert _last_text(session) == f"You have one plan.\n\n{t('assistant.reply_footer', lang)}"


async def test_a_logged_set_can_be_corrected_and_undone_but_not_to_an_implausible_load(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)

    def fix(reps: int, kg: float | None) -> FixLoggedSet:
        return FixLoggedSet(
            op="fix_logged_set",
            session_id=seeded.session_id,
            exercise_id="barbell_back_squat",
            set_number=3,
            reps=reps,
            load_kg=kg,
        )

    async def third_set() -> tuple[int | None, float | None]:
        async with db.read() as conn:
            rows = await list_set_logs_for_session(conn, seeded.session_id)
        return rows[2].actual_reps, rows[2].actual_load_kg

    llm.steps = [AssistantEdits(ops=[fix(5, 600.0)])]
    await _send(dispatcher, bot, "last set was 600")
    assert await third_set() == (5, 60.0)
    assert _last_text(session).startswith(t("assistant.log_implausible", lang))

    llm.steps = [AssistantEdits(ops=[fix(3, None)])]  # reps only: the kg stays
    await _send(dispatcher, bot, "my last squat set was only 3 reps")
    assert await third_set() == (3, 60.0)
    reply = _messages(session)[-1]
    assert reply.text.startswith(t("assistant.log_fixed_title", lang))

    await _click(dispatcher, bot, AssistantUndo(decision_id=_undo_id(reply)).pack())
    assert await third_set() == (5, 60.0)


async def test_an_action_opens_the_existing_flow(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [AssistantAction(action="show_plan", plan_id=seeded.plan_id)]
    await _send(dispatcher, bot, "show my plan")
    assert "Home plan" in _messages(session)[-1].text
    assert t("disclosure.ai", lang) in _messages(session)[-1].text


async def test_a_refusal_is_logged_shows_the_model_reason_and_changes_nothing(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    llm.steps = [Refusal(code=RefusalCode.OUT_OF_SCOPE, message="Nutrition is out of scope.")]
    await _send(dispatcher, bot, "what should I eat?")
    assert _last_text(session) == "Nutrition is out of scope."
    assert len(await _bodies(db, seeded.plan_id)) == 1
    # Logged as a refusal decision with the prompt and model that chose it.
    async with db.read() as conn:
        refusals = [
            d for d in await list_decisions_for_user(conn, seeded.user_id) if d.kind == "refusal"
        ]
    assert len(refusals) == 1
    assert refusals[0].prompt_template == "assistant" and refusals[0].model
    assert refusals[0].user_report == {"source": "assistant"}
    # A safety refusal code keeps its fixed copy even with model text attached.
    llm.steps = [Refusal(code=RefusalCode.NEEDS_CLEARANCE, message="free-form model text")]
    await _send(dispatcher, bot, "anything")
    assert _last_text(session) == t("refusal.needs_clearance", lang)


async def test_disabled_assistant_falls_back_to_the_menu_hint(
    db: Database, settings: Settings, bot: Bot, session: FakeSession, llm: ScriptedLlm
) -> None:
    off = settings.model_copy(update={"assistant_enabled": False})
    dispatcher = build_dispatcher(db, off, llm.runtime(off))
    seeded = await _ready(dispatcher, bot, db)
    await _send(dispatcher, bot, "squat 62.5")
    assert llm.calls == 0
    assert _last_text(session) == t("unknown.free_text_hint", await _lang(db, seeded.user_id))


async def test_a_typed_new_plan_asks_for_guidance_or_uses_the_words_given(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)

    # No description: same as tapping New plan with plans present — ask first.
    llm.steps = [AssistantAction(action="new_plan")]
    await _send(dispatcher, bot, "new plan please")
    assert _last_text(session) == t("plan.new_guidance_prompt", lang, names="“Home plan”")
    assert llm.calls == 1

    # The answer is the guidance for the generator.
    llm.steps = [_plan()]
    await _send(dispatcher, bot, "upper body focus")
    payload = json.loads(_user_prompt(llm.requests[-1]))
    assert payload["user_request"] == "upper body focus"

    # A description in the same message is used directly.
    llm.steps = [AssistantAction(action="new_plan", request="a gym version"), _plan()]
    await _send(dispatcher, bot, "make me a new gym version of my plan")
    payload = json.loads(_user_prompt(llm.requests[-1]))
    assert payload["user_request"] == "a gym version"
    assert _last_text(session).startswith(t("plan.draft_title", lang))


def _user_prompt(request: ModelRequest) -> str:
    return next(str(p.content) for p in request.parts if p.part_kind == "user-prompt")


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


async def test_during_a_workout_as_planned_logs_the_current_block(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    settings: Settings,
    session: FakeSession,
    llm: ScriptedLlm,
) -> None:
    """The screenshot case: "по плану" under an open block logs it (✅), it doesn't re-open
    /train with "the workout is already in progress"."""
    seeded = await _ready(dispatcher, bot, db)
    lang = await _lang(db, seeded.user_id)
    session_id = await _start_workout(db, settings, seeded)

    llm.steps = [AssistantAction(action="log_block_as_planned")]
    await _send(dispatcher, bot, "по плану")

    state = json.loads(llm.first_prompt())["state"]
    assert state["current_block"]["block"] == 1
    assert state["current_block"]["exercises"][0]["exercise_id"] == "barbell_back_squat"
    logged = await _logged(db, session_id)
    assert logged[:3] == [("barbell_back_squat", 5, False)] * 3
    assert all(reps is None for _id, reps, _skipped in logged[3:])  # next block: not yet
    texts = [m.text for m in _messages(session)]
    assert t("train.block_logged", lang) in texts
    assert "Push-up" in _last_text(session)  # the next block is shown


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
    llm.steps = [AssistantAction(action="log_block_results"), parsed]
    await _send(dispatcher, bot, "сделал 4, 4, 4 по 60")

    # The result parser got the user's own words, not a paraphrase.
    parse_prompt = json.loads(
        next(str(p.content) for p in llm.requests[-1].parts if p.part_kind == "user-prompt")
    )
    assert parse_prompt["result_text"] == "сделал 4, 4, 4 по 60"
    # Shown for Correct / Fix, nothing written yet (only a confirmed parse writes set_logs).
    datas = [
        b.callback_data
        for row in _messages(session)[-1].reply_markup.inline_keyboard  # type: ignore[union-attr]
        for b in row
    ]
    assert any(d and d.startswith("tr:parse_ok:") for d in datas)


async def test_during_a_workout_skip_skips_the_current_block(
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

    llm.steps = [AssistantAction(action="skip_block")]
    await _send(dispatcher, bot, "приседания пропускаю")
    assert (await _logged(db, session_id))[:3] == [("barbell_back_squat", None, True)] * 3
    assert t("train.block_skipped", lang) in [m.text for m in _messages(session)]


async def test_block_actions_without_an_open_block_say_so(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [AssistantAction(action="log_block_as_planned")]
    await _send(dispatcher, bot, "done")
    assert "current_block" not in json.loads(llm.first_prompt())["state"]
    assert _last_text(session) == t("assistant.no_open_block", await _lang(db, seeded.user_id))
