"""`services/planning.py` (A§6.4, IMPLEMENTATION_PLAN M6) against a real temp DB and a
`FunctionModel` LLM (no network): gates, the guard/retry loop, A§7.3 load substitution,
`load_changes` and the weekly cap, draft → confirm (re-validation, idempotency, staleness),
revise → version n+1, escalation, and plan management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme import clock
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.profile import upsert_profile, upsert_screening_flag
from fitme.db.controllers.training import (
    answer_checkin,
    insert_checkin,
    insert_health_hold,
    insert_set_log,
    insert_workout_session,
)
from fitme.db.selectors.decisions import (
    get_decision,
    list_decision_outcomes,
    list_decisions_for_user,
    list_llm_calls_since,
)
from fitme.db.selectors.plans import list_plan_versions, list_plans_for_user
from fitme.db.selectors.training import historical_max_by_exercise, list_workout_sessions_for_user
from fitme.domain.enums import AREA_FLAGS, RED_FLAGS, RefusalCode, ScreeningFlag
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    Prescription,
    Refusal,
    ScheduledDay,
    Workout,
)
from fitme.llm.agents import plan_generate_agent, plan_revise_agent
from fitme.services import planning
from fitme.services.llm_runtime import LlmRuntime

_EPOCH = "1970-01-01T00:00:00.000000Z"
_SQUAT = "barbell_back_squat"
_SQUAT_AREAS = ("knee", "lower_back", "hip", "neck")


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="x",
        db_path="./fitme-test.db",
        web_base_url="https://fit.example.org",
        secret_key="x" * 32,
    )


# --- The fake LLM ------------------------------------------------------------------------


@dataclass
class FakeLlm:
    """Answers each model call with the next queued `Plan`/`Refusal`, records every prompt
    it received, and counts calls — one queue shared by `plan_generate` and `plan_revise`."""

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
            t.name
            for t in info.output_tools
            if wanted in t.name and (wanted == "Refusal" or "Refusal" not in t.name)
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool, args=output.model_dump(mode="json"))]
        )

    def runtime(self, settings: Settings | None = None) -> LlmRuntime:
        return LlmRuntime(
            settings=settings or _settings(),
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


# --- Seeding -------------------------------------------------------------------------------


async def seed_profile(
    db: Database,
    user_id: int,
    *,
    sessions_per_week: int = 2,
    red_flags: str = "no",
    areas: tuple[ScreeningFlag, ...] = (),
) -> None:
    """A complete home-gym profile (barbell + rack + dumbbells + bench), every red flag
    answered `red_flags`, every area flag "no" except `areas`."""
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
            sessions_per_week=sessions_per_week,
            session_minutes=60,
            focus="strength",
            completed_at=clock.now(),
        )
        for flag in RED_FLAGS:
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value=red_flags, clearance=None
            )
        for flag in AREA_FLAGS:
            value = "yes" if flag in areas else "no"
            await upsert_screening_flag(
                conn, user_id=user_id, flag=flag.value, value=value, clearance=None
            )
        await upsert_screening_flag(
            conn, user_id=user_id, flag="other_unlisted", value="no", clearance=None
        )


async def seed_checkins_fine(db: Database, user_id: int, areas: tuple[str, ...]) -> None:
    async with db.transaction() as conn:
        for area in areas:
            checkin_id = await insert_checkin(
                conn, user_id=user_id, session_id=None, question_key=f"area:{area}"
            )
            await answer_checkin(conn, checkin_id, answer="fine")


async def seed_completed_squat_session(
    db: Database, user_id: int, plan_version_id: int, *, planned_kg: float, reps: int
) -> None:
    async with db.transaction() as conn:
        session_id = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=plan_version_id,
            workout_key="A",
            status="completed",
        )
        for index in (1, 2, 3):
            await insert_set_log(
                conn,
                session_id=session_id,
                exercise_id=_SQUAT,
                set_index=index,
                planned_load_kg=planned_kg,
                planned_reps_min=5,
                planned_reps_max=8,
                actual_load_kg=planned_kg,
                actual_reps=reps,
                rpe=None,
                source="button",
            )


async def open_hold(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        await insert_health_hold(conn, user_id=user_id, reason="stop_word", source_session_id=None)


# --- Plan factories ------------------------------------------------------------------------


def prescription(exercise_id: str, load: Load, *, reps: tuple[int, int] = (5, 8)) -> Prescription:
    return Prescription(
        exercise_id=exercise_id,
        sets=3,
        reps_min=reps[0],
        reps_max=reps[1],
        load=load,
        rest_seconds=90,
    )


def make_plan(
    *,
    squat_load: Load | None = None,
    extra: list[Prescription] | None = None,
    days: int = 2,
    name: str = "Home strength",
) -> Plan:
    """Two workouts (A: squat + push-up superset with dumbbell bench; B: push-ups), scheduled
    on `days` weekdays."""
    squat = prescription(_SQUAT, squat_load or Load(kind="calibration"))
    items_a: list[Block] = [
        Block(kind="single", items=[squat]),
        Block(
            kind="superset",
            items=[
                prescription("pushup", Load(kind="bodyweight"), reps=(8, 12)),
                prescription("dumbbell_bench_press", Load(kind="calibration"), reps=(8, 12)),
            ],
        ),
    ]
    if extra:
        items_a.extend(Block(kind="single", items=[item]) for item in extra)
    keys = ["A", "B"]
    return Plan(
        name=name,
        schedule=[ScheduledDay(weekday=day, workout_key=keys[day % 2]) for day in range(days)],
        workouts=[
            Workout(key="A", title="Lower + push", blocks=items_a),
            Workout(
                key="B",
                title="Push-ups",
                blocks=[
                    Block(
                        kind="single",
                        items=[prescription("pushup", Load(kind="bodyweight"), reps=(8, 12))],
                    )
                ],
            ),
        ],
    )


def bad_plan() -> Plan:
    return make_plan(extra=[prescription("unicorn_press", Load(kind="calibration"))])


async def decisions(db: Database, user_id: int) -> list:
    async with db.read() as conn:
        return list(reversed(await list_decisions_for_user(conn, user_id)))


async def llm_calls(db: Database) -> list:
    async with db.read() as conn:
        return await list_llm_calls_since(conn, since=_EPOCH)


# --- Propose + confirm ----------------------------------------------------------------------


async def test_valid_plan_is_a_draft_then_version_1_on_confirm(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([make_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.plan is not None
    assert llm.calls == 1
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_generate"]
    decision = rounds[0]
    assert decision.id == result.decision_id
    assert decision.model == _settings().llm_tier_large
    assert decision.prompt_template == "plan_generate" and decision.prompt_version == "3"
    assert decision.guards_fired  # the gate verdict at least
    assert decision.proposal is not None
    assert Plan.model_validate(decision.proposal["plan"]) == result.plan
    assert decision.proposal["proposed_load_changes"] == []
    assert decision.load_changes == []  # A§4.3: a draft never counts toward the cap
    # llm_input is exactly what the model received (B2: one rendering path).
    assert decision.llm_input == json.loads(llm.prompts[0])
    assert "guard_feedback" not in llm.prompts[0]
    calls = await llm_calls(db)
    assert len(calls) == 1 and calls[0].decision_id == decision.id
    # No plans row for an unconfirmed draft.
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)

    assert confirmed.status == planning.ConfirmStatus.SAVED
    assert confirmed.version == 1 and confirmed.is_default
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, plans[0].id)
        outcomes = await list_decision_outcomes(conn, result.decision_id)
    assert len(plans) == 1 and plans[0].is_default and plans[0].status == "active"
    assert plans[0].name == "Home strength"
    assert [v.version for v in versions] == [1]
    # The version links to the `plan_confirm` decision, whose user_report names the draft.
    assert confirmed.decision_id is not None
    assert versions[0].origin == "llm" and versions[0].decision_id == confirmed.decision_id
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id)
    assert confirm_decision is not None and confirm_decision.kind == "plan_confirm"
    assert confirm_decision.user_report == {
        "draft_decision_id": result.decision_id,
        "plan_id": plans[0].id,
    }
    assert Plan.model_validate(versions[0].body) == result.plan
    assert len(outcomes) == 1 and outcomes[0].outcome["confirmed"] is True
    assert outcomes[0].outcome["confirm_decision_id"] == confirmed.decision_id


async def test_pseudonymized_prompt_never_carries_forbidden_values(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([make_plan()])
    await planning.propose_new_plan(db, llm.runtime(), user_id)
    sent = llm.prompts[0].lower()
    assert "telegram" not in sent and "chat_id" not in sent and "screening_notes" not in sent
    assert f'"user_id": {user_id}' in llm.prompts[0]
    assert _SQUAT in sent  # an allowed catalog id


async def test_non_catalog_exercise_is_rejected_retried_then_refused(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([bad_plan(), bad_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.NO_SAFE_PLAN
    assert llm.calls == 2
    assert "guard_feedback" in llm.prompts[1] and "unicorn_press" in llm.prompts[1]
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_generate", "plan_generate", "refusal"]
    for attempt in rounds[:2]:
        assert any(g["rule"] == "plan.catalog_id" and not g["ok"] for g in attempt.guards_fired)
    assert rounds[2].id == result.decision_id
    assert rounds[2].proposal is not None and "rejected_plan" in rounds[2].proposal
    assert any(not g["ok"] for g in rounds[2].guards_fired)
    assert await planning.current_draft_id(db, user_id) is None


async def test_open_hold_refuses_with_no_llm_call(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    await open_hold(db, user_id)
    llm = FakeLlm([make_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.OPEN_HEALTH_HOLD
    assert llm.calls == 0
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["refusal"]
    assert rounds[0].guards_fired and not rounds[0].guards_fired[0]["ok"]
    assert rounds[0].llm_input is None and rounds[0].model is None
    assert await llm_calls(db) == []


async def test_incomplete_screening_refuses_with_no_llm_call(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    async with db.transaction() as conn:
        await upsert_screening_flag(
            conn, user_id=user_id, flag="heart_condition", value="unknown", clearance=None
        )
    llm = FakeLlm([make_plan()])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.SCREENING_INCOMPLETE
    assert llm.calls == 0


async def test_red_flag_without_clearance_refuses_with_no_llm_call(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id, red_flags="yes")
    llm = FakeLlm([make_plan()])
    result = await planning.propose_new_plan(db, llm.runtime(), user_id)
    assert result.refusal is not None and result.refusal.code == RefusalCode.NEEDS_CLEARANCE
    assert llm.calls == 0


async def test_incomplete_profile_refuses_with_no_llm_call(db: Database, user_id: int) -> None:
    llm = FakeLlm([make_plan()])
    result = await planning.propose_new_plan(db, llm.runtime(), user_id)
    assert result.refusal is not None and result.refusal.code == RefusalCode.PROFILE_INCOMPLETE
    assert llm.calls == 0


# --- Load handling (A§7.3) -----------------------------------------------------------------


async def test_load_only_violation_is_replaced_by_the_engine_without_a_retry(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=60.0))])  # no history: not allowed

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert llm.calls == 1  # no retry spent
    plan = result.plan
    assert plan is not None
    squat = plan.workouts[0].blocks[0].items[0]
    assert squat.load == Load(kind="calibration")
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("ceiling.historical_max", False) in rules
    assert ("loads.substituted", True) in rules
    rounds = await decisions(db, user_id)
    assert rounds[0].proposal is not None
    assert Plan.model_validate(rounds[0].proposal["plan"]) == plan
    assert rounds[0].load_changes == []


async def _saved_plan_with_squat_history(db: Database, user_id: int) -> int:
    """A confirmed plan plus one completed squat session at 40 kg that hit reps_max, so the
    reference load is 40 and the engine would propose 42.5. Returns the plan id."""
    await seed_profile(db, user_id)
    draft = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    confirmed = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    assert confirmed.plan_id is not None and confirmed.plan_version_id is not None
    await seed_completed_squat_session(
        db, user_id, confirmed.plan_version_id, planned_kg=40.0, reps=8
    )
    return confirmed.plan_id


def _squat_load(plan: Plan | None) -> Load:
    assert plan is not None
    return plan.workouts[0].blocks[0].items[0].load


async def test_proposed_increase_is_counted_once_by_the_plan_confirm_decision(
    db: Database, user_id: int
) -> None:
    """B1 (A§4.3 "load changes count once, when applied"): the draft shows the +2.5 as a
    *proposed* change and stores `load_changes = []`; confirming it is SAVED (the draft's own
    increase is not double-counted at re-validation), and the `plan_confirm` decision holds
    the change."""
    await _saved_plan_with_squat_history(db, user_id)
    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5))])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert llm.calls == 1
    assert _squat_load(result.plan) == Load(kind="kg", kg=42.5)
    assert [(c.exercise_id, c.from_kg, c.to_kg) for c in result.proposed_load_changes] == [
        (_SQUAT, 40.0, 42.5)
    ]
    rounds = await decisions(db, user_id)
    draft = rounds[-1]
    assert draft.load_changes == []
    assert draft.proposal is not None
    assert draft.proposal["proposed_load_changes"] == [
        {"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}
    ]

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)

    assert confirmed.status == planning.ConfirmStatus.SAVED, confirmed
    assert confirmed.decision_id is not None
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id)
    assert confirm_decision is not None and confirm_decision.kind == "plan_confirm"
    assert confirm_decision.load_changes == [
        {"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}
    ]
    assert confirm_decision.user_report == {
        "draft_decision_id": result.decision_id,
        "plan_id": confirmed.plan_id,
    }


async def test_a_second_plan_the_same_week_sees_the_first_confirmation(
    db: Database, user_id: int
) -> None:
    """After a confirmed +2.5 (40 -> 42.5), a second plan the same week that *keeps* 42.5 is
    not a new increase (A§7: applied this week, not counted twice) — it stays at 42.5 and its
    `plan_confirm` applies nothing; one that asks for 45 is a +2.5 on a used cap, so the
    engine's hold at 42.5 is substituted (load-only, no retry)."""
    await _saved_plan_with_squat_history(db, user_id)
    first = await planning.propose_new_plan(
        db, FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5))]).runtime(), user_id
    )
    saved = await planning.confirm_plan(db, _settings(), user_id, first.decision_id)
    assert saved.status == planning.ConfirmStatus.SAVED

    keep = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5), name="Second")])
    second = await planning.propose_new_plan(db, keep.runtime(), user_id)

    assert keep.calls == 1
    assert _squat_load(second.plan) == Load(kind="kg", kg=42.5)
    assert not any(
        g.rule in ("progression.weekly_cap", "loads.substituted") for g in second.guards_fired
    )
    assert second.proposed_load_changes == []
    confirmed = await planning.confirm_plan(db, _settings(), user_id, second.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
    assert confirm_decision is not None and confirm_decision.load_changes == []

    more = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=45.0), name="Third")])
    third = await planning.propose_new_plan(db, more.runtime(), user_id)

    assert more.calls == 1  # load-only: substituted, not retried
    assert _squat_load(third.plan) == Load(kind="kg", kg=42.5)
    rules = {(g.rule, g.ok) for g in third.guards_fired}
    assert ("progression.weekly_cap", False) in rules
    assert ("loads.substituted", True) in rules
    assert third.proposed_load_changes == []


