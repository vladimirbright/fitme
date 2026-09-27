"""`llm/escalation.py` (A§8.5 rule 3): fails twice on the normal tier -> one large-tier
attempt -> refusal, with every attempt's model recorded in its own `llm_calls` row. No
network: every model here is a `pydantic_ai.models.function.FunctionModel`."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.selectors.decisions import list_llm_calls_since
from fitme.domain.enums import RefusalCode
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    Prescription,
    Refusal,
    ScheduledDay,
    Workout,
)
from fitme.llm.agents import plan_revise_agent
from fitme.llm.escalation import run_with_escalation
from fitme.llm.usage import CAUSE_OUTPUT_VALIDATION, AgentRunOutcome

_PLAN = Plan(
    name="Plan",
    schedule=[ScheduledDay(weekday=0, workout_key="A")],
    workouts=[
        Workout(
            key="A",
            title="Full body",
            blocks=[
                Block(
                    kind="single",
                    items=[
                        Prescription(
                            exercise_id="barbell_back_squat",
                            sets=3,
                            reps_min=5,
                            reps_max=8,
                            load=Load(kind="calibration"),
                            rest_seconds=90,
                        )
                    ],
                )
            ],
        )
    ],
)


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="x",
        db_path="./fitme-test.db",
        web_base_url="https://fit.example.org",
        secret_key="x" * 32,
    )


def _plan_response_factory():
    def make_response(messages: list, info: AgentInfo) -> ModelResponse:
        tool = next(
            t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name
        )
        args = _PLAN.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    return make_response


def _agent_factory(model):
    return plan_revise_agent(FunctionModel(_plan_response_factory(), model_name=str(model)))


def _always_fail_guard(_output: Plan) -> Sequence[GuardVerdict]:
    return [GuardVerdict(rule="test.always_fail", ok=False, detail="never passes")]


def _always_pass_guard(_output: Plan) -> Sequence[GuardVerdict]:
    return [GuardVerdict(rule="test.always_pass", ok=True, detail="fine")]


async def test_escalation_succeeds_on_the_first_attempt_when_guards_pass(
    db: Database,
) -> None:
    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=_agent_factory,
        build_prompt=lambda verdicts: "revise please",
        guard_check=_always_pass_guard,
        settings=_settings(),
        db=db,
        prices={},
        language="en",
    )

    assert outcome.output == _PLAN
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].record.model == _settings().llm_tier_medium


async def test_escalation_fails_twice_on_medium_then_once_on_large_then_refuses(
    db: Database,
) -> None:
    settings = _settings()
    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=_agent_factory,
        build_prompt=lambda verdicts: "retry" if verdicts else "first attempt",
        guard_check=_always_fail_guard,
        settings=settings,
        db=db,
        prices={},
        language="en",
    )

    assert isinstance(outcome.output, Refusal)
    assert outcome.output.code == RefusalCode.NO_SAFE_PLAN

    assert len(outcome.attempts) == 3
    assert outcome.attempts[0].record.model == settings.llm_tier_medium
    assert outcome.attempts[1].record.model == settings.llm_tier_medium
    assert outcome.attempts[2].record.model == settings.llm_tier_large

    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert len(calls) == 3
    assert [call.model for call in calls] == [
        settings.llm_tier_medium,
        settings.llm_tier_medium,
        settings.llm_tier_large,
    ]
    assert all(call.ok for call in calls)

    # B2: each attempt carries the exact prompt sent and the prompt template/version used —
    # available for M6 to store one `decisions` row per round, not just a final summary.
    assert [attempt.prompt.user_prompt for attempt in outcome.attempts if attempt.prompt] == [
        "first attempt",
        "retry",
        "retry",
    ]
    for attempt in outcome.attempts:
        assert attempt.prompt is not None
        assert attempt.prompt.template_name == "plan_revise"
        assert attempt.prompt.version == 1


async def test_escalation_succeeds_on_the_large_tier_attempt(db: Database) -> None:
    settings = _settings()
    call_count = 0

    def guard_check(_output: Plan) -> Sequence[GuardVerdict]:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return [GuardVerdict(rule="test.transient", ok=False, detail="not yet")]
        return [GuardVerdict(rule="test.transient", ok=True, detail="passes on the third try")]

    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=_agent_factory,
        build_prompt=lambda verdicts: "retry" if verdicts else "first attempt",
        guard_check=guard_check,
        settings=settings,
        db=db,
        prices={},
        language="en",
    )

    assert outcome.output == _PLAN
    assert len(outcome.attempts) == 3
    assert outcome.attempts[2].record.model == settings.llm_tier_large


async def test_an_llm_refusal_short_circuits_without_further_escalation(db: Database) -> None:
    def make_refusal_response(messages: list, info: AgentInfo) -> ModelResponse:
        tool = next(t.name for t in info.output_tools if "Refusal" in t.name)
        refusal = Refusal(code=RefusalCode.OUT_OF_SCOPE, message="not applicable")
        args = refusal.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    def agent_factory(model):
        return plan_revise_agent(FunctionModel(make_refusal_response, model_name=str(model)))

    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=agent_factory,
        build_prompt=lambda verdicts: "revise please",
        guard_check=_always_pass_guard,
        settings=_settings(),
        db=db,
        prices={},
        language="en",
    )

    assert isinstance(outcome.output, Refusal)
    assert outcome.output.code == RefusalCode.OUT_OF_SCOPE
    assert len(outcome.attempts) == 1


def _invalid_plan_response(messages: list, info: AgentInfo) -> ModelResponse:
    """A `Plan` tool call that fails pydantic validation regardless of how many times
    pydantic-ai retries it within one `agent.run()` call (an out-of-range weekday — not
    display-text length, which is trimmed rather than rejected)."""
    tool = next(t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name)
    args = {"name": "Bad plan", "schedule": [{"weekday": 9, "workout_key": "A"}], "workouts": []}
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])


async def test_output_validation_failure_on_the_normal_tier_escalates_to_the_large_tier(
    db: Database,
) -> None:
    """Bug fix: `plan_revise` failing pydantic validation through every output retry (an
    `UnexpectedModelBehavior`, turned into `Refusal(LLM_UNAVAILABLE)` by `run_agent`) used to
    end the round immediately, with no large-tier attempt — `_one_attempt` treated *any*
    `Refusal` as a final answer. It's now eligible for the same one large-tier attempt a guard
    failure gets: the normal tier fails validation, the large tier returns a valid plan, and
    both attempts are recorded (not three — an output-validation failure has no guard feedback
    to retry the normal tier with, so it goes straight to the large tier)."""
    settings = _settings()

    def agent_factory(model: object):
        model_str = str(model)
        if model_str == settings.llm_tier_large:
            return plan_revise_agent(FunctionModel(_plan_response_factory(), model_name=model_str))
        return plan_revise_agent(FunctionModel(_invalid_plan_response, model_name=model_str))

    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=agent_factory,
        build_prompt=lambda verdicts: "revise please",
        guard_check=_always_pass_guard,
        settings=settings,
        db=db,
        prices={},
        language="en",
    )

    assert outcome.output == _PLAN
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].record.model == settings.llm_tier_medium
    assert outcome.attempts[0].record.ok is False
    assert outcome.attempts[0].record.error_cause == CAUSE_OUTPUT_VALIDATION
    assert outcome.attempts[1].record.model == settings.llm_tier_large
    assert outcome.attempts[1].record.ok is True

    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert len(calls) == 2
    assert [call.model for call in calls] == [settings.llm_tier_medium, settings.llm_tier_large]


async def test_on_attempt_hook_runs_per_attempt_and_links_the_llm_call(db: Database) -> None:
    """M6 writes each attempt's `decisions` row from the hook; its id lands on that attempt's
    `llm_calls.decision_id`. The hook sees the guard verdicts of the attempt it's called for."""
    from fitme.db.controllers.users import insert_user

    async with db.transaction() as conn:
        user_id = await insert_user(conn, language="en", timezone=None)
    seen: list[tuple[str, int]] = []

    async def on_attempt(
        outcome: AgentRunOutcome[Plan], verdicts: Sequence[GuardVerdict]
    ) -> int | None:
        async with db.transaction() as conn:
            decision_id = await insert_decision(
                conn,
                user_id=user_id,
                kind="plan_revise",
                prompt_template=None,
                prompt_version=None,
                model=outcome.record.model,
                content_version="abc123def456",
                llm_input=None,
                user_report=None,
                proposal=None,
                guards_fired=[v.model_dump() for v in verdicts],
            )
        seen.append((outcome.record.model, decision_id))
        return decision_id

    outcome = await run_with_escalation(
        agent_name="plan_revise",
        agent_factory=_agent_factory,
        build_prompt=lambda verdicts: "retry" if verdicts else "first attempt",
        guard_check=_always_fail_guard,
        settings=_settings(),
        db=db,
        prices={},
        language="en",
        on_attempt=on_attempt,
    )

    assert isinstance(outcome.output, Refusal)
    assert len(seen) == 3
    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert [(c.model, c.decision_id) for c in calls] == seen
