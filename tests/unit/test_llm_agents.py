"""`llm/agents.py`: one factory per agent, each returning a `BuiltAgent` (a typed `Agent` plus
the prompt `template_name`/`version` it was built from, B2) with instrumentation left off
(AGENTS.md §1/§5: no telemetry that leaves the operator's own infrastructure)."""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from fitme.domain.models import Plan, PlanProposal, SessionAdjustProposal
from fitme.domain.results import ParsedResults, Recap
from fitme.llm.agents import (
    AGENT_FACTORIES,
    plan_generate_agent,
    plan_revise_agent,
    recap_agent,
    result_parse_agent,
    session_adjust_agent,
)
from fitme.llm.models import output_retries_for
from fitme.llm.prompts import render_prompt


def test_every_factory_is_registered_in_agent_factories() -> None:
    assert set(AGENT_FACTORIES) == {
        "plan_generate",
        "plan_revise",
        "session_adjust",
        "result_parse",
        "recap",
        "plan_import",
    }


def test_every_factory_builds_an_agent_from_a_test_model() -> None:
    for factory in AGENT_FACTORIES.values():
        built = factory(TestModel())
        assert isinstance(built.agent, Agent)


def test_every_factory_reports_its_own_prompt_version() -> None:
    for name, factory in AGENT_FACTORIES.items():
        built = factory(TestModel())
        assert built.template_name == name
        assert built.version == render_prompt(name).version


def test_every_factory_leaves_instrumentation_off() -> None:
    """`agent.instrument` is `None` unless a caller explicitly sets it (via the property) or
    calls `Agent.instrument_all()` — neither ever happens in `llm/agents.py`. This is the
    closest the pydantic-ai API gets to an assertable "instrumentation disabled" signal."""
    for name, factory in AGENT_FACTORIES.items():
        built = factory(TestModel())
        assert built.agent.instrument is None, name


def test_plan_generate_agent_output_type_is_plan_proposal() -> None:
    built = plan_generate_agent(TestModel())
    assert built.agent.output_type is PlanProposal


def test_plan_revise_agent_output_type_is_plan_proposal() -> None:
    built = plan_revise_agent(TestModel())
    assert built.agent.output_type is PlanProposal


def test_session_adjust_agent_output_type_is_session_adjust_proposal() -> None:
    built = session_adjust_agent(TestModel())
    assert built.agent.output_type is SessionAdjustProposal


def test_result_parse_agent_output_type_is_parsed_results() -> None:
    built = result_parse_agent(TestModel())
    assert built.agent.output_type is ParsedResults


def test_recap_agent_output_type_is_recap() -> None:
    built = recap_agent(TestModel())
    assert built.agent.output_type is Recap


async def test_each_agent_sends_its_own_versioned_prompt_as_instructions() -> None:
    """The prompt file's text reaches the model as `ModelRequest.instructions` (pydantic-ai's
    own field for a rendered instructions string) on every request, matching `llm/agents.py`'s
    own docstring claim that the prompt is fixed, request-independent behavior."""
    import contextlib

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    captured: dict[str, str | None] = {}

    def capture(messages: list[ModelRequest], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        captured["instructions"] = request.instructions
        return ModelResponse(parts=[TextPart(content="ok")])

    for name, factory in AGENT_FACTORIES.items():
        # A bare TextPart reply never satisfies any of these agents' structured output
        # schemas, so every run ends in a (expected, ignored) validation failure — the
        # instructions are captured before that, on every request pydantic-ai actually sends.
        built = factory(FunctionModel(capture))
        with contextlib.suppress(Exception):
            await built.agent.run("hello")
        expected = render_prompt(name).text
        instructions = captured["instructions"]
        assert instructions is not None
        assert instructions.strip() == expected.strip(), name


def test_every_factory_wires_the_configured_output_retry_budget() -> None:
    """Bug fix: `_built` passes `retries={"output": output_retries_for(name)}` to `Agent(...)`
    rather than leaving pydantic-ai's own default (1) in place."""
    for name, factory in AGENT_FACTORIES.items():
        built = factory(TestModel())
        assert built.agent._max_output_retries == output_retries_for(name)


async def test_plan_revise_agent_recovers_from_two_invalid_attempts_within_its_budget() -> None:
    """The exact bug fixed here: pydantic-ai's own default output-retry budget (1) let two
    invalid structured-output attempts in a row exhaust it (`UnexpectedModelBehavior`,
    `error_type=UnexpectedModelBehavior` in the operator's log). `plan_revise`'s budget is 3
    (`llm/models.py::AGENT_OUTPUT_RETRIES`), well clear of two failures, so a third, valid
    attempt still succeeds."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    calls = {"n": 0}
    valid_plan = Plan(name="Plan", schedule=[], workouts=[])

    def flaky(messages: list, info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        tool = next(
            t.name for t in info.output_tools if "Plan" in t.name and "Refusal" not in t.name
        )
        if calls["n"] <= 2:
            # A structural validation failure unrelated to display-text length (which is now
            # trimmed, not rejected) — an out-of-range weekday.
            args: dict[str, object] = {
                "name": "ok",
                "schedule": [{"weekday": 9, "workout_key": "A"}],
                "workouts": [],
            }
        else:
            args = valid_plan.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    built = plan_revise_agent(FunctionModel(flaky))
    result = await built.agent.run("revise please")
    assert result.output == valid_plan
    assert calls["n"] == 3