async def test_unconfirmed_revise_drafts_do_not_consume_the_weekly_cap(
    db: Database, user_id: int
) -> None:
    """Three revise drafts each proposing +2.5, none confirmed: each still passes the cap
    (nothing was applied), and confirming the last one is SAVED with exactly one +2.5."""
    plan_id = await _saved_plan_with_squat_history(db, user_id)
    base: planning.RevisionBase = planning.PlanBase(plan_id=plan_id)
    last = None
    for _ in range(3):
        llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5))])
        last = await planning.revise_plan(db, llm.runtime(), user_id, base, "a bit heavier")
        assert llm.calls == 1
        assert _squat_load(last.plan) == Load(kind="kg", kg=42.5)
        assert not any(g.rule == "progression.weekly_cap" for g in last.guards_fired)
        assert [c.to_kg for c in last.proposed_load_changes] == [42.5]
        base = planning.DraftBase(decision_id=last.decision_id)
    assert last is not None
    rounds = await decisions(db, user_id)
    assert all(d.load_changes == [] for d in rounds if d.kind == "plan_revise")

    confirmed = await planning.confirm_plan(db, _settings(), user_id, last.decision_id)

    assert confirmed.status == planning.ConfirmStatus.SAVED and confirmed.version == 2
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
    assert confirm_decision is not None
    assert confirm_decision.load_changes == [
        {"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}
    ]


async def _confirmed_42_5(db: Database, user_id: int) -> int:
    """`_saved_plan_with_squat_history` (session at 40), then a revision to 42.5 confirmed as
    version 2 — its `plan_confirm` applied [40 -> 42.5] this week. Returns the plan id."""
    plan_id = await _saved_plan_with_squat_history(db, user_id)
    heavier = await planning.revise_plan(
        db,
        FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5))]).runtime(),
        user_id,
        planning.PlanBase(plan_id=plan_id),
        "heavier squat",
    )
    confirmed = await planning.confirm_plan(db, _settings(), user_id, heavier.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED and confirmed.version == 2
    return plan_id


async def test_keeping_a_confirmed_increase_in_a_later_revision_is_not_counted_again(
    db: Database, user_id: int
) -> None:
    """B1 (A§7): after a confirmed 42.5, an unrelated revision that keeps 42.5 must not be
    knocked back to 40 by the weekly cap; its `plan_confirm` applies no change."""
    plan_id = await _confirmed_42_5(db, user_id)
    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5), name="Renamed")])

    kept = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "rename workout B"
    )

    assert llm.calls == 1
    assert _squat_load(kept.plan) == Load(kind="kg", kg=42.5)
    assert not any(
        g.rule in ("progression.weekly_cap", "loads.substituted") for g in kept.guards_fired
    )
    assert kept.proposed_load_changes == []
    confirmed = await planning.confirm_plan(db, _settings(), user_id, kept.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED and confirmed.version == 3
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
        versions = await list_plan_versions(conn, plan_id)
    assert confirm_decision is not None and confirm_decision.load_changes == []
    assert Plan.model_validate(versions[-1].body).workouts[0].blocks[0].items[0].load == Load(
        kind="kg", kg=42.5
    )


async def test_a_further_increase_on_top_of_the_confirmed_one_is_capped(
    db: Database, user_id: int
) -> None:
    """Same week, the revision proposes 45: only the +2.5 above the applied 42.5 is an
    increase, and the cap is already used — the engine's hold at 42.5 is substituted."""
    plan_id = await _confirmed_42_5(db, user_id)
    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=45.0))])

    result = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "even heavier"
    )

    assert llm.calls == 1
    assert _squat_load(result.plan) == Load(kind="kg", kg=42.5)
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("progression.weekly_cap", False) in rules
    assert ("loads.substituted", True) in rules
    assert result.proposed_load_changes == []


