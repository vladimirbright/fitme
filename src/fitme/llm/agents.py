"""One pydantic-ai `Agent` factory per LLM job (A§8.1). Each factory takes a `model` (a
resolved model string in production, `pydantic_ai.models.test.TestModel` or
`pydantic_ai.models.function.FunctionModel` in tests) and returns a `BuiltAgent`: the typed
`Agent` plus the `template_name`/`version` of the prompt file it was actually built from, so
`llm/usage.py::run_agent` can log exactly what was sent without re-rendering the prompt to
find out (B2). Nothing here is tied to one provider or needs network access to construct or
test.

Telemetry stays off (AGENTS.md §1/§5): nothing here calls `logfire.configure`, sets
`instrument=True`, or touches `Agent.instrument_all()`. `Agent.__init__` has no `instrument`
parameter to begin with — instrumentation is opt-in only via the `agent.instrument` property
or the `Agent.instrument_all()` classmethod, both left untouched, so every agent built here
has `agent.instrument is None` (verified by a test).

Each agent's system-level behavior comes from its versioned prompt file
(`llm/prompts.py`), loaded once per factory call and passed as `instructions` — fixed rules
that don't vary per request. The per-request pseudonymized context (`llm/context.py`) is sent
separately, as the `user_prompt` at `agent.run(...)` time (see `llm/usage.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models import Model

from fitme.domain.models import PlanImportProposal, PlanProposal, SessionAdjustProposal
from fitme.domain.results import ParsedResults, Recap
from fitme.llm.models import model_settings_for, output_retries_for
from fitme.llm.prompts import render_prompt

AgentModel = Model | str
"""What a factory accepts for `model`: a resolved `"provider:model"` string in production, or
a pydantic-ai test double (`TestModel`/`FunctionModel`, both `Model` subclasses) in tests. Not
`Agent.__init__`'s own broader `Model | KnownModelName | str` — `KnownModelName` is a large
`Literal[...]` of known model id strings that a resolved model string will always also match
as plain `str`, so it adds nothing here, only surface area."""


@dataclass(frozen=True, slots=True)
class BuiltAgent[T]:
    """A constructed `Agent` plus the prompt metadata (B2) that went into it — the single
    source of truth `llm/usage.py::run_agent` records as `PromptInfo.template_name`/
    `.version`, instead of a caller re-rendering the prompt file a second time to find out."""

    agent: Agent[None, T]
    template_name: str
    version: int


def _built(agent_name: str, model: AgentModel, output_type: Any) -> BuiltAgent[Any]:
    rendered = render_prompt(agent_name)
    agent = Agent(
        model,
        output_type=output_type,
        instructions=rendered.text,
        model_settings=model_settings_for(agent_name),
        # Output-validation retry budget (bug fix): pydantic-ai's own default is 1 (one retry
        # after the first attempt), which two over-long display-text fields in a row can
        # exhaust. `retries` takes an `int` (same budget for both `tools`/`output`) or an
        # `AgentRetries` dict to set them separately — none of these agents register any
        # `@agent.tool`, so only `output` matters here; `tools` is left at its default.
        retries={"output": output_retries_for(agent_name)},
    )
    return BuiltAgent(agent=agent, template_name=rendered.template_name, version=rendered.version)


# mypy (unlike Pyright — see pydantic_ai/agent/__init__.py's own comment on Agent.__init__'s
# overload pair) can't resolve Agent.__init__'s overloads when `output_type` is itself a
# union type (`PlanProposal`/`SessionAdjustProposal`, both `X | Refusal`): it reports a
# call-overload failure and, following from that, a spurious no-any-return. This is a known
# mypy/pydantic-ai limitation with union output types, not a real type error — every one of
# these factories is exercised at runtime by `tests/unit/test_llm_agents.py`. Routing every
# factory through the untyped `_built()` helper above (rather than calling `Agent(...)`
# directly in each) keeps the `# type: ignore` in one place instead of five.


def plan_generate_agent(model: AgentModel) -> BuiltAgent[PlanProposal]:
    """A§8.1: pseudonymized context in, `PlanProposal` (`Plan | Refusal`) out. Default tier:
    large (`llm/models.py`)."""
    return _built("plan_generate", model, PlanProposal)


def plan_revise_agent(model: AgentModel) -> BuiltAgent[PlanProposal]:
    """A§8.1: context + current plan + user request in, `PlanProposal` out. Default tier:
    medium."""
    return _built("plan_revise", model, PlanProposal)


def session_adjust_agent(model: AgentModel) -> BuiltAgent[SessionAdjustProposal]:
    """A§8.1: context + today's workout + user request in, `SessionAdjustProposal`
    (`Workout | Refusal`) out. Default tier: medium."""
    return _built("session_adjust", model, SessionAdjustProposal)


def result_parse_agent(model: AgentModel) -> BuiltAgent[ParsedResults]:
    """A§8.1: planned block + user text in, `ParsedResults` out. Default tier: small. Never
    escalates (A§8.5 rule 3) — callers don't route this one through `llm/escalation.py`."""
    return _built("result_parse", model, ParsedResults)


def recap_agent(model: AgentModel) -> BuiltAgent[Recap]:
    """A§8.1: planned vs actual + engine decisions in, `Recap` out. Default tier: small."""
    return _built("recap", model, Recap)


def plan_import_agent(model: AgentModel) -> BuiltAgent[PlanImportProposal]:
    """M8b: context + the user's pasted program (`imported_text`) in, `PlanImportProposal`
    (`PlanImport{plan, unmatched} | Refusal`) out. Transcribes, never designs. Default tier:
    medium; routed through `llm/escalation.py` like `plan_revise`."""
    return _built("plan_import", model, PlanImportProposal)


# Every factory above, keyed by agent name — the single source of truth `llm/escalation.py`
# and `cli`'s `llm eval` use to go from an agent name (a plain string, e.g. from
# `--agent plan_revise`) to the right factory, instead of each maintaining their own copy of
# this mapping.
AGENT_FACTORIES: dict[str, Callable[[AgentModel], BuiltAgent[Any]]] = {
    "plan_generate": plan_generate_agent,
    "plan_revise": plan_revise_agent,
    "session_adjust": session_adjust_agent,
    "result_parse": result_parse_agent,
    "recap": recap_agent,
    "plan_import": plan_import_agent,
}
