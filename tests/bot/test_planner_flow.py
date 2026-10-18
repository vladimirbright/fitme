"""The conversational planner (experiment: llm-first planning) end to end through the real
dispatcher and a real migrated DB. The model is a scripted `FunctionModel` (the
`ScriptedLlm` of `test_assistant_flow.py`, here running the real `planner` agent) whose
steps stand in for what the model would say and which tools it would call.

Covers: a proposal in plain words, then "looks good" stages the whole plan (`stage_plan`),
shown whole with Confirm / Change / Cancel and saved unchanged with its own location; it
trains (an outdoor run for a gym profile, a BJJ class as an activity item); the draft and
the earlier turns reach the next message; loads above the guards are limited; a swap starts
from its own load; "save it" saves in one turn; tool errors let the model correct itself;
one plan per session; a started workout stays with the legacy assistant; the planner sees
pseudonymized data only; the Change / Change something / New plan buttons feed the planner.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from pydantic_ai.models.function import FunctionModel
from test_assistant_flow import (
    Call,
    ScriptedLlm,
    _click,
    _datas,
    _last_text,
    _messages,
    _ready,
    _send,
    _start_workout,
    _texts,
    say,
)

from conftest import FakeSession, make_user, message_update
from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import PlanDraft, PlanMenu, TrainAction, TrainPick
from fitme.catalog import load_catalog
from fitme.cli.commands import sync_search_index
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.records import DecisionRecord
from fitme.db.selectors.conversations import list_open_conversations
from fitme.db.selectors.decisions import list_decisions_for_user, list_llm_calls_since
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import list_set_logs_for_session
from fitme.db.selectors.users import get_telegram_account_by_telegram_user_id
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, Location, Measure
from fitme.domain.models import Block, Load, Plan, Prescription, ScheduledDay, Workout
from fitme.domain.results import Recap
from fitme.i18n import t
from fitme.llm.agents import assistant_agent, plan_generate_agent, planner_agent, recap_agent
from fitme.services import conversations, exclusions, planning
from fitme.services import profile as profile_service
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime

OWNER_CHAT_ID = 1
_SATURDAY_NOON_UTC = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_MONDAY_NOON_UTC = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_BJJ = "activity:bjj_class"


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm(planner_factory=planner_agent)


def _runtime(llm: ScriptedLlm, settings: Settings) -> LlmRuntime:
    """`ScriptedLlm.runtime` plus `recap`, so a workout trained here can finish."""

    def wrap(factory: Any) -> Any:
        return lambda model: factory(FunctionModel(llm._respond, model_name=str(model)))

    return LlmRuntime(
        settings=settings,
        prices={},
        agent_factories={
            "assistant": wrap(assistant_agent),
            "planner": wrap(planner_agent),
            "plan_generate": wrap(plan_generate_agent),
            "recap": wrap(recap_agent),
        },
    )


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: ScriptedLlm) -> Dispatcher:
    return build_dispatcher(db, settings, _runtime(llm, settings))


async def _fresh(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    *,
    location: str = "public_gym",
    equipment: tuple[str, ...] = (),
    injured: str | None = None,
) -> int:
    """An activated owner with a finished profile and screening, and no plans yet."""
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
            location=location,
            equipment=list(equipment),
            sessions_per_week=2,
            session_minutes=60,
            focus="general_fitness",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS | AREA_FLAGS:
            value = "yes" if flag.value == injured else "no"
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value=value, clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )
    await profile_service.set_timezone(db, user_id, "UTC")
    await profile_service.clear_progress(db, user_id)
    return user_id


def _timed(
    exercise_id: str,
    seconds: int,
    *,
    note: str | None = None,
    name: str | None = None,
    sets: int = 1,
) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        activity_name=name,
        sets=sets,
        measure=Measure.TIME,
        seconds=seconds,
        load=Load(kind="bodyweight"),
        rest_seconds=60,
        note=note,
    )


def _reps(exercise_id: str, load: Load, *, sets: int = 3) -> Prescription:
    return Prescription(
        exercise_id=exercise_id, sets=sets, reps_min=8, reps_max=10, load=load, rest_seconds=90
    )


def _single(item: Prescription) -> Block:
    return Block(kind="single", items=[item])


def _saturday_run() -> Plan:
    return Plan(
        name="Saturday run",
        location=Location.OUTDOOR,
        schedule=[ScheduledDay(weekday=5, workout_key="A")],
        workouts=[
            Workout(
                key="A",
                title="Easy run",
                blocks=[
                    _single(_timed("brisk_walk", 600)),
                    _single(_timed("easy_jog", 1500, note="conversational pace")),
                    _single(_timed("brisk_walk", 300)),
                ],
            )
        ],
    )


def _stage(plan: Plan, plan_id: int | None = None) -> Call:
    return Call("stage_plan", {"plan": plan.model_dump(mode="json"), "plan_id": plan_id})


async def _decisions(db: Database, user_id: int, kind: str) -> list[DecisionRecord]:
    async with db.read() as conn:
        return [d for d in await list_decisions_for_user(conn, user_id) if d.kind == kind]


async def _planner_drafts(db: Database, user_id: int) -> list[DecisionRecord]:
    return [
        d
        for d in await _decisions(db, user_id, "plan_revise")
        if (d.user_report or {}).get("base") == "planner_stage"
    ]


def _draft_message(session: FakeSession) -> SendMessage:
    """The newest message that carries a draft's Confirm button."""
    for message in reversed(_messages(session)):
        if any(data.startswith("pd:confirm:") for data in _datas(message)):
            return message
    raise AssertionError("no draft keyboard was sent")