async def test_after_the_window_keeping_the_load_counts_as_a_fresh_increase(
    db: Database, user_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expected by design: once the confirmed 42.5 falls out of the trailing 7 days and no
    session was done at it, the last completed session (40) is the reference again, so
    keeping 42.5 counts as +2.5 against an empty window — allowed, and applied afresh."""
    plan_id = await _confirmed_42_5(db, user_id)
    # Simulate "7 days later": the window starts after every decision written so far.
    monkeypatch.setattr(
        planning, "_since_7d", lambda: clock.format_timestamp(clock.now() + timedelta(minutes=1))
    )
    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=42.5), name="Week two")])

    kept = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=plan_id), "new week"
    )

    assert _squat_load(kept.plan) == Load(kind="kg", kg=42.5)
    assert [(c.from_kg, c.to_kg) for c in kept.proposed_load_changes] == [(40.0, 42.5)]
    confirmed = await planning.confirm_plan(db, _settings(), user_id, kept.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
    assert confirm_decision is not None
    assert confirm_decision.load_changes == [
        {"exercise_id": _SQUAT, "from_kg": 40.0, "to_kg": 42.5}
    ]


async def test_kg_on_a_non_kg_loadable_exercise_is_replaced_without_a_retry(
    db: Database, user_id: int
) -> None:
    """A§4.4 "non-kg exercises": a kg load on a push-up fails `plan.kg_loadable`; it is a
    load rule, so the engine's `bodyweight` is substituted and no retry is spent."""
    await seed_profile(db, user_id)
    plan = make_plan()
    plan.workouts[1].blocks[0].items[0].load = Load(kind="kg", kg=20.0)  # push-up
    llm = FakeLlm([plan])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert llm.calls == 1
    assert result.plan is not None
    assert result.plan.workouts[1].blocks[0].items[0].load == Load(kind="bodyweight")
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("plan.kg_loadable", False) in rules
    assert ("loads.substituted", True) in rules


async def test_forbidden_wording_in_model_text_is_replaced_or_dropped(
    db: Database, user_id: int
) -> None:
    """AGENTS.md §3 over model-authored display text: a bad plan name and workout title get
    neutral defaults, a bad note is dropped, and each replacement is logged."""
    await seed_profile(db, user_id)
    plan = make_plan(name="Your personal trainer plan")
    plan.workouts[0].title = "Rehab day"
    plan.workouts[0].blocks[0].items[0].note = "this cures knee pain"
    plan.workouts[1].blocks[0].items[0].note = "brace the core"
    llm = FakeLlm([plan])

    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    shown = result.plan
    assert shown is not None
    assert shown.name == "Plan"
    assert shown.workouts[0].title == "Workout A"
    assert shown.workouts[1].title == "Push-ups"  # untouched
    assert shown.workouts[0].blocks[0].items[0].note is None
    assert shown.workouts[1].blocks[0].items[0].note == "brace the core"
    wording_hits = [g for g in result.guards_fired if g.rule == "wording.forbidden_term"]
    assert len(wording_hits) == 3
    rounds = await decisions(db, user_id)
    assert rounds[-1].proposal is not None
    assert Plan.model_validate(rounds[-1].proposal["plan"]).name == "Plan"

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
    assert plans[0].name == "Plan"


# --- Confirm: re-validation, idempotency, staleness ---------------------------------------


async def test_confirm_revalidates_and_refuses_after_a_hold_opened(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([make_plan()])
    draft = await planning.propose_new_plan(db, llm.runtime(), user_id)
    await open_hold(db, user_id)

    confirmed = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)

    assert confirmed.status == planning.ConfirmStatus.REFUSED
    assert confirmed.refusal is not None
    assert confirmed.refusal.code == RefusalCode.OPEN_HEALTH_HOLD
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_generate", "refusal"]
    assert rounds[1].id == confirmed.decision_id
    assert rounds[1].user_report == {"confirm_of": draft.decision_id}


async def test_double_confirm_creates_one_plan(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    llm = FakeLlm([make_plan()])
    draft = await planning.propose_new_plan(db, llm.runtime(), user_id)

    first = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    second = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)

    assert first.status == planning.ConfirmStatus.SAVED
    assert second.status == planning.ConfirmStatus.ALREADY_SAVED
    assert second.plan_id == first.plan_id and second.plan_version_id == first.plan_version_id
    # The confirm itself ends the round: the confirmed draft is no longer "current".
    assert await planning.current_draft_id(db, user_id) is None
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        assert len(plans) == 1
        assert len(await list_plan_versions(conn, plans[0].id)) == 1


async def test_stale_confirm_is_rejected(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    older = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    newer = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)

    stale = await planning.confirm_plan(db, _settings(), user_id, older.decision_id)
    assert stale.status == planning.ConfirmStatus.STALE
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []

    assert await planning.current_draft_id(db, user_id) == newer.decision_id
    fresh = await planning.confirm_plan(db, _settings(), user_id, newer.decision_id)
    assert fresh.status == planning.ConfirmStatus.SAVED


async def test_confirm_of_a_refusal_or_foreign_decision_is_not_found(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    await open_hold(db, user_id)
    refused = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    result = await planning.confirm_plan(db, _settings(), user_id, refused.decision_id)
    assert result.status == planning.ConfirmStatus.NOT_FOUND
    missing = await planning.confirm_plan(db, _settings(), user_id, 999_999)
    assert missing.status == planning.ConfirmStatus.NOT_FOUND


# --- Revise --------------------------------------------------------------------------------


async def test_revise_then_confirm_creates_version_2_on_the_same_plan(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    draft = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    saved = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    assert saved.plan_id is not None

    revised_plan = make_plan(name="Home strength v2")
    llm = FakeLlm([revised_plan])
    result = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.PlanBase(plan_id=saved.plan_id), "rename it"
    )

    assert result.plan == revised_plan and result.plan_id == saved.plan_id
    assert llm.calls == 1
    assert json.loads(llm.prompts[0])["user_request"] == "rename it"
    assert "current_plan" in json.loads(llm.prompts[0])
    rounds = await decisions(db, user_id)
    assert rounds[-1].kind == "plan_revise" and rounds[-1].model == _settings().llm_tier_medium
    assert rounds[-1].load_changes == []
    assert rounds[-1].user_report == {
        "base": "plan_version",
        "base_plan_version_id": saved.plan_version_id,
        "plan_id": saved.plan_id,
    }
    calls = await llm_calls(db)
    assert calls[-1].decision_id == rounds[-1].id

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)

    assert confirmed.status == planning.ConfirmStatus.SAVED
    assert confirmed.plan_id == saved.plan_id and confirmed.version == 2
    async with db.read() as conn:
        plans = await list_plans_for_user(conn, user_id)
        versions = await list_plan_versions(conn, saved.plan_id)
    assert len(plans) == 1
    assert [v.version for v in versions] == [1, 2]
    assert Plan.model_validate(versions[1].body).name == "Home strength v2"


async def test_revise_a_draft_keeps_the_chain_and_scrubs_the_request(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    draft = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)

    llm = FakeLlm([make_plan(name="Lighter")])
    result = await planning.revise_plan(
        db,
        llm.runtime(),
        user_id,
        planning.DraftBase(decision_id=draft.decision_id),
        "call me at 415-555-0132, and drop the superset",
    )

    assert result.plan is not None and result.plan.name == "Lighter"
    assert "415-555-0132" not in llm.prompts[0]
    assert "[redacted]" in llm.prompts[0]
    # The old draft is superseded: confirming it is stale, confirming the new one works.
    assert (
        await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    ).status == planning.ConfirmStatus.STALE
    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED and confirmed.version == 1


async def test_revise_failing_guards_twice_escalates_once_then_refuses(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    draft = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    saved = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    assert saved.plan_id is not None
    settings = _settings()

    llm = FakeLlm([bad_plan(), bad_plan(), bad_plan()])
    result = await planning.revise_plan(
        db, llm.runtime(settings), user_id, planning.PlanBase(plan_id=saved.plan_id), "more"
    )

    assert result.refusal is not None and result.refusal.code == RefusalCode.NO_SAFE_PLAN
    assert llm.calls == 3
    assert "guard_feedback" not in llm.prompts[0]
    assert "guard_feedback" in llm.prompts[1] and "guard_feedback" in llm.prompts[2]
    calls = await llm_calls(db)
    assert [c.model for c in calls[-3:]] == [
        settings.llm_tier_medium,
        settings.llm_tier_medium,
        settings.llm_tier_large,
    ]
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds[-4:]] == ["plan_revise", "plan_revise", "plan_revise", "refusal"]
    assert [d.model for d in rounds[-4:-1]] == [c.model for c in calls[-3:]]
    assert [c.decision_id for c in calls[-3:]] == [d.id for d in rounds[-4:-1]]
    for attempt in rounds[-4:-1]:
        assert any(not g["ok"] for g in attempt.guards_fired)
        assert attempt.llm_input is not None
    assert rounds[-1].id == result.decision_id
    async with db.read() as conn:
        assert len(await list_plan_versions(conn, saved.plan_id)) == 1


async def test_revise_with_a_stale_or_unknown_base_raises(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    older = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    llm = FakeLlm([make_plan()])
    try:
        await planning.revise_plan(
            db, llm.runtime(), user_id, planning.DraftBase(decision_id=older.decision_id), "x"
        )
    except planning.StaleDraftError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected StaleDraftError")
    try:
        await planning.revise_plan(db, llm.runtime(), user_id, planning.PlanBase(plan_id=42), "x")
    except planning.PlanNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected PlanNotFoundError")
    assert llm.calls == 0


async def test_revise_gate_refuses_with_no_llm_call(db: Database, user_id: int) -> None:
    await seed_profile(db, user_id)
    draft = await planning.propose_new_plan(db, FakeLlm([make_plan()]).runtime(), user_id)
    await open_hold(db, user_id)
    llm = FakeLlm([make_plan()])
    result = await planning.revise_plan(
        db, llm.runtime(), user_id, planning.DraftBase(decision_id=draft.decision_id), "x"
    )
    assert result.refusal is not None and result.refusal.code == RefusalCode.OPEN_HEALTH_HOLD
    assert llm.calls == 0


# --- Plan management -----------------------------------------------------------------------


async def _two_saved_plans(db: Database, user_id: int) -> tuple[int, int]:
    first = await planning.propose_new_plan(db, FakeLlm([make_plan(name="One")]).runtime(), user_id)
    saved_first = await planning.confirm_plan(db, _settings(), user_id, first.decision_id)
    second = await planning.propose_new_plan(
        db, FakeLlm([make_plan(name="Two")]).runtime(), user_id
    )
    saved_second = await planning.confirm_plan(db, _settings(), user_id, second.decision_id)
    assert saved_first.plan_id is not None and saved_second.plan_id is not None
    return saved_first.plan_id, saved_second.plan_id


async def test_any_plan_can_be_set_as_default(db: Database, user_id: int) -> None:
    """A§4.3 "all plans are equal": there is no archived state, and every plan of the
    user's can become the default; a foreign/unknown plan can't."""
    await seed_profile(db, user_id)
    one, two = await _two_saved_plans(db, user_id)

    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].is_default and not plans[two].is_default  # the first stays default
    assert all(p.status == "active" for p in plans.values())

    assert await planning.set_default(db, user_id, two)
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[two].is_default and not plans[one].is_default

    assert await planning.set_default(db, user_id, one)
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].is_default and not plans[two].is_default

    # A new plan confirmed while a default exists doesn't take the default over.
    draft = await planning.propose_new_plan(
        db, FakeLlm([make_plan(name="Three")]).runtime(), user_id
    )
    saved = await planning.confirm_plan(db, _settings(), user_id, draft.decision_id)
    assert not saved.is_default

    assert not hasattr(planning, "archive")
    assert await planning.get_plan_detail(db, user_id, 999) is None
    detail = await planning.get_plan_detail(db, user_id, one)
    assert detail is not None and detail.plan.name == "One" and detail.version.version == 1
    assert not await planning.set_default(db, user_id, 999)


