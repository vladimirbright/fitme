"""The end-of-workout recap (A§6.5 step 6, IMPLEMENTATION_PLAN M8) end to end: the
deterministic summary and the engine's next-load preview, check-ins for flagged areas loaded
today (Pain halts; unanswered blocks the increase), the `progression` decision as a preview
(`load_changes = []`), the model's text (numbers never taken from it; wording-checked) and
Apply of structural suggestions through `validate_plan` into an `origin=progression` plan
version, plus the A§7 reference rule after a decrease. The LLM is a `FunctionModel`; Telegram
is the `FakeSession`."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from aiogram import Bot, Dispatcher
from conftest import FakeSession
from pydantic_ai.exceptions import UserError
from test_train_flow import (
    _BENCH,
    _PUSHUP,
    _SQUAT,
    _TUESDAY_NOON_UTC,
    FakeLlm,
    _activate,
    _adjusted_workout,
    _callback_datas,
    _click,
    _holds,
    _last_action,
    _last_text,
    _parsed,
    _ready,
    _rows,
    _seed_completed_session,
    _seed_plan,
    _seed_profile,
    _send,
    _sent,
    _session_status,
    _to_first_block,
    _to_review,
    _toasts,
    make_plan,
)

from fitme import clock
from fitme.bot.app import build_dispatcher
from fitme.bot.callback_data import CheckinReply, TrainAction, TrainPick
from fitme.catalog import load_catalog
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_screening_flag
from fitme.db.controllers.training import finish_workout_session
from fitme.db.selectors.decisions import (
    list_decision_outcomes,
    list_decisions_for_user,
    list_llm_calls_since,
)
from fitme.db.selectors.plans import list_plan_versions
from fitme.db.selectors.training import list_checkins_for_session, list_open_health_holds
from fitme.domain.models import Load, Plan
from fitme.domain.results import ChangeReps, Recap, SwapExercise
from fitme.i18n import t
from fitme.services import planning, recap, training
from fitme.services.training import Status

_EPOCH = "1970-01-01T00:00:00.000000Z"


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def dispatcher(db: Database, settings: Settings, llm: FakeLlm) -> Dispatcher:
    return build_dispatcher(db, settings, llm.runtime(settings))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC)


async def _finish_block(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> None:
    """✅ where it is offered; ⏭ on an all-calibration block (no weight to confirm)."""
    datas = _callback_datas(_sent(session)[-1])
    action = "done" if any(d.startswith("tr:done:") for d in datas) else "skip"
    await _click(dispatcher, bot, _last_action(session, action))


async def _complete_a(dispatcher: Dispatcher, bot: Bot, session: FakeSession) -> int:
    """Start workout A and finish both blocks (✅ every set at the top of the range)."""
    session_id = await _to_first_block(dispatcher, bot, session)
    await _finish_block(dispatcher, bot, session)
    await _finish_block(dispatcher, bot, session)
    return session_id


async def _flag(db: Database, user_id: int, flag: str) -> None:
    async with db.transaction() as conn:
        await upsert_screening_flag(conn, user_id=user_id, flag=flag, value="yes", clearance=None)


async def _progression(
    db: Database, user_id: int, session_id: int
) -> tuple[int, recap.RecapProposal]:
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    decision = next(
        d
        for d in decisions
        if d.kind == "progression"
        and d.user_report is not None
        and d.user_report.get("session_id") == session_id
    )
    proposal = recap.recap_of_decision(decision)
    assert proposal is not None
    return decision.id, proposal


def _recap_message(session: FakeSession) -> str:
    return next(
        m.text or ""
        for m in reversed(_sent(session))
        if (m.text or "").startswith(t("recap.title", "en", workout="A — Full body"))
    )


async def _engine_squat(db: Database, settings: Settings, user_id: int) -> tuple[str, str]:
    """The engine's next squat load right now: `(load text, reason)`."""
    catalog = load_catalog()
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    inputs = planning.build_inputs(catalog, snapshot, settings, user_id)
    exercise = catalog.by_id(_SQUAT)
    assert exercise is not None
    load, reason, _verdicts = planning.engine_load(exercise, inputs)
    return f"{load.kg:g}" if load.kg is not None else load.kind, reason


