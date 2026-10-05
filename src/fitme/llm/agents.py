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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models import Model

from fitme.domain.assistant import AssistantProposal
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

    agent: Agent[Any, T]
    template_name: str
    version: int


_NONE_TYPE: type[None] = type(None)


def _built(
    agent_name: str,
    model: AgentModel,
    output_type: Any,
    *,
    deps_type: type[Any] = _NONE_TYPE,
    tools: Sequence[Tool[Any]] = (),
) -> BuiltAgent[Any]:
    rendered = render_prompt(agent_name)
    agent = Agent(
        model,
        output_type=output_type,
        instructions=rendered.text,
        deps_type=deps_type,
        tools=tools,
        model_settings=model_settings_for(agent_name),
        # Output-validation retry budget (bug fix): pydantic-ai's own default is 1 (one retry
        # after the first attempt), which two over-long display-text fields in a row can
        # exhaust. `retries` takes an `int` (same budget for both `tools`/`output`) or an
        # `AgentRetries` dict to set them separately — only `assistant` registers tools (all
        # read-only, see `AssistantTools`); `tools` is left at its default.
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


class AssistantTools(Protocol):
    """The read-only lookups the `assistant` agent may call (ADR 0003), implemented in
    `services.assistant` over one user's data. Every method returns JSON-safe, pseudonymized
    data (AGENTS.md §5: catalog ids, numbers, DB row ids, scrubbed plan names — never a
    Telegram id or a person's name). There is deliberately **no** write method: the agent's
    edits come back as its typed output and are applied, after the guards, by deterministic
    code."""

    async def list_plans(self) -> list[dict[str, object]]: ...

    async def get_plan(self, plan_id: int) -> dict[str, object]: ...

    async def list_recent_sessions(self, limit: int) -> list[dict[str, object]]: ...

    async def get_session(self, session_id: int) -> dict[str, object]: ...

    async def find_exercises(self, query: str) -> list[dict[str, object]]: ...


_NO_DATA = {"error": "no data available in this run"}


async def _list_plans(ctx: RunContext[AssistantTools | None]) -> object:
    """Every plan of this user: id, name, whether it is the default, and its workouts'
    keys and titles. Call this first when the user doesn't say which plan they mean."""
    return _NO_DATA if ctx.deps is None else await ctx.deps.list_plans()


async def _get_plan(ctx: RunContext[AssistantTools | None], plan_id: int) -> object:
    """One plan's full current version: schedule and every workout's prescriptions."""
    return _NO_DATA if ctx.deps is None else await ctx.deps.get_plan(plan_id)


async def _list_recent_sessions(ctx: RunContext[AssistantTools | None], limit: int = 5) -> object:
    """The most recent workout sessions (newest first, at most 10): id, date, plan, workout
    key and status."""
    if ctx.deps is None:
        return _NO_DATA
    return await ctx.deps.list_recent_sessions(max(1, min(limit, 10)))


async def _get_session(ctx: RunContext[AssistantTools | None], session_id: int) -> object:
    """One session's logged sets per exercise: set number, planned and actual reps/kg."""
    return _NO_DATA if ctx.deps is None else await ctx.deps.get_session(session_id)


async def _find_exercises(ctx: RunContext[AssistantTools | None], query: str) -> object:
    """Allowed catalog exercises whose id or name contains `query` (any language). Use it
    to map an exercise the user names to an allowed `exercise_id`."""
    return _NO_DATA if ctx.deps is None else await ctx.deps.find_exercises(query)


_ASSISTANT_TOOLS: tuple[Tool[Any], ...] = (
    Tool(_list_plans, name="list_plans"),
    Tool(_get_plan, name="get_plan"),
    Tool(_list_recent_sessions, name="list_recent_sessions"),
    Tool(_get_session, name="get_session"),
    Tool(_find_exercises, name="find_exercises"),
)


def assistant_agent(model: AgentModel) -> BuiltAgent[AssistantProposal]:
    """ADR 0003: the owner's unprompted free text in, `AssistantProposal` (`AssistantEdits |
    AssistantAction | AssistantReply | Refusal`) out, with read-only lookup tools. Run with
    `deps=` an `AssistantTools`; without one (eval, tests) every tool answers "no data".
    Default tier: medium."""
    return _built(
        "assistant",
        model,
        AssistantProposal,
        deps_type=AssistantTools,
        tools=_ASSISTANT_TOOLS,
    )


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
    "assistant": assistant_agent,
}