async def test_rename_plan_validates_and_logs_a_minimal_user_edit(
    db: Database, user_id: int
) -> None:
    """`rename_plan`: trim, 1–60 characters, the AGENTS.md §3 wording check and ownership,
    in one place for the bot and the website. A saved rename is a `user_edit` decision that
    names the action and the plan, never the names."""
    await seed_profile(db, user_id)
    one, two = await _two_saved_plans(db, user_id)
    before = len(await decisions(db, user_id))

    result = await planning.rename_plan(db, user_id, one, "  Upper / lower  ")
    assert result.status == planning.RenameStatus.OK and result.name == "Upper / lower"
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].name == "Upper / lower" and plans[two].name == "Two"
    logged = [d for d in await decisions(db, user_id) if d.kind == "user_edit"]
    assert len(logged) == 1 and len(await decisions(db, user_id)) == before + 1
    assert logged[0].user_report == {"action": "rename", "plan_id": one}
    assert logged[0].load_changes == [] and logged[0].proposal is None
    assert "Upper" not in json.dumps(logged[0].user_report)

    assert (await planning.rename_plan(db, user_id, one, "   ")).status == (
        planning.RenameStatus.EMPTY
    )
    assert (await planning.rename_plan(db, user_id, one, "x" * 61)).status == (
        planning.RenameStatus.TOO_LONG
    )
    assert (await planning.rename_plan(db, user_id, one, "x" * 60)).status == (
        planning.RenameStatus.OK
    )
    forbidden = await planning.rename_plan(db, user_id, one, "My personal trainer plan")
    assert forbidden.status == planning.RenameStatus.FORBIDDEN and forbidden.term == "trainer"
    assert (await planning.rename_plan(db, user_id, one, "Похудение")).status == (
        planning.RenameStatus.FORBIDDEN
    )
    assert (await planning.rename_plan(db, user_id, 999, "Nope")).status == (
        planning.RenameStatus.NOT_FOUND
    )
    assert (await planning.rename_plan(db, user_id + 1, one, "Nope")).status == (
        planning.RenameStatus.NOT_FOUND
    )
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].name == "x" * 60  # nothing rejected was saved
    # Only the two successful renames were logged.
    assert len([d for d in await decisions(db, user_id) if d.kind == "user_edit"]) == 2