def _draft_id(session: FakeSession) -> int:
    data = next(d for d in _datas(_draft_message(session)) if d.startswith("pd:confirm:"))
    return PlanDraft.unpack(data).decision_id


def _last_action(session: FakeSession, action: str) -> TrainAction:
    for message in reversed(_messages(session)):
        for data in _datas(message):
            if data.startswith(f"tr:{action}:"):
                return TrainAction.unpack(data)
    raise AssertionError(f"no {action!r} button was sent")


def _last_pick(session: FakeSession) -> TrainPick:
    for message in reversed(_messages(session)):
        for data in _datas(message):
            if data.startswith("tp:workout:"):
                return TrainPick.unpack(data)
    raise AssertionError("no workout pick button was sent")


async def _to_first_block(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> int:
    """`/train` → this workout → precheck No → Start. Returns the session id."""
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, _last_pick(session))
    no = _last_action(session, "precheck_no")
    await _click(dispatcher, bot, no)
    await _click(dispatcher, bot, _last_action(session, "start"))
    return no.session_id


async def _only_plan(db: Database, user_id: int) -> tuple[int, list[Plan]]:
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        assert len(plans) == 1
        versions = await list_plan_versions(conn, plans[0].id)
    return plans[0].id, [Plan.model_validate(v.body) for v in versions]


# --- A: talk, agree, stage, save, train ---------------------------------------------------------


async def test_an_easy_outdoor_run_is_proposed_staged_saved_and_trained(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock, "now", lambda: _SATURDAY_NOON_UTC)
    user_id = await _fresh(dispatcher, bot, db, location="public_gym")

    proposal = "Warm-up 10 min brisk walk, 25 min easy jog, 5 min walk. Shall I save it?"
    llm.steps = [say(proposal)]
    await _send(dispatcher, bot, "easy outdoor running on Saturdays")
    assert proposal in _last_text(session)
    assert await _decisions(db, user_id, "plan_revise") == []

    staged = _saturday_run()
    llm.steps = [_stage(staged), say("The Saturday run plan, ready to save.")]
    await _send(dispatcher, bot, "Looks good")

    assert llm.returns("stage_plan")[0]["ok"] is True
    # The second message saw the first exchange.
    history = llm.prompts()[-1]["history"]
    assert [turn["text"] for turn in history][-1] == proposal
    drafts = await _planner_drafts(db, user_id)
    assert len(drafts) == 1 and "plan_id" not in (drafts[0].user_report or {})
    assert drafts[0].prompt_template == "planner"
    draft = _draft_message(session)
    text = draft.text or ""
    assert text.startswith(t("plan.draft_title", "en"))
    assert "Brisk walk" in text and "Easy jog" in text and "conversational pace" in text
    assert "kg" not in text
    assert t("plan.location_line", "en", location=t("enum.location.outdoor", "en")) in text
    assert "Requested:" not in text  # no session-length warning for a planner draft
    datas = _datas(draft)
    draft_id = drafts[0].id
    for action in ("confirm", "change", "cancel"):
        assert PlanDraft(action=action, decision_id=draft_id).pack() in datas

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=draft_id))
    assert _last_text(session).startswith(t("plan.saved", "en", name="Saturday run", version=1))
    _, bodies = await _only_plan(db, user_id)
    assert bodies == [staged]
    assert bodies[0].location == Location.OUTDOOR
    assert await conversations.current_planning(db, user_id) is None

    session_id = await _to_first_block(dispatcher, bot, session)
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=3))
    await _click(dispatcher, bot, _last_action(session, "done"))
    block = _last_text(session)
    assert block.startswith(t("train.block_title", "en", index=2, total=3))
    assert "Easy jog" in block and "25 min" in block
    await _click(dispatcher, bot, _last_action(session, "done"))
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    jog = [row for row in rows if row.exercise_id == "easy_jog"]
    assert len(jog) == 1
    assert jog[0].actual_seconds == 1500 and jog[0].actual_load_kg is None


# --- B/C: the draft and the conversation reach the next message --------------------------------


def _home_circuit(*, with_rope: bool) -> Plan:
    items = [_single(_timed("jump_rope_skipping", 60, sets=3))] if with_rope else []
    items.append(_single(_reps("dumbbell_row", Load(kind="calibration"))))
    items.append(_single(_timed("brisk_walk", 600)))
    return Plan(
        name="Home circuit",
        schedule=[ScheduledDay(weekday=2, workout_key="A")],
        workouts=[Workout(key="A", title="Circuit", blocks=items)],
    )