# --- Accept list ------------------------------------------------------------------------------


async def test_all_reps_hit_gives_plus_increment_in_the_next_session(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    # A steady-state session at 40 (reps between min and max): the review holds 40.
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=9)
    session_id = await _complete_a(dispatcher, bot, session)

    text = _recap_message(session)
    assert "Barbell back squat: 3/3 sets, 30 reps, 1200 kg total" in text
    assert "Push-up: 3/3 sets, 30 reps" in text
    assert t("recap.next_title", "en") in text
    assert (
        t("recap.next_increase", "en", name="Barbell back squat", load="42.5 kg", current="40 kg")
        in text
    )
    assert (
        t("recap.next_hold", "en", name="Push-up", load="bodyweight", current="bodyweight")
        not in text
    )
    assert "• Push-up: bodyweight" in text
    assert t("disclosure.ai", "en") in text
    decision_id, proposal = await _progression(db, user_id, session_id)
    squat = next(p for p in proposal.preview if p.exercise_id == _SQUAT)
    assert squat.kind == recap.NextKind.INCREASE and squat.next.kg == 42.5
    async with db.read() as conn:
        decisions = {d.id: d for d in await list_decisions_for_user(conn, user_id)}
    assert decisions[decision_id].load_changes == []  # a preview, applied at the next Start
    assert decisions[decision_id].llm_input is not None
    assert decisions[decision_id].llm_input == json.loads(llm.prompts[-1])
    assert "user_id" not in llm.prompts[-1] and "history" not in llm.prompts[-1]

    # The next session's review prescribes the increment (and its Start applies it).
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)


async def test_an_unanswered_checkin_blocks_the_increase_and_fine_unblocks_it(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, settings: Settings
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=9)
    session_id = await _to_first_block(dispatcher, bot, session)
    # Flagged during the workout (via /profile): knee is loaded by the squat, ankle by nothing.
    await _flag(db, user_id, "knee_injury_current")
    await _flag(db, user_id, "ankle_injury_current")
    await _click(dispatcher, bot, _last_action(session, "done"))
    await _click(dispatcher, bot, _last_action(session, "done"))

    async with db.read() as conn:
        checkins = await list_checkins_for_session(conn, session_id)
    assert [c.question_key for c in checkins] == ["area:knee"]  # only flagged AND loaded today
    assert checkins[0].answer == "unknown" and checkins[0].answered_at is None
    question = next(m for m in _sent(session) if m.text == t("checkin.question", "en", area="knee"))
    datas = _callback_datas(question)
    assert CheckinReply(checkin_id=checkins[0].id, answer="fine").pack() in datas
    assert CheckinReply(checkin_id=checkins[0].id, answer="pain").pack() in datas

    # Unanswered: the earned increase is held (silence is not consent), and the recap says
    # what it will be if the check-in is fine.
    _decision_id, proposal = await _progression(db, user_id, session_id)
    squat = next(p for p in proposal.preview if p.exercise_id == _SQUAT)
    assert squat.kind == recap.NextKind.HOLD_CHECKIN and squat.next.kg == 40.0
    assert squat.if_fine is not None and squat.if_fine.kg == 42.5
    assert "check-in" in squat.reason
    assert t(
        "recap.next_hold_checkin",
        "en",
        name="Barbell back squat",
        load="40 kg",
        current="40 kg",
        if_fine="42.5 kg",
    ) in _recap_message(session)
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    progression = next(d for d in decisions if d.kind == "progression")
    assert progression.user_report is not None
    assert progression.user_report["checkins"] == {"knee": "unknown"}
    assert (await _engine_squat(db, settings, user_id))[0] == "40"

    # An explicit Fine unblocks it; a second tap changes nothing.
    await _click(dispatcher, bot, CheckinReply(checkin_id=checkins[0].id, answer="fine"))
    assert _toasts(session)[-1] == t("recap.checkin_saved", "en")
    assert (await _engine_squat(db, settings, user_id))[0] == "42.5"
    await _click(dispatcher, bot, CheckinReply(checkin_id=checkins[0].id, answer="worse"))
    assert _toasts(session)[-1] == t("recap.checkin_already", "en")
    async with db.read() as conn:
        (checkin,) = await list_checkins_for_session(conn, session_id)
    assert checkin.answer == "fine"
    assert await _holds(db, user_id) == []