# --- Delete (A§4.3 "any plan can be deleted") -----------------------------------------------


async def _rows(db: Database, sql: str) -> list[tuple[object, ...]]:
    async with db.read() as conn, conn.execute(sql) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def test_delete_plan_aborts_and_detaches_every_session_then_removes_the_plan(
    db: Database, user_id: int
) -> None:
    """A§4.3 `delete_plan` steps 2-4: a `draft`/`confirmed`/`in_progress` session on the
    plan is aborted; every session (any status) is detached (`plan_version_id` -> NULL,
    `workout_key` kept, `set_logs` untouched); the plan's versions and row are gone."""
    plan_id = await _saved_plan_with_squat_history(db, user_id)  # 1 completed session, 40 kg
    async with db.read() as conn:
        version = await list_plan_versions(conn, plan_id)
    version_id = version[-1].id

    async with db.transaction() as conn:
        draft_session = await insert_workout_session(
            conn, user_id=user_id, plan_version_id=version_id, workout_key="A", status="draft"
        )
        in_progress_session = await insert_workout_session(
            conn,
            user_id=user_id,
            plan_version_id=version_id,
            workout_key="B",
            status="in_progress",
        )

    result = await planning.delete_plan(db, user_id, plan_id)

    assert result.status == planning.DeleteStatus.OK
    assert await planning.get_plan_detail(db, user_id, plan_id) is None
    async with db.read() as conn:
        assert await list_plans_for_user(conn, user_id) == []
        assert await list_plan_versions(conn, plan_id) == []

    async with db.read() as conn:
        sessions = {s.id: s for s in await list_workout_sessions_for_user(conn, user_id)}
    assert len(sessions) == 3
    for session in sessions.values():
        assert session.plan_version_id is None  # every session is detached
    assert sessions[draft_session].status == "aborted"
    assert sessions[draft_session].workout_key == "A"
    assert sessions[in_progress_session].status == "aborted"
    assert sessions[in_progress_session].workout_key == "B"
    completed_ids = set(sessions) - {draft_session, in_progress_session}
    assert len(completed_ids) == 1
    completed = sessions[completed_ids.pop()]
    assert completed.status == "completed"  # a finished session is left as it is
    assert completed.workout_key == "A"

    # No dangling FK, and history is untouched (AGENTS.md §2/§4.3: deleting a plan keeps the
    # data the guards read).
    assert await _rows(db, "PRAGMA foreign_key_check") == []
    async with db.read() as conn:
        assert (await historical_max_by_exercise(conn, user_id))[_SQUAT] == 40.0