async def test_a_change_sees_the_draft_and_the_conversation_and_replaces_the_draft(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    equipment = ("dumbbells", "bench", "jump_rope")
    user_id = await _fresh(dispatcher, bot, db, location="home_equipment", equipment=equipment)
    llm.steps = [say("Jump rope, rows, a walk. Save?")]
    await _send(dispatcher, bot, "a short home circuit on Wednesdays")
    llm.steps = [_stage(_home_circuit(with_rope=True)), say("Staged.")]
    await _send(dispatcher, bot, "yes")
    first_id = _draft_id(session)

    llm.steps = [_stage(_home_circuit(with_rope=False)), say("No rope now.")]
    await _send(dispatcher, bot, "Remove the jump rope")

    prompt = llm.prompts()[-1]
    draft = prompt["state"]["planning_session"]["draft"]
    ids = [i["exercise_id"] for w in draft["workouts"] for b in w["blocks"] for i in b["items"]]
    assert "jump_rope_skipping" in ids and "dumbbell_row" in ids
    texts = [turn["text"] for turn in prompt["history"]]
    assert "a short home circuit on Wednesdays" in texts and "yes" in texts
    assert "Jump rope, rows, a walk. Save?" in texts

    second_id = _draft_id(session)
    assert second_id != first_id
    assert await planning.current_draft_id(db, user_id) == second_id
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=first_id))
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=second_id))
    _, bodies = await _only_plan(db, user_id)
    # The judged plan is saved: the calibration row gets its default reps in reserve.
    saved = [i.exercise_id for b in bodies[0].workouts[0].blocks for i in b.items]
    assert saved == ["dumbbell_row", "brisk_walk"]
    assert bodies[0].workouts[0].blocks[0].items[0].rir == 3


# --- E: one question, then an activity plan -----------------------------------------------------


def _bjj_plan() -> Plan:
    def class_day(key: str) -> Workout:
        bjj = _timed(_BJJ, 5400, name="BJJ class", note="Class at the club")
        return Workout(key=key, title="BJJ", blocks=[_single(bjj)])

    return Plan(
        name="BJJ",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=3, workout_key="B"),
        ],
        workouts=[class_day("A"), class_day("B")],
    )


async def test_bjj_twice_a_week_asks_once_then_stages_the_classes_and_trains_them(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clock, "now", lambda: _MONDAY_NOON_UTC)
    user_id = await _fresh(dispatcher, bot, db)
    question = "Put the two classes in the plan, or build sessions around them?"
    llm.steps = [say(question)]
    await _send(dispatcher, bot, "BJJ twice per week")
    assert question in _last_text(session)
    assert llm.returns("stage_plan") == []
    assert await _decisions(db, user_id, "plan_revise") == []

    llm.steps = [_stage(_bjj_plan()), say("Two classes, Monday and Thursday.")]
    await _send(dispatcher, bot, "just put the two classes in")
    assert question in [turn["text"] for turn in llm.prompts()[-1]["history"]]
    text = _draft_message(session).text or ""
    assert "BJJ class" in text and "activity:" not in text
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=_draft_id(session)))
    _, bodies = await _only_plan(db, user_id)
    assert bodies == [_bjj_plan()]

    session_id = await _to_first_block(dispatcher, bot, session)
    block = _last_text(session)
    assert "BJJ class" in block and "activity:" not in block
    llm.steps = [Recap(text="")]
    await _click(dispatcher, bot, _last_action(session, "done"))
    async with db.read() as conn:
        rows = await list_set_logs_for_session(conn, session_id)
    assert [(r.exercise_id, r.actual_seconds, r.actual_load_kg) for r in rows] == [
        (_BJJ, 5400, None)
    ]


async def test_an_activity_item_with_an_injury_area_flagged_is_refused_to_the_model(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db, injured="knee_injury_current")
    llm.steps = [_stage(_bjj_plan()), say("The app can't include it while a knee is flagged.")]
    await _send(dispatcher, bot, "put my two BJJ classes in a plan")
    answer = llm.returns("stage_plan")[0]
    assert answer["ok"] is False and _BJJ in answer["error"]
    assert await _planner_drafts(db, user_id) == []


# --- F/G: loads ---------------------------------------------------------------------------------


def _strength_plan(*, squat_kg: float, bench: Prescription) -> Plan:
    def day(key: str, items: list[Prescription]) -> Workout:
        return Workout(key=key, title=f"Day {key}", blocks=[_single(i) for i in items])

    return Plan(
        name="Three days",
        schedule=[
            ScheduledDay(weekday=0, workout_key="A"),
            ScheduledDay(weekday=2, workout_key="B"),
            ScheduledDay(weekday=4, workout_key="C"),
        ],
        workouts=[
            day("A", [_reps("barbell_back_squat", Load(kind="kg", kg=squat_kg)), bench]),
            day("B", [_reps("dumbbell_row", Load(kind="calibration"))]),
            day("C", [_reps("pushup", Load(kind="bodyweight"))]),
        ],
    )


