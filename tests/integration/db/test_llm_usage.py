"""`llm/usage.py`: a `FunctionModel` run recording an `llm_calls` row and a `decisions` row
(prompt version, resolved model id, content_version — read from `AgentRunOutcome.prompt`, B2),
the provider-error -> `Refusal` path (`ok=0`), and the agent-construction-error -> `Refusal`
path (B3, `UserError`). No network: every model here is a
`pydantic_ai.models.function.FunctionModel`.

Also the bug-fix diagnostics: `error_type=UnexpectedModelBehavior` in the operator's log used
to be the only clue that `plan_revise` had refused — no hint of *why* the model's structured
output failed pydantic validation. `test_output_validation_failure_logs_safe_diagnostics_*`
below reproduces that failure with a `FunctionModel` and checks the WARNING log record carries
`loc`/`type`/`msg` for the underlying pydantic errors, and nothing else (never the offending
value, the prompt, or a response body — A§10).
"""

from __future__ import annotations

import logging

import pytest
from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.exceptions import ModelHTTPError, UserError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.selectors.decisions import get_decision, list_llm_calls_since
from fitme.domain.enums import DecisionKind, RefusalCode
from fitme.domain.models import (
    Block,
    Load,
    Plan,
    PlanProposal,
    Prescription,
    Refusal,
    ScheduledDay,
    Workout,
)
from fitme.llm.agents import BuiltAgent, plan_generate_agent
from fitme.llm.context import build_user_context, render_user_prompt
from fitme.llm.usage import (
    CAUSE_OUTPUT_VALIDATION,
    CAUSE_PROVIDER_ERROR,
    record_llm_call,
    run_agent,
)
from fitme.services.decisions import record_decision

_SAMPLE_PLAN = Plan(
    name="Full body A/B",
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
                            rest_seconds=120,
                        )
                    ],
                )
            ],
        )
    ],
)


def _sample_context():
    from fitme.domain.enums import (
        AgeBucket,
        BarbellExperience,
        Experience,
        Focus,
        Location,
        WeightBucket,
    )

    return build_user_context(
        user_id=1,
        language="en",
        age_bucket=AgeBucket.AGE_30_39,
        weight_bucket=WeightBucket.KG_80_89,
        experience=Experience.M6_TO_2Y,
        barbell_experience=BarbellExperience.SOME,
        preferences=[],
        focus=Focus.STRENGTH,
        location=Location.PUBLIC_GYM,
        equipment=[],
        sessions_per_week=3,
        session_minutes=60,
        flags=[],
        allowed_exercise_ids=["barbell_back_squat"],
    )


def _make_plan_response(messages: list, info: AgentInfo) -> ModelResponse:
    tool = next(t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name)
    args = _SAMPLE_PLAN.model_dump(mode="json")
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])


async def test_a_function_model_run_writes_an_llm_calls_row_and_a_decisions_row(
    db: Database, user_id: int
) -> None:
    model_id = "anthropic:claude-opus-5"
    rendered = render_user_prompt(_sample_context())

    def agent_factory(model: object):
        return plan_generate_agent(FunctionModel(_make_plan_response, model_name=str(model)))

    outcome = await run_agent(
        agent_factory,
        model_id,
        rendered.text,
        purpose="plan_generate",
        model_name=model_id,
        prices={},
        language="en",
    )
    assert isinstance(outcome.output, Plan)
    assert outcome.record.ok is True
    assert outcome.prompt is not None
    assert outcome.prompt.template_name == "plan_generate"
    assert outcome.prompt.version == 3  # plan_generate.v3.md
    assert outcome.prompt.user_prompt == rendered.text

    decision_id = await record_decision(
        db,
        user_id=user_id,
        kind=DecisionKind.PLAN_GENERATE,
        prompt_template=outcome.prompt.template_name,
        prompt_version=str(outcome.prompt.version),
        model=outcome.record.model,
        content_version=content_version(),
        llm_input=rendered.payload,
        user_report=None,
        proposal=outcome.output.model_dump(mode="json"),
    )

    await record_llm_call(db, decision_id=decision_id, record=outcome.record)

    async with db.read() as conn:
        decision = await get_decision(conn, decision_id)
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")

    assert decision is not None
    assert decision.prompt_template == "plan_generate"
    assert decision.prompt_version == "3"
    assert decision.model == model_id
    assert decision.content_version == content_version()
    assert decision.proposal == outcome.output.model_dump(mode="json")
    # The stored llm_input equals exactly what was sent (B2).
    assert decision.llm_input == rendered.payload

    assert len(calls) == 1
    assert calls[0].decision_id == decision_id
    assert calls[0].model == model_id
    assert calls[0].purpose == "plan_generate"
    assert calls[0].ok is True
    assert calls[0].input_tokens > 0
    assert calls[0].output_tokens > 0


def _boom(messages: list, info: AgentInfo) -> ModelResponse:
    raise ModelHTTPError(status_code=503, model_name="anthropic:claude-opus-5", body="down")