async def test_delete_plan_reassigns_the_default_to_the_newest_remaining_plan(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    one, two = await _two_saved_plans(db, user_id)
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].is_default and not plans[two].is_default

    # Deleting the non-default plan changes nothing about the default.
    result = await planning.delete_plan(db, user_id, two)
    assert result.status == planning.DeleteStatus.OK and result.new_default_plan_id is None
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert plans[one].is_default

    third = await planning.propose_new_plan(
        db, FakeLlm([make_plan(name="Three")]).runtime(), user_id
    )
    saved_third = await planning.confirm_plan(db, _settings(), user_id, third.decision_id)
    assert saved_third.plan_id is not None and not saved_third.is_default

    # Deleting the default reassigns it to the most recently created remaining plan.
    result = await planning.delete_plan(db, user_id, one)
    assert result.status == planning.DeleteStatus.OK
    assert result.new_default_plan_id == saved_third.plan_id
    plans = {p.id: p for p in await planning.list_plans(db, user_id)}
    assert set(plans) == {saved_third.plan_id}
    assert plans[saved_third.plan_id].is_default

    # Deleting the last plan leaves no default to reassign.
    result = await planning.delete_plan(db, user_id, saved_third.plan_id)
    assert result.status == planning.DeleteStatus.OK and result.new_default_plan_id is None
    assert await planning.list_plans(db, user_id) == []