async def test_an_increase_already_applied_this_week_blocks_the_second(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})  # hit: +2.5
    session_id = await _complete_a(dispatcher, bot, session)  # trained at 42.5, all sets hit

    _decision_id, proposal = await _progression(db, user_id, session_id)
    squat = next(p for p in proposal.preview if p.exercise_id == _SQUAT)
    assert squat.kind == recap.NextKind.HOLD_BLOCKED and squat.next.kg == 42.5
    assert "weekly" in squat.reason or "cap" in squat.reason
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)


async def test_the_model_cannot_change_the_numbers_and_forbidden_wording_is_dropped(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=9)
    llm.recap_responses.append(
        Recap(text="Great session. Next time squat 100 kg, you are ready for it.", suggestions=[])
    )
    session_id = await _complete_a(dispatcher, bot, session)
    text = _recap_message(session)
    assert "Barbell back squat: 42.5 kg (up from 40 kg" in text
    assert "100 kg" not in text  # a figure the recap doesn't state: the text is dropped
    decision_id, proposal = await _progression(db, user_id, session_id)
    assert next(p for p in proposal.preview if p.exercise_id == _SQUAT).next.kg == 42.5
    assert proposal.recap_text is None
    async with db.read() as conn:
        decisions = {d.id: d for d in await list_decisions_for_user(conn, user_id)}
    assert any(
        g["rule"] == "recap.unknown_figure" and not g["ok"]
        for g in decisions[decision_id].guards_fired
    )
    await _to_review(dispatcher, bot, session)
    assert "@ 42.5 kg" in _last_text(session) and "100" not in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "abort"))

    # Text that only quotes the recap's own figures is shown.
    llm.recap_responses.append(Recap(text="Every set landed; 42.5 kg is next.", suggestions=[]))
    await _complete_a(dispatcher, bot, session)
    assert "Every set landed; 42.5 kg is next." in _recap_message(session)

    # Forbidden wording (AGENTS.md §3) drops the model text and logs the drop.
    llm.recap_responses.append(Recap(text="Your personal trainer says: well done.", suggestions=[]))
    session_id = await _complete_a(dispatcher, bot, session)
    text = _recap_message(session)
    assert "personal trainer" not in text
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    progression = next(d for d in decisions if d.kind == "progression")
    assert progression.proposal is not None and progression.proposal["recap_text"] is None
    assert any(
        g["rule"] == "wording.forbidden_term" and not g["ok"] for g in progression.guards_fired
    )