async def test_a_provider_failure_becomes_a_refusal_and_is_logged_with_ok_zero(
    db: Database, user_id: int
) -> None:
    model_id = "anthropic:claude-opus-5"
    rendered = render_user_prompt(_sample_context())

    def agent_factory(model: object):
        return plan_generate_agent(FunctionModel(_boom, model_name=str(model)))

    outcome = await run_agent(
        agent_factory,
        model_id,
        rendered.text,
        purpose="plan_generate",
        model_name=model_id,
        prices={},
        language="en",
    )

    assert isinstance(outcome.output, Refusal)
    assert outcome.output.code == RefusalCode.LLM_UNAVAILABLE
    assert outcome.output.message  # non-empty, in the requested language
    assert outcome.record.ok is False
    assert outcome.record.input_tokens == 0
    assert outcome.record.output_tokens == 0
    assert outcome.record.cost_estimate_usd is None
    # Bug fix: a provider-side HTTP error, not a validation failure -> not eligible for
    # escalation.run_with_escalation's large-tier retry.
    assert outcome.record.error_cause == CAUSE_PROVIDER_ERROR
    # The prompt was already known (the factory succeeded; only the run itself failed), so
    # it's still reported.
    assert outcome.prompt is not None
    assert outcome.prompt.user_prompt == rendered.text

    call_id = await record_llm_call(db, decision_id=None, record=outcome.record)
    assert call_id > 0

    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert len(calls) == 1
    assert calls[0].ok is False
    assert calls[0].decision_id is None


async def test_an_agent_construction_failure_becomes_a_refusal_and_is_logged_with_ok_zero(
    db: Database,
) -> None:
    """B3: a `UserError` (or any other config error) raised while *building* the agent —
    not while running it — is caught the same way a provider failure is: a
    `Refusal(LLM_UNAVAILABLE)`, an `ok=0` row, no crash."""
    model_id = "anthropic:claude-opus-5"
    rendered = render_user_prompt(_sample_context())

    def broken_factory(model: object):
        raise UserError("pretend the configured model string can't be resolved")

    outcome = await run_agent(
        broken_factory,
        model_id,
        rendered.text,
        purpose="plan_generate",
        model_name=model_id,
        prices={},
        language="en",
    )

    assert isinstance(outcome.output, Refusal)
    assert outcome.output.code == RefusalCode.LLM_UNAVAILABLE
    assert outcome.record.ok is False
    assert outcome.record.error_cause == CAUSE_PROVIDER_ERROR
    # Construction itself failed, so there's genuinely no prompt-metadata to report.
    assert outcome.prompt is None

    call_id = await record_llm_call(db, decision_id=None, record=outcome.record)
    assert call_id > 0

    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, since="1970-01-01T00:00:00.000000Z")
    assert len(calls) == 1
    assert calls[0].ok is False


def _invalid_plan_response(messages: list, info: AgentInfo) -> ModelResponse:
    """A `Plan` tool call that fails pydantic validation on a *structural* field (an
    out-of-range weekday) — not on display-text length, which is now trimmed rather than
    rejected (the other half of this bug fix, `domain/models.py`)."""
    tool = next(t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name)
    args = {"name": "Bad plan", "schedule": [{"weekday": 9, "workout_key": "A"}], "workouts": []}
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])


def _low_budget_plan_agent_factory(model: object) -> BuiltAgent[PlanProposal]:
    """A bare `Agent` with pydantic-ai's own default output-retry budget (1: one attempt plus
    one retry) — deliberately *not* one of `llm/agents.py`'s factories (`plan_generate`'s own
    budget is now 3, A§8.5 rule 6 extended), so two invalid responses are enough to exhaust it
    here, matching the operator's original report exactly (two failed HTTP calls, one
    `UnexpectedModelBehavior`)."""
    agent = PydanticAgent(model, output_type=PlanProposal)
    return BuiltAgent(agent=agent, template_name="test_diagnostics", version=1)


async def test_output_validation_failure_logs_safe_diagnostics_and_no_input_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="fitme.llm.usage")

    def agent_factory(model: object) -> BuiltAgent[PlanProposal]:
        return _low_budget_plan_agent_factory(
            FunctionModel(_invalid_plan_response, model_name=str(model))
        )

    outcome = await run_agent(
        agent_factory,
        "anthropic:claude-sonnet-5",
        "revise please: свежая программа",
        purpose="plan_revise",
        model_name="anthropic:claude-sonnet-5",
        prices={},
        language="en",
    )

    assert isinstance(outcome.output, Refusal)
    assert outcome.output.code == RefusalCode.LLM_UNAVAILABLE
    assert outcome.record.ok is False
    assert outcome.record.error_cause == CAUSE_OUTPUT_VALIDATION

    failure_records = [r for r in caplog.records if r.message == "llm call failed"]
    assert len(failure_records) == 1
    record = failure_records[0]
    assert record.cause == CAUSE_OUTPUT_VALIDATION
    assert record.error_type == "UnexpectedModelBehavior"

    validation_errors = record.validation_errors
    assert validation_errors  # non-empty
    for entry in validation_errors:
        assert set(entry) == {"loc", "type", "msg"}
    assert any("weekday" in entry["loc"] for entry in validation_errors)

    # A§10: never the offending value, never the prompt/user text, never a response body.
    dumped = repr(record.__dict__)
    assert "свежая программа" not in dumped
    assert "9" not in repr(validation_errors)  # the actual bad weekday value never appears
    assert "input" not in repr(validation_errors)
    assert not hasattr(record, "body")

    # `model_message` (`exc.message`, never `.body`) is a short, fixed pydantic-ai phrase.
    assert record.model_message == "Exceeded maximum output retries (1)"