async def test_delete_plan_checks_ownership_and_logs_a_minimal_user_edit(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)
    one, two = await _two_saved_plans(db, user_id)
    before = len(await decisions(db, user_id))

    assert (await planning.delete_plan(db, user_id, 999)).status == planning.DeleteStatus.NOT_FOUND
    assert (
        await planning.delete_plan(db, user_id + 1, one)
    ).status == planning.DeleteStatus.NOT_FOUND
    # Nothing changed by either rejected call.
    assert {p.id for p in await planning.list_plans(db, user_id)} == {one, two}
    assert len(await decisions(db, user_id)) == before

    result = await planning.delete_plan(db, user_id, two)
    assert result.status == planning.DeleteStatus.OK

    logged = [d for d in await decisions(db, user_id) if d.kind == "user_edit"]
    assert len(logged) == 1
    assert logged[0].user_report == {"action": "delete_plan", "plan_id": two}
    assert logged[0].load_changes == [] and logged[0].proposal is None
    # The plan's own draft/confirm decisions are kept (append-only, A§4.3: "decisions are
    # kept"); only the new user_edit decision was added.
    assert len(await decisions(db, user_id)) == before + 1


async def test_deleting_a_plan_does_not_reset_the_weekly_cap_for_other_plans(
    db: Database, user_id: int
) -> None:
    """A§4.3 "deleting a plan never raises what the guards allow": the weekly cap and the
    ceiling come from `set_logs`/`decisions.load_changes`, never from `plans`. After a plan's
    `plan_confirm` applies +2.5 this week and that plan is deleted, a *different* plan's
    confirm the same week still sees the cap as used, not reset."""
    plan_id = await _confirmed_42_5(db, user_id)  # plan_confirm applied 40 -> 42.5 this week

    deleted = await planning.delete_plan(db, user_id, plan_id)
    assert deleted.status == planning.DeleteStatus.OK
    assert await planning.list_plans(db, user_id) == []

    llm = FakeLlm([make_plan(squat_load=Load(kind="kg", kg=45.0), name="Fresh")])
    result = await planning.propose_new_plan(db, llm.runtime(), user_id)

    assert llm.calls == 1  # load-only substitution, no retry spent
    assert _squat_load(result.plan) == Load(kind="kg", kg=42.5)
    rules = {(g.rule, g.ok) for g in result.guards_fired}
    assert ("progression.weekly_cap", False) in rules
    assert ("loads.substituted", True) in rules
    assert result.proposed_load_changes == []

    confirmed = await planning.confirm_plan(db, _settings(), user_id, result.decision_id)
    assert confirmed.status == planning.ConfirmStatus.SAVED
    async with db.read() as conn:
        confirm_decision = await get_decision(conn, confirmed.decision_id or 0)
    assert confirm_decision is not None and confirm_decision.load_changes == []