async def test_a_suggestion_that_cannot_pass_the_guards_is_dropped_at_recap_time(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _plan_id, _ = await _ready(dispatcher, bot, db)
    llm.recap_responses.append(
        Recap(
            text="",
            suggestions=[
                SwapExercise(from_exercise_id=_PUSHUP, to_exercise_id="unicorn_press"),
                SwapExercise(from_exercise_id=_PUSHUP, to_exercise_id="pike_pushup"),
            ],
        )
    )
    session_id = await _complete_a(dispatcher, bot, session)
    decision_id, proposal = await _progression(db, user_id, session_id)
    assert [c.to_exercise_id for c in proposal.suggestions if isinstance(c, SwapExercise)] == [
        "pike_pushup"
    ]
    applies = [
        TrainAction.unpack(d)
        for m in _sent(session)
        for d in _callback_datas(m)
        if d.startswith("tr:apply:")
    ]
    assert [(a.item, a.decision_id) for a in applies] == [(0, decision_id)]
    async with db.read() as conn:
        decisions = {d.id: d for d in await list_decisions_for_user(conn, user_id)}
    dropped = [
        g for g in decisions[decision_id].guards_fired if g["rule"] == "recap.suggestion_dropped"
    ]
    assert len(dropped) == 1 and "unicorn_press" in dropped[0]["detail"]


async def test_apply_that_trips_a_guard_is_refused_and_logged(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, plan_id, _ = await _ready(dispatcher, bot, db)
    llm.recap_responses.append(
        Recap(text="", suggestions=[ChangeReps(exercise_id=_SQUAT, reps_min=5, reps_max=8)])
    )
    session_id = await _complete_a(dispatcher, bot, session)
    apply = _last_action(session, "apply")
    assert apply.session_id == session_id and apply.item == 0
    # Flagged after the recap: the squat is now contraindicated, so the plan can't be saved.
    await _flag(db, user_id, "knee_injury_current")

    await _click(dispatcher, bot, apply)

    assert _toasts(session)[-1] == t("recap.apply_failed", "en")
    async with db.read() as conn:
        versions = await list_plan_versions(conn, plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert [v.version for v in versions] == [1]
    refusal = decisions[0]
    assert refusal.kind == "refusal"
    assert any(
        g["rule"] == "screening.exercise_allowed" and not g["ok"] for g in refusal.guards_fired
    )
    assert refusal.user_report is not None and refusal.user_report["applied_index"] == 0


# --- The rest ---------------------------------------------------------------------------------


async def test_post_decrease_rule_the_reference_is_the_last_completed_prescription(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A§7: 42.5 applied by a Start this week; then two completed sessions at 40 under the
    rep range → the engine decreases, and a request for 42.5 the same week is an increase
    over 40 that the weekly cap (2.5 already used) blocks — the applied 42.5 no longer lifts
    the reference once a later session on the exercise is completed."""
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)  # applies 40 -> 42.5
    await _click(dispatcher, bot, TrainAction(action="abort", session_id=session_id))
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(hours=1))
    for _ in range(2):
        failed = await _seed_completed_session(
            db, user_id, version_id, loads={_SQUAT: 40.0}, reps=5
        )
        async with db.transaction() as conn:
            await finish_workout_session(conn, failed, status="completed")

    session_id = await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 8–10 @ 35 kg" in _last_text(session)  # the decrease

    llm.adjust_responses.append(_adjusted_workout(42.5))
    await _click(dispatcher, bot, _last_action(session, "adjust"))
    await _send(dispatcher, bot, "back to 42.5 please")
    assert "Barbell back squat: 3 × 8–10 @ 35 kg" in _last_text(session)
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    adjust = next(d for d in decisions if d.kind == "session_adjust")
    assert any(g["rule"] == "progression.weekly_cap" and not g["ok"] for g in adjust.guards_fired)
    assert any(g["rule"] == "loads.substituted" for g in adjust.guards_fired)


async def test_checkin_pain_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _to_first_block(dispatcher, bot, session)
    await _flag(db, user_id, "shoulder_injury_current")
    await _finish_block(dispatcher, bot, session)
    await _finish_block(dispatcher, bot, session)
    async with db.read() as conn:
        (checkin,) = await list_checkins_for_session(conn, session_id)
    assert checkin.question_key == "area:shoulder"

    await _click(dispatcher, bot, CheckinReply(checkin_id=checkin.id, answer="pain"))

    assert _last_text(session) == t("halt.message", "en")
    assert await _holds(db, user_id) == ["checkin_pain"]
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
        (hold,) = await list_open_health_holds(conn, user_id)
    halt_decision = next(d for d in decisions if d.kind == "session_halt")
    assert halt_decision.user_report is not None
    assert halt_decision.user_report["trigger"] == "checkin_pain"
    assert halt_decision.user_report["area"] == "shoulder"
    assert hold.source_session_id == session_id  # tied to the completed session
    assert halt_decision.user_report["session_id"] == session_id
    assert await _session_status(db, session_id) == "completed"  # not rewritten to halted
    await _send(dispatcher, bot, "/train")
    assert _last_text(session) == t("refusal.open_health_hold", "en")


async def test_recap_without_the_model_shows_the_deterministic_recap_alone(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=9)
    llm.recap_responses.append(UserError("provider down"))
    session_id = await _complete_a(dispatcher, bot, session)
    text = _recap_message(session)
    assert "Barbell back squat: 3/3 sets" in text
    assert "Barbell back squat: 42.5 kg (up from 40 kg" in text
    _decision_id, proposal = await _progression(db, user_id, session_id)
    assert proposal.refusal is not None and proposal.refusal.code == "llm_unavailable"
    assert proposal.recap_text is None and proposal.suggestions == []
    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, _EPOCH)
    assert [c.purpose for c in calls] == ["recap"] and not calls[0].ok
    assert not any(d.startswith("tr:apply:") for m in _sent(session) for d in _callback_datas(m))


async def test_apply_creates_an_origin_progression_version_and_is_idempotent(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, plan_id, _ = await _ready(dispatcher, bot, db)
    llm.recap_responses.append(
        Recap(text="", suggestions=[ChangeReps(exercise_id=_SQUAT, reps_min=5, reps_max=8)])
    )
    session_id = await _complete_a(dispatcher, bot, session)
    suggestion = next(m for m in _sent(session) if (m.text or "").startswith("Suggested change"))
    assert suggestion.text == t(
        "recap.suggestion_reps", "en", name="Barbell back squat", reps_min=5, reps_max=8
    )
    apply = _last_action(session, "apply")

    await _click(dispatcher, bot, apply)

    assert _last_text(session) == t("recap.applied", "en", name="Home plan", version=2)
    async with db.read() as conn:
        versions = await list_plan_versions(conn, plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert [(v.version, v.origin) for v in versions] == [(1, "llm"), (2, "progression")]
    saved = Plan.model_validate(versions[-1].body)
    squat = saved.workouts[0].blocks[0].items[0]
    assert (squat.reps_min, squat.reps_max) == (5, 8)
    assert saved.workouts[0].blocks[1].items[1].reps_max == 10  # others untouched
    confirm = next(d for d in decisions if d.kind == "plan_confirm")
    assert confirm.load_changes == []
    assert confirm.user_report is not None and confirm.user_report["applied_index"] == 0
    assert versions[-1].decision_id == confirm.id
    async with db.read() as conn:
        outcomes = await list_decision_outcomes(conn, apply.decision_id)
    assert outcomes[-1].outcome["version"] == 2

    await _click(dispatcher, bot, apply)
    assert _toasts(session)[-1] == t("recap.already_applied", "en")
    async with db.read() as conn:
        assert len(await list_plan_versions(conn, plan_id)) == 2
    # The next session trains the new rep range.
    await _to_review(dispatcher, bot, session)
    assert "Barbell back squat: 3 × 5–8" in _last_text(session)
    assert session_id != _last_action(session, "start").session_id


async def test_a_stale_apply_is_rejected(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, plan_id, _ = await _ready(dispatcher, bot, db)
    llm.recap_responses.append(
        Recap(text="", suggestions=[ChangeReps(exercise_id=_SQUAT, reps_min=5, reps_max=8)])
    )
    await _complete_a(dispatcher, bot, session)
    old_apply = _last_action(session, "apply")
    # A later session's recap supersedes the earlier suggestions.
    newer_session_id = await _complete_a(dispatcher, bot, session)

    await _click(dispatcher, bot, old_apply)
    assert _toasts(session)[-1] == t("recap.apply_stale", "en")
    # A forged decision id, or another user's, is not found either.
    await _click(
        dispatcher,
        bot,
        TrainAction(action="apply", session_id=old_apply.session_id, item=0, decision_id=999),
    )
    assert _toasts(session)[-1] == t("recap.apply_stale", "en")
    async with db.read() as conn:
        assert len(await list_plan_versions(conn, plan_id)) == 1
    settings = Settings(
        telegram_bot_token="x", db_path="x", web_base_url="https://f.example", secret_key="x" * 32
    )
    stale = await recap.apply_suggestion(
        db, settings, user_id, old_apply.session_id, old_apply.decision_id, 0
    )
    assert stale.status == Status.STALE
    newest_id, _proposal = await _progression(db, user_id, newer_session_id)
    missing = await recap.apply_suggestion(db, settings, user_id, newer_session_id, newest_id, 5)
    assert missing.status == Status.NOT_FOUND


async def test_decision_outcomes_carry_planned_vs_actual_per_set(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_completed_session(db, user_id, version_id, loads={_SQUAT: 40.0})
    session_id = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "skip"))
    await _click(dispatcher, bot, _last_action(session, "done"))

    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    start = next(
        d
        for d in decisions
        if d.kind == "session_adjust" and d.user_report and d.user_report["event"] == "start"
    )
    async with db.read() as conn:
        (outcome,) = await list_decision_outcomes(conn, start.id)
    assert outcome.outcome["session_id"] == session_id and outcome.outcome["completed"] is True
    sets = outcome.outcome["sets"]
    assert len(sets) == 9
    assert sets[0] == {
        "exercise_id": _SQUAT,
        "set_index": 1,
        "planned_load_kg": 42.5,
        "planned_reps_min": 8,
        "planned_reps_max": 10,
        "actual_load_kg": None,
        "actual_reps": None,
        "skipped": True,
    }
    assert sets[-1]["exercise_id"] == _PUSHUP and sets[-1]["actual_reps"] == 10
    text = _recap_message(session)
    assert "Barbell back squat: 0/3 sets, 0 reps (3 skipped)" in text
    assert "Dumbbell bench press: 3/3 sets, 30 reps" in text
    # A skipped session is never a success: the squat holds (no increase, no decrease).
    assert (
        t("recap.next_hold", "en", name="Barbell back squat", load="42.5 kg", current="42.5 kg")
        in text
    )


async def test_recap_is_idempotent_after_a_restart(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    settings: Settings,
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    session_id = await _complete_a(dispatcher, bot, session)
    assert llm.calls == 1
    view = await recap.build_recap(db, llm.runtime(settings), user_id, session_id)
    assert view is not None and llm.calls == 1  # rebuilt from the stored decision
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    assert sum(1 for d in decisions if d.kind == "progression") == 1
    assert view.proposal.preview[0].kind == recap.NextKind.CALIBRATION
    assert t(
        "recap.next_calibration", "en", name="Barbell back squat", load="", current=""
    ) in _recap_message(session)
    assert await _session_status(db, session_id) == "completed"
    assert len(await _rows(db, session_id)) == 9


# --- Review round 2 -----------------------------------------------------------------------------


async def _to_review_a(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, plan_id: int
) -> None:
    await _send(dispatcher, bot, "/train")
    await _click(dispatcher, bot, TrainPick(kind="workout", plan_id=plan_id, key="A"))
    await _click(dispatcher, bot, _last_action(session, "precheck_no"))


async def _seed_finished(
    db: Database, user_id: int, version_id: int, *, loads: dict[str, float | None], reps: int
) -> int:
    session_id = await _seed_completed_session(db, user_id, version_id, loads=loads, reps=reps)
    async with db.transaction() as conn:
        await finish_workout_session(conn, session_id, status="completed")
    return session_id


async def _start_decision(db: Database, user_id: int) -> tuple[int, list[dict[str, object]]]:
    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    start = next(
        d
        for d in decisions
        if d.kind == "session_adjust" and d.user_report and d.user_report["event"] == "start"
    )
    return start.id, start.load_changes


async def test_q1_a_skipped_block_after_an_applied_increase_keeps_the_held_load(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, plan_id, version_id = await _ready(dispatcher, bot, db)
    await _seed_finished(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=10)
    # S1: Start applies 40 -> 42.5; the squat block is skipped, the rest done.
    s1 = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "skip"))
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(minutes=30))
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert await _session_status(db, s1) == "completed"
    assert "• Barbell back squat: 42.5 kg (unchanged)" in _recap_message(session)

    # S2 the same week: the review holds 42.5 and Start proceeds with no new load change.
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(days=1))
    await _to_review_a(dispatcher, bot, session, plan_id)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    _start_id, load_changes = await _start_decision(db, user_id)
    assert load_changes == []


async def test_q2_logging_lighter_than_the_applied_load_keeps_the_held_load(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, plan_id, version_id = await _ready(dispatcher, bot, db)
    await _seed_finished(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=10)
    s1 = await _to_first_block(dispatcher, bot, session)  # applies 42.5
    llm.parse_responses.append(_parsed((1, 10, 40.0), (2, 10, 40.0), (3, 10, 40.0)))
    await _click(dispatcher, bot, _last_action(session, "enter"))
    await _send(dispatcher, bot, "10 10 10 at 40")
    await _click(dispatcher, bot, _last_action(session, "parse_ok"))
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(minutes=30))
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert await _session_status(db, s1) == "completed"

    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(days=1))
    await _to_review_a(dispatcher, bot, session, plan_id)
    assert "Barbell back squat: 3 × 8–10 @ 42.5 kg" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    _start_id, load_changes = await _start_decision(db, user_id)
    assert load_changes == []


async def test_q3_apply_of_an_unrelated_rep_change_after_an_engine_decrease(
    dispatcher: Dispatcher,
    bot: Bot,
    session: FakeSession,
    db: Database,
    llm: FakeLlm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    plan = make_plan()
    plan.workouts[0].blocks[0].items[0].load = Load(kind="kg", kg=40.0)  # stored squat: 40
    plan_id, version_id = await _seed_plan(db, user_id, plan)
    for _ in range(2):  # two completed sessions at 40 under the rep range: the engine decreases
        await _seed_finished(db, user_id, version_id, loads={_SQUAT: 40.0}, reps=5)
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(hours=1))
    llm.recap_responses.append(
        Recap(text="", suggestions=[ChangeReps(exercise_id=_PUSHUP, reps_min=10, reps_max=15)])
    )
    sid = await _to_first_block(dispatcher, bot, session)
    assert "Set 1: 8–10 reps @ 35 kg" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "done"))
    monkeypatch.setattr(clock, "now", lambda: _TUESDAY_NOON_UTC + timedelta(hours=2))
    await _click(dispatcher, bot, _last_action(session, "done"))
    assert await _session_status(db, sid) == "completed"

    await _click(dispatcher, bot, _last_action(session, "apply"))

    assert _last_text(session) == t("recap.applied", "en", name="Home plan", version=2)
    async with db.read() as conn:
        versions = await list_plan_versions(conn, plan_id)
        decisions = await list_decisions_for_user(conn, user_id)
    assert [(v.version, v.origin) for v in versions] == [(1, "llm"), (2, "progression")]
    saved = Plan.model_validate(versions[-1].body)
    squat = saved.workouts[0].blocks[0].items[0]
    # The stored 40 no longer passes; the engine's current value (35 trained with every set
    # at the top of the range, so +2.5) is what the new version carries.
    assert squat.load.kg == 37.5
    pushup = saved.workouts[0].blocks[1].items[1]
    assert (pushup.reps_min, pushup.reps_max) == (10, 15)
    confirm = next(d for d in decisions if d.kind == "plan_confirm")
    # The substituted load is above the reference (35, the last completed prescription), so
    # the confirm records it: applied once here, then held at the next Start.
    assert confirm.load_changes == [{"exercise_id": _SQUAT, "from_kg": 35.0, "to_kg": 37.5}]
    assert any(g["rule"] == "loads.substituted" for g in confirm.guards_fired)
    await _to_review_a(dispatcher, bot, session, plan_id)
    assert "Barbell back squat: 3 × 8–10 @ 37.5 kg" in _last_text(session)
    await _click(dispatcher, bot, _last_action(session, "start"))
    assert _last_text(session).startswith(t("train.block_title", "en", index=1, total=2))
    _start_id, load_changes = await _start_decision(db, user_id)
    assert load_changes == []


async def test_q4_pain_after_an_earlier_fine_still_halts(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, _ = await _ready(dispatcher, bot, db)
    sid = await _to_first_block(dispatcher, bot, session)
    await _flag(db, user_id, "shoulder_injury_current")
    await _finish_block(dispatcher, bot, session)
    await _finish_block(dispatcher, bot, session)
    async with db.read() as conn:
        (checkin,) = await list_checkins_for_session(conn, sid)
    await _click(dispatcher, bot, CheckinReply(checkin_id=checkin.id, answer="fine"))

    await _click(dispatcher, bot, CheckinReply(checkin_id=checkin.id, answer="pain"))

    assert await _holds(db, user_id) == ["checkin_pain"]
    assert _last_text(session) == t("halt.message", "en")
    async with db.read() as conn:
        (stored,) = await list_checkins_for_session(conn, sid)
    assert stored.answer == "fine"  # the first explicit answer stays; the hold is what matters


async def test_completion_creates_checkins_and_the_start_outcome_in_one_transaction(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    """`_complete()` (not the recap) creates the check-ins and the outcome; a recap that was
    never written (a crash right after completion) is shown by the next `/train`."""
    user_id, _, _ = await _ready(dispatcher, bot, db)
    sid = await _to_first_block(dispatcher, bot, session)
    await _flag(db, user_id, "shoulder_injury_current")
    await _finish_block(dispatcher, bot, session)
    # Finish the last block through the service, so no recap runs.
    advance = await training.complete_block_as_planned(db, user_id, sid, 1)
    assert advance.completion is not None
    async with db.read() as conn:
        checkins = await list_checkins_for_session(conn, sid)
        decisions = await list_decisions_for_user(conn, user_id)
    assert [c.question_key for c in checkins] == ["area:shoulder"]
    assert not any(d.kind == "progression" for d in decisions)
    start_id, _changes = await _start_decision(db, user_id)
    async with db.read() as conn:
        (outcome,) = await list_decision_outcomes(conn, start_id)
    assert outcome.outcome["completed"] is True and len(outcome.outcome["sets"]) == 9
    assert llm.calls == 0

    await _send(dispatcher, bot, "/train")

    assert llm.calls == 1
    texts = [m.text or "" for m in _sent(session)]
    assert any(x.startswith(t("recap.title", "en", workout="A — Full body")) for x in texts)
    assert t("checkin.question", "en", area="shoulder") in texts
    assert await recap.pending_recap_session(db, user_id) is None
    await _send(dispatcher, bot, "/train")
    assert llm.calls == 1  # shown once


async def test_volume_counts_both_dumbbells(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    user_id, _, version_id = await _ready(dispatcher, bot, db)
    await _seed_finished(db, user_id, version_id, loads={_SQUAT: 40.0, _BENCH: 10.0}, reps=9)
    sid = await _to_first_block(dispatcher, bot, session)
    await _click(dispatcher, bot, _last_action(session, "done"))
    await _click(dispatcher, bot, _last_action(session, "done"))  # bench 3 × 10 @ 10 kg each
    _decision_id, proposal = await _progression(db, user_id, sid)
    bench = next(item for item in proposal.summary if item.exercise_id == _BENCH)
    assert bench.volume_kg == 600.0  # 3 × 10 reps × 10 kg × 2 dumbbells
    assert "Dumbbell bench press: 3/3 sets, 30 reps, 600 kg total" in _recap_message(session)


async def test_apply_swap_drops_the_old_exercises_declared_hint(
    dispatcher: Dispatcher, bot: Bot, session: FakeSession, db: Database, llm: FakeLlm
) -> None:
    """M8b follow-up: the "your plan says 80 kg" hint belongs to the squat the pasted plan
    named; a swap to another exercise carries no hint over (and, with no history, gets the
    engine's calibration load)."""
    user_id = await _activate(dispatcher, bot, db)
    await _seed_profile(db, user_id)
    plan = make_plan()
    plan.workouts[0].blocks[0].items[0].declared_kg = 80.0
    plan_id, _version_id = await _seed_plan(db, user_id, plan)
    llm.recap_responses.append(
        Recap(
            text="",
            suggestions=[
                SwapExercise(from_exercise_id=_SQUAT, to_exercise_id="dumbbell_goblet_squat")
            ],
        )
    )
    await _complete_a(dispatcher, bot, session)
    apply = _last_action(session, "apply")

    await _click(dispatcher, bot, apply)

    assert _last_text(session) == t("recap.applied", "en", name="Home plan", version=2)
    async with db.read() as conn:
        versions = await list_plan_versions(conn, plan_id)
    assert [(v.version, v.origin) for v in versions] == [(1, "llm"), (2, "progression")]
    swapped = Plan.model_validate(versions[-1].body).workouts[0].blocks[0].items[0]
    assert swapped.exercise_id == "dumbbell_goblet_squat"
    assert swapped.load == Load(kind="calibration")
    assert swapped.declared_kg is None
    assert Plan.model_validate(versions[0].body).workouts[0].blocks[0].items[0].declared_kg == 80.0