async def test_a_load_above_the_guards_is_limited_in_a_staged_plan(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """AGENTS.md §2: 80 kg against a logged 60 kg max becomes what the load engine allows,
    and both the model and the user are told."""
    seeded = await _ready(dispatcher, bot, db)
    bench = _reps("dumbbell_bench_press", Load(kind="calibration"))
    plan = _strength_plan(squat_kg=80, bench=bench)
    llm.steps = [_stage(plan, seeded.plan_id), say("Three days, staged.")]
    await _send(dispatcher, bot, "three days a week, squat 80")

    answer = llm.returns("stage_plan")[0]
    assert answer["ok"] is True and answer["notes"]
    drafts = await _planner_drafts(db, seeded.user_id)
    assert len(drafts) == 1 and drafts[0].proposal is not None
    drafted = Plan.model_validate(drafts[0].proposal["plan"])
    kg = drafted.workouts[0].blocks[0].items[0].load.kg
    assert kg is not None and kg <= 62.5
    assert "⚠️" in "\n".join(_texts(session))


async def test_a_barbell_to_dumbbell_swap_starts_from_its_own_load(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    swapped = _strength_plan(
        squat_kg=60, bench=_reps("dumbbell_bench_press", Load(kind="calibration"))
    )
    llm.steps = [_stage(swapped, seeded.plan_id), say("Dumbbell bench press instead.")]
    await _send(dispatcher, bot, "replace bench press with dumbbell bench press")
    drafts = await _planner_drafts(db, seeded.user_id)
    assert len(drafts) == 1 and drafts[0].proposal is not None
    bench = Plan.model_validate(drafts[0].proposal["plan"]).workouts[0].blocks[1].items[0]
    assert bench.exercise_id == "dumbbell_bench_press" and bench.load.kind == "calibration"
    assert (drafts[0].user_report or {}).get("plan_id") == seeded.plan_id


# --- H: save in the same turn -------------------------------------------------------------------


async def test_save_it_stages_and_saves_in_one_turn(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [say("Warm-up walk, easy jog, walk. Save?")]
    await _send(dispatcher, bot, "easy outdoor running on Saturdays")
    llm.steps = [_stage(_saturday_run()), Call("save_draft"), say("Saved.")]
    await _send(dispatcher, bot, "Looks good. Save it.")

    assert t("plan.saved", "en", name="Saturday run", version=1) in "\n".join(_texts(session))
    _, bodies = await _only_plan(db, user_id)
    assert bodies == [_saturday_run()]
    assert await conversations.current_planning(db, user_id) is None
    # The plan shown in that turn carries no Confirm button: it is already saved.
    assert not any(d.startswith("pd:confirm:") for m in _messages(session) for d in _datas(m))


# --- Tool errors and session rules --------------------------------------------------------------


async def test_a_bare_unknown_id_is_returned_to_the_model_which_corrects_it(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    wrong = _bjj_plan()
    for workout in wrong.workouts:
        item = workout.blocks[0].items[0]
        workout.blocks[0].items[0] = item.model_copy(
            update={"exercise_id": "bjj_class", "activity_name": None}
        )
    llm.steps = [_stage(wrong), _stage(_bjj_plan()), say("Staged.")]
    await _send(dispatcher, bot, "put my two BJJ classes in a plan")
    answers = llm.returns("stage_plan")
    assert answers[0]["ok"] is False and "bjj_class" in answers[0]["error"]
    assert answers[1]["ok"] is True
    drafts = await _planner_drafts(db, user_id)
    assert len(drafts) == 1 and drafts[0].proposal is not None
    assert Plan.model_validate(drafts[0].proposal["plan"]) == _bjj_plan()


async def test_staging_another_plan_while_a_draft_is_open_is_refused(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [_stage(_saturday_run()), say("A new plan's draft.")]
    await _send(dispatcher, bot, "a Saturday run plan, draft it")
    assert llm.returns("stage_plan")[0]["ok"] is True
    record = await conversations.current_planning(db, seeded.user_id)
    assert record is not None and record.plan_id is None

    llm.steps = [_stage(_saturday_run(), seeded.plan_id), say("Save or discard it first.")]
    await _send(dispatcher, bot, "and change my home plan too")
    answer = llm.returns("stage_plan")[-1]
    assert answer["ok"] is False and "save or close it first" in answer["error"]
    assert len(await _planner_drafts(db, seeded.user_id)) == 1


# --- Routing and data ---------------------------------------------------------------------------


async def _purposes(db: Database) -> list[str]:
    async with db.read() as conn:
        return [call.purpose for call in await list_llm_calls_since(conn, "1970-01-01")]


async def test_a_started_workout_keeps_the_assistant_and_anything_else_runs_the_planner(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    settings: Settings,
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [say("One plan.")]
    await _send(dispatcher, bot, "how many plans do I have?")
    assert await _purposes(db) == ["planner"]

    await _start_workout(db, settings, seeded)
    llm.steps = [say("Noted.")]
    await _send(dispatcher, bot, "felt fine")
    assert await _purposes(db) == ["planner", "assistant"]


async def test_the_planner_sees_only_pseudonymized_data(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [Call("list_plans"), say("One plan.")]
    await _send(dispatcher, bot, "how many plans? write to a@b.example")
    payload = llm.prompts()[0]
    prompt = json.dumps(payload, ensure_ascii=False)
    assert payload["context"]["user_id"] == seeded.user_id
    assert set(payload) == {"context", "user_request", "state", "history"}
    assert "a@b.example" not in prompt and "Owner" not in prompt
    assert "telegram" not in prompt.lower() and "chat_id" not in prompt
    assert "telegram" not in json.dumps(llm.returns("list_plans"))
    assert await _purposes(db) == ["planner"]


# --- Buttons --------------------------------------------------------------------------------------


async def test_change_on_a_staged_draft_sends_the_next_text_to_the_planner(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [_stage(_saturday_run()), say("Staged.")]
    await _send(dispatcher, bot, "a Saturday run plan")
    draft_id = _draft_id(session)

    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=draft_id))
    assert _last_text(session) == t("plan.change_prompt", "en")
    assert user_id not in dispatcher["pending_plan_revisions"]

    shorter = _saturday_run()
    shorter.workouts[0].blocks[1].items[0].seconds = 1200
    llm.steps = [_stage(shorter), say("20 minutes now.")]
    await _send(dispatcher, bot, "make the jog 20 minutes")
    prompt = llm.prompts()[-1]
    assert prompt["state"]["planning_session"]["draft"]["name"] == "Saturday run"
    assert t("plan.change_prompt", "en") in [turn["text"] for turn in prompt["history"]]
    assert _draft_id(session) != draft_id
    assert await _purposes(db) == ["planner", "planner"]


async def test_change_something_on_a_saved_plan_opens_its_session_for_the_planner(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    await _click(dispatcher, bot, PlanMenu(action="revise", plan_id=seeded.plan_id))
    assert _last_text(session) == t("plan.change_prompt", "en")
    assert seeded.user_id not in dispatcher["pending_plan_revisions"]
    record = await conversations.current_planning(db, seeded.user_id)
    assert record is not None and record.plan_id == seeded.plan_id

    llm.steps = [say("What should replace it?")]
    await _send(dispatcher, bot, "swap the push-ups")
    prompt = llm.prompts()[-1]
    assert prompt["state"]["planning_session"]["plan_id"] == seeded.plan_id
    assert await _purposes(db) == ["planner"]


async def test_new_plan_with_no_plans_asks_first(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    question = _messages(session)[-1]
    assert question.text == t("plan.new_first_prompt", "en")
    assert PlanMenu(action="new_skip", plan_id=0).pack() in _datas(question)
    assert llm.calls == 0
    record = await conversations.current_planning(db, user_id)
    assert record is not None and record.plan_id is None

    llm.steps = [say("Saturdays, outdoors? About 40 minutes. Shall I draft it?")]
    await _send(dispatcher, bot, "easy runs on Saturdays")
    assert t("plan.new_first_prompt", "en") in [
        turn["text"] for turn in llm.prompts()[-1]["history"]
    ]
    assert await _purposes(db) == ["planner"]


# --- Sessions and targeting (review fixes) ------------------------------------------------------


def _home_plan_with(*items: Prescription) -> Plan:
    return Plan(
        name="Home plan",
        schedule=[ScheduledDay(weekday=1, workout_key="A")],
        workouts=[Workout(key="A", title="Full body", blocks=[_single(i) for i in items])],
    )


async def _exclusion_rows(db: Database, user_id: int) -> list[tuple[str, str, int | None]]:
    return [
        (row.value, row.scope, row.plan_id) for row in await exclusions.list_for_user(db, user_id)
    ]


async def test_a_talk_about_a_saved_plan_keeps_its_exclusion_and_history_when_staged(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """A session-less question opens a new plan's conversation; the exclusion made in it
    and the conversation itself follow the plan staged later, instead of being dropped."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [say("It has squats, push-ups and a dumbbell press.")]
    await _send(dispatcher, bot, "what is in my home plan?")
    first = await conversations.current_planning(db, seeded.user_id)
    assert first is not None and first.plan_id is None

    exclude = {"kind": "name", "value": "lunge", "for_all_plans": False}
    llm.steps = [Call("add_exclusion", exclude), say("No lunges.")]
    await _send(dispatcher, bot, "no lunges in it")
    assert await _exclusion_rows(db, seeded.user_id) == [("lunge", "new_plan", None)]

    changed = _home_plan_with(_reps("pushup", Load(kind="bodyweight")))
    llm.steps = [_stage(changed, seeded.plan_id), say("Only push-ups now.")]
    await _send(dispatcher, bot, "just push-ups in the home plan")

    record = await conversations.current_planning(db, seeded.user_id)
    assert record is not None and record.id == first.id and record.plan_id == seeded.plan_id
    assert record.draft_decision_id == _draft_id(session)
    assert await _exclusion_rows(db, seeded.user_id) == [("lunge", "plan", seeded.plan_id)]
    texts = [turn.text for turn in await conversations.history(db, record.id)]
    assert "what is in my home plan?" in texts and "no lunges in it" in texts
    # The draft says which plan it replaces.
    title = t("plan.changes_draft_title", "en", name="Home plan")
    assert (_draft_message(session).text or "").startswith(title)


async def test_staging_and_saving_in_the_first_message_leaves_no_session_open(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [_stage(_saturday_run()), Call("save_draft"), say("Saved.")]
    await _send(dispatcher, bot, "an easy outdoor run on Saturdays, save it")
    _, bodies = await _only_plan(db, user_id)
    assert len(bodies) == 1
    async with db.read() as conn:
        assert await list_open_conversations(conn, user_id) == []


async def test_a_null_plan_id_is_a_new_plan_even_in_a_saved_plans_session(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    await _click(dispatcher, bot, PlanMenu(action="revise", plan_id=seeded.plan_id))
    llm.steps = [_stage(_saturday_run()), say("A separate Saturday run plan.")]
    await _send(dispatcher, bot, "actually, a separate Saturday run plan")
    assert (_draft_message(session).text or "").startswith(t("plan.draft_title", "en"))
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=_draft_id(session)))
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, seeded.user_id)
        home = await list_plan_versions(conn, seeded.plan_id)
    assert sorted(p.name for p in plans) == ["Home plan", "Saturday run"]
    assert len(home) == 1  # not version 2 of the home plan


async def test_staging_judges_with_the_target_plans_own_exclusions(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    catalog = load_catalog()
    # The default plan excludes push-ups for itself: a new plan may still have them.
    await exclusions.add(
        db,
        seeded.user_id,
        "exercise",
        "pushup",
        catalog,
        scope=exclusions.Scope.PLAN,
        plan_id=seeded.plan_id,
    )
    with_pushups = _home_plan_with(_reps("pushup", Load(kind="bodyweight")))
    llm.steps = [_stage(with_pushups.model_copy(update={"name": "New"})), say("Staged.")]
    await _send(dispatcher, bot, "a new plan with push-ups")
    assert llm.returns("stage_plan")[-1]["ok"] is True
    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=_draft_id(session)))

    # A second, non-default plan that excludes rows: staging rows into it is refused at
    # once, although the turn's snapshot is read for the default plan.
    async with db.read() as conn:
        other = next(
            p.id for p in await list_plans_for_user(conn, seeded.user_id) if p.name == "New"
        )
    await exclusions.add(
        db,
        seeded.user_id,
        "exercise",
        "dumbbell_row",
        catalog,
        scope=exclusions.Scope.PLAN,
        plan_id=other,
    )
    rows = _home_plan_with(_reps("dumbbell_row", Load(kind="calibration")))
    llm.steps = [_stage(rows, other), say("Rows are excluded there.")]
    await _send(dispatcher, bot, "add rows to the new plan")
    answer = llm.returns("stage_plan")[-1]
    assert answer["ok"] is False and "dumbbell_row" in answer["error"]


async def test_save_after_a_draft_that_fails_when_recorded_saves_nothing(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [_stage(_saturday_run()), say("Staged.")]
    await _send(dispatcher, bot, "an easy Saturday run plan")
    previous = _draft_id(session)

    async def blocked(*_args: Any, **_kwargs: Any) -> planning.EditDraftResult:
        return planning.EditDraftResult(errors=("blocked when recorded",))

    monkeypatch.setattr(planning, "record_edit_draft", blocked)
    longer = _saturday_run()
    longer.workouts[0].blocks[1].items[0].seconds = 1800
    llm.steps = [_stage(longer), Call("save_draft"), say("Saved.")]
    await _send(dispatcher, bot, "make the jog 30 minutes and save it")

    texts = "\n".join(_texts(session))
    assert t("assistant.blocked", "en") in texts and "blocked when recorded" in texts
    assert await _decisions(db, user_id, "plan_confirm") == []
    assert await planning.current_draft_id(db, user_id) == previous


async def test_stage_plan_answers_only_what_changed(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    await _fresh(dispatcher, bot, db)
    plan = _saturday_run()
    plan.workouts[0].blocks[1].items[0].rir = 2  # no reps in reserve on an easy jog
    llm.steps = [_stage(plan), say("Staged.")]
    await _send(dispatcher, bot, "an easy Saturday run plan")
    answer = llm.returns("stage_plan")[0]
    assert set(answer) == {"ok", "notes", "repaired", "saved", "hint"}
    assert answer["notes"] == []
    assert any(line.startswith("easy_jog: rir 2 dropped") for line in answer["repaired"])


async def test_search_gives_the_planner_exercise_facts_and_outdoor_exercises(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """A gym profile's planner finds outdoor-only exercises (the plan's location is what
    the guards judge), and each hit says how it is loaded and where it is done."""
    await _fresh(dispatcher, bot, db, location="public_gym")
    await sync_search_index(db)
    llm.steps = [
        Call("find_exercises", {"queries": ["interval run outdoors"]}),
        Call("find_substitutes", {"exercise_id": "barbell_bench_press", "avoid_areas": []}),
        say("Found."),
    ]
    await _send(dispatcher, bot, "what can I do for intervals outside?")
    answer = llm.returns("find_exercises")[0]
    (result,) = answer["results"]
    assert result["query"] == "interval run outdoors"
    assert "outdoor_interval_jog" in result["exercise_ids"]
    assert set(answer["exercises"]) == set(result["exercise_ids"])
    jog = answer["exercises"]["outdoor_interval_jog"]
    assert jog["locations"] == ["outdoor"]
    assert jog["kg_loadable"] is False and jog["equipment"] == []
    substitutes = llm.returns("find_substitutes")[0]
    assert substitutes
    assert {"kg_loadable", "load_unit", "equipment", "locations"} <= set(substitutes[0])


_STRENGTH_NAMES = [
    "barbell back squat",
    "bench press",
    "dumbbell row",
    "push-up",
    "romanian deadlift",
    "overhead press",
    "lat pulldown",
    "plank",
    "lunge",
    "pull-up",
]


async def test_one_bulk_lookup_is_enough_for_a_three_day_strength_plan(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """The 2026-10-18 finding: a strength plan looked up one exercise per model request and
    hit the request cap. One `find_exercises` call with every name gives the ids and the load
    facts for all of them, so the turn is lookup, stage, answer."""
    user_id = await _fresh(dispatcher, bot, db)
    await sync_search_index(db)
    plan = _strength_plan(
        squat_kg=20, bench=_reps("dumbbell_bench_press", Load(kind="calibration"))
    )
    plan.workouts[0].blocks[0].items[0].load = Load(kind="calibration")
    llm.steps = [
        Call("find_exercises", {"queries": _STRENGTH_NAMES}),
        _stage(plan),
        say("Three days, staged."),
    ]
    before = llm.calls
    await _send(dispatcher, bot, "three strength days a week")
    assert llm.calls - before == 3  # lookup, stage, answer

    (found,) = llm.returns("find_exercises")
    results = found["results"]
    assert [result["query"] for result in results] == _STRENGTH_NAMES
    for result in results:
        assert 0 < len(result["exercise_ids"]) <= 8, result["query"]
    ids = {exercise_id for result in results for exercise_id in result["exercise_ids"]}
    assert set(found["exercises"]) == ids  # each hit's facts once
    for facts in found["exercises"].values():
        assert {"names", "kg_loadable", "load_unit", "equipment", "locations"} <= set(facts)
    staged = {item.exercise_id for w in plan.workouts for b in w.blocks for item in b.items}
    assert staged <= ids
    assert llm.returns("stage_plan")[0]["ok"] is True
    assert len(await _planner_drafts(db, user_id)) == 1


async def test_a_single_or_empty_lookup_keeps_twenty_hits_and_too_many_is_an_error(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    await _fresh(dispatcher, bot, db)
    await sync_search_index(db)
    llm.steps = [
        Call("find_exercises", {"queries": []}),
        Call("find_exercises", {"queries": ["back"]}),
        Call("find_exercises", {"queries": ["squat"] * 26}),
        say("Found."),
    ]
    await _send(dispatcher, bot, "what is there?")
    listed, single, too_many = llm.returns("find_exercises")
    assert [r["query"] for r in listed["results"]] == [""]
    assert len(listed["results"][0]["exercise_ids"]) == 20
    assert len(single["results"][0]["exercise_ids"]) == 20
    assert too_many["ok"] is False and "split" in too_many["error"]


async def test_a_tool_call_is_logged_with_counts_and_not_the_query_text(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AGENTS.md §5: the tool-call log names the agent, the tool and counts or ids, never
    the free text the model passed."""
    await _fresh(dispatcher, bot, db)
    caplog.set_level(logging.INFO, logger="fitme.services.assistant")
    llm.steps = [
        Call("find_exercises", {"queries": ["приседания как у соседа Пети", "row"]}),
        Call("find_substitutes", {"exercise_id": "made up text", "avoid_areas": []}),
        say("Found."),
    ]
    await _send(dispatcher, bot, "squats please")
    lines = [r.getMessage() for r in caplog.records if r.name == "fitme.services.assistant"]
    assert "assistant tool call agent=planner tool=find_exercises args=queries=2" in lines
    assert (
        "assistant tool call agent=planner tool=find_substitutes "
        "args=exercise_id=not_in_catalog avoid_areas=0"
    ) in lines
    logged = "\n".join(lines)
    assert "Пети" not in logged and "соседа" not in logged and "made up" not in logged


async def test_the_planner_may_use_more_requests_than_the_assistant(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """A plan turn gets `PLANNER_REQUEST_LIMIT` (30) model requests: 16 serial lookups (over
    the assistant's 15) still end in an answer, not the "LLM unavailable" refusal."""
    await _fresh(dispatcher, bot, db)
    llm.steps = [Call("find_exercises", {"queries": [f"squat {n}"]}) for n in range(16)]
    llm.steps.append(say("Sixteen lookups later."))
    await _send(dispatcher, bot, "a leg day")
    assert _last_text(session).startswith("Sixteen lookups later.")
    assert len(llm.returns("find_exercises")) == 16


async def test_a_started_workout_keeps_the_assistants_cap_and_says_why_it_stopped(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The legacy assistant (a started workout) keeps `ASSISTANT_REQUEST_LIMIT` (15): 16
    serial lookups end in the "LLM unavailable" refusal, recorded and logged with cause
    `request_limit`, not `provider_error`."""
    seeded = await _ready(dispatcher, bot, db)
    await _start_workout(db, settings, seeded)
    caplog.set_level(logging.WARNING, logger="fitme.llm.usage")
    llm.steps = [Call("find_exercises", {"queries": [f"squat {n}"]}) for n in range(16)]
    llm.steps.append(say("Sixteen lookups later."))
    await _send(dispatcher, bot, "which squat next?")

    assert len(llm.returns("find_exercises")) == 14  # the 15th request is the last allowed
    assert t("refusal.llm_unavailable", "en") in "\n".join(_texts(session))
    (refusal,) = await _decisions(db, seeded.user_id, "refusal")
    assert refusal.proposal is not None and refusal.proposal["cause"] == "request_limit"
    (failure,) = [r for r in caplog.records if r.getMessage() == "llm call failed"]
    assert failure.cause == "request_limit" and failure.purpose == "assistant"


async def test_cancel_closes_an_open_planning_conversation(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [say("Saturdays? Shall I put this into a plan?")]
    await _send(dispatcher, bot, "a running plan")
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("plan.new_cancelled", "en")
    assert await conversations.current_planning(db, user_id) is None

    llm.steps = [_stage(_saturday_run()), say("Staged.")]
    await _send(dispatcher, bot, "a Saturday run plan")
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("plan.change_cancelled", "en")
    assert await conversations.current_planning(db, user_id) is None


async def test_change_on_a_draft_whose_session_idled_out_reopens_it_for_the_planner(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _fresh(dispatcher, bot, db)
    llm.steps = [_stage(_saturday_run()), say("Staged.")]
    await _send(dispatcher, bot, "a Saturday run plan")
    draft_id = _draft_id(session)

    later = clock.now() + timedelta(hours=conversations.PLANNING_IDLE_HOURS + 1)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert await conversations.current_planning(db, user_id) is None  # idled out
    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=draft_id))
    record = await conversations.current_planning(db, user_id)
    assert record is not None and record.draft_decision_id == draft_id

    llm.steps = [say("How long should the jog be?")]
    await _send(dispatcher, bot, "a longer jog")
    assert llm.prompts()[-1]["state"]["planning_session"]["draft"]["name"] == "Saturday run"


# --- Review fixes, round 2 -----------------------------------------------------------------------


async def test_a_talks_exclusion_judges_the_saved_plan_it_turns_out_to_be_about(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    """The exclusion made in a draftless new-plan talk moves to the saved plan the talk is
    about, so it judges the staged plan at once: stage, record and Save agree."""
    seeded = await _ready(dispatcher, bot, db)
    llm.steps = [say("Squats, push-ups and a dumbbell press.")]
    await _send(dispatcher, bot, "what is in my home plan?")
    exclude = {"kind": "name", "value": "lunge", "for_all_plans": False}
    llm.steps = [Call("add_exclusion", exclude), say("No lunges.")]
    await _send(dispatcher, bot, "no lunges in it")

    with_lunge = _home_plan_with(
        _reps("pushup", Load(kind="bodyweight")),
        _reps("barbell_forward_lunge", Load(kind="calibration")),
    )
    without = _home_plan_with(_reps("pushup", Load(kind="bodyweight")))
    llm.steps = [
        _stage(with_lunge, seeded.plan_id),
        _stage(without, seeded.plan_id),
        say("Lunges are excluded: push-ups only."),
    ]
    await _send(dispatcher, bot, "push-ups and lunges in the home plan")
    refused, accepted = llm.returns("stage_plan")
    assert refused["ok"] is False and "barbell_forward_lunge" in refused["error"]
    assert accepted["ok"] is True

    await _click(dispatcher, bot, PlanDraft(action="confirm", decision_id=_draft_id(session)))
    assert _last_text(session).startswith(t("plan.saved", "en", name="Home plan", version=2))
    assert await _exclusion_rows(db, seeded.user_id) == [("lunge", "plan", seeded.plan_id)]


async def test_cancel_while_the_new_plan_question_is_pending_closes_its_session(
    dispatcher: Dispatcher, bot: Bot, db: Database, session: FakeSession, llm: ScriptedLlm
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    await _click(dispatcher, bot, PlanMenu(action="new", plan_id=0))
    assert await conversations.current_planning(db, seeded.user_id) is not None
    await _send(dispatcher, bot, "/cancel")
    assert _last_text(session) == t("plan.new_cancelled", "en")
    assert await conversations.current_planning(db, seeded.user_id) is None


async def test_change_after_idle_moves_a_draftless_talk_and_keeps_its_exclusion(
    dispatcher: Dispatcher,
    bot: Bot,
    db: Database,
    session: FakeSession,
    llm: ScriptedLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _ready(dispatcher, bot, db)
    changed = _home_plan_with(_reps("pushup", Load(kind="bodyweight")))
    llm.steps = [_stage(changed, seeded.plan_id), say("Staged.")]
    await _send(dispatcher, bot, "only push-ups in the home plan")
    draft_id = _draft_id(session)

    later = clock.now() + timedelta(hours=conversations.PLANNING_IDLE_HOURS + 1)
    monkeypatch.setattr(clock, "now", lambda: later)
    llm.steps = [say("What should change?")]
    await _send(dispatcher, bot, "about my plans")  # opens a new, draftless talk
    exclude = {"kind": "name", "value": "lunge", "for_all_plans": False}
    llm.steps = [Call("add_exclusion", exclude), say("No lunges.")]
    await _send(dispatcher, bot, "no lunges")
    talk = await conversations.current_planning(db, seeded.user_id)
    assert talk is not None and talk.plan_id is None and talk.draft_decision_id is None

    await _click(dispatcher, bot, PlanDraft(action="change", decision_id=draft_id))
    record = await conversations.current_planning(db, seeded.user_id)
    assert record is not None and record.id == talk.id
    assert record.plan_id == seeded.plan_id and record.draft_decision_id == draft_id
    assert await _exclusion_rows(db, seeded.user_id) == [("lunge", "plan", seeded.plan_id)]