# --- Output-validation retry (bug fix, M8b) --------------------------------------------------


def _generate_runtime(outputs: list[Plan | str]) -> LlmRuntime:
    """A `plan_generate` runtime answering each *run* from `outputs`: a `Plan`, or `"invalid"`
    to make every output retry of that run fail pydantic validation (an out-of-range weekday),
    so the run ends in `run_agent`'s `LLM_UNAVAILABLE` refusal with `CAUSE_OUTPUT_VALIDATION`."""
    runs: list[Plan | str] = list(outputs)
    current: list[Plan | str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        if any(part.part_kind == "user-prompt" for part in request.parts):
            current[:] = [runs.pop(0)]  # a new run (not an output retry within one)
        output = current[0]
        tool = next(
            t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name
        )
        if output == "invalid":
            args: dict[str, object] = {
                "name": "Bad",
                "schedule": [{"weekday": 9, "workout_key": "A"}],
                "workouts": [],
            }
        else:
            assert isinstance(output, Plan)
            args = output.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    return LlmRuntime(
        settings=_settings(),
        prices={},
        agent_factories={
            "plan_generate": lambda model: plan_generate_agent(
                FunctionModel(respond, model_name=str(model))
            )
        },
    )


async def test_generate_retries_once_after_an_output_validation_failure(
    db: Database, user_id: int
) -> None:
    """Bug fix: `propose_new_plan`'s own retry loop used to end the round on the first
    output-validation refusal. It now spends the second attempt on it, like a guard failure,
    and every attempt is still its own decision with the cause recorded."""
    await seed_profile(db, user_id)

    result = await planning.propose_new_plan(
        db, _generate_runtime(["invalid", make_plan()]), user_id
    )

    assert result.plan is not None
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_generate", "plan_generate"]
    assert rounds[0].proposal is not None
    assert rounds[0].proposal["refusal"]["code"] == "llm_unavailable"
    assert rounds[0].proposal["cause"] == "output_validation"
    assert rounds[1].id == result.decision_id
    calls = await llm_calls(db)
    assert [c.ok for c in calls] == [False, True]


async def test_generate_refuses_after_two_output_validation_failures(
    db: Database, user_id: int
) -> None:
    await seed_profile(db, user_id)

    result = await planning.propose_new_plan(db, _generate_runtime(["invalid", "invalid"]), user_id)

    assert result.refusal is not None
    assert result.refusal.code == RefusalCode.LLM_UNAVAILABLE
    rounds = await decisions(db, user_id)
    assert [d.kind for d in rounds] == ["plan_generate", "plan_generate"]
    assert rounds[-1].id == result.decision_id  # the retry's own logged refusal is the answer
    assert all(d.proposal and d.proposal["cause"] == "output_validation" for d in rounds)
    assert [c.ok for c in await llm_calls(db)] == [False, False]
    assert await planning.current_draft_id(db, user_id) is None


async def test_generate_never_keeps_a_models_declared_kg(db: Database, user_id: int) -> None:
    """M8b: nothing was pasted, so a `declared_kg` the model emits is stripped."""
    await seed_profile(db, user_id)
    plan = make_plan()
    plan.workouts[0].blocks[0].items[0].declared_kg = 80.0
    result = await planning.propose_new_plan(db, FakeLlm([plan]).runtime(), user_id)
    assert result.plan is not None
    assert all(
        item.declared_kg is None
        for workout in result.plan.workouts
        for block in workout.blocks
        for item in block.items
    )
