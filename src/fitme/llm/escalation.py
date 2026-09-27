"""A§8.5 rule 3: "Escalate once, then refuse." Used by `plan_revise` and `session_adjust`
only — `result_parse` never escalates (it sets `unclear=true` instead, A§8.5 rule 3's own last
sentence).

Up to three attempts: the agent's normally-resolved model (`llm/models.py::model_for`), the
same model again with the guard failures fed back, and — only if that also fails — one more
attempt on the `large` tier. Each attempt's `llm_calls` row is written immediately after it
returns (A§4.6 rule 4: never around an LLM call), before the next attempt starts. Guard logic
itself is a caller-supplied callback (`guard_check`), so this module never imports
`guards.plan` or any specific guard rule — M6/M7 plug those in when they build the real
`/plan` and `/train` flows this helper serves.

Every attempt's full `AgentRunOutcome` (B2: the exact `user_prompt` sent, the `template_name`/
`version` that built it, and the `llm_calls`-shaped record) is kept on `EscalationOutcome.
attempts`, so a caller (M6/M7) can write one `decisions` row per round with `llm_input` stored
verbatim, not just a final summary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from fitme import i18n
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.domain.enums import RefusalCode
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import Refusal
from fitme.llm.agents import AgentModel, BuiltAgent
from fitme.llm.models import model_for, resolve_tier
from fitme.llm.usage import AgentRunOutcome, PriceTable, record_llm_call, run_agent

T = TypeVar("T")

_LARGE_TIER = "large"
_MAX_NORMAL_TIER_ATTEMPTS = 2  # the first attempt, plus one guard-feedback retry

GuardCheck = Callable[[T], Sequence[GuardVerdict]]
"""Runs the real guards (e.g. `guards.plan.validate_plan`, bound to a `GuardContext` by the
caller) over a successfully-parsed agent output and returns every verdict."""

PromptBuilder = Callable[[Sequence[GuardVerdict] | None], str]
"""Builds the `user_prompt` for one attempt. Called with `None` for the first attempt, and
with the failed guard verdicts from the previous attempt for every retry — the caller decides
how to phrase that feedback into the prompt, typically via `llm.context.render_user_prompt`."""


@dataclass(frozen=True, slots=True)
class EscalationOutcome[T]:
    """`output` is the agent's own `Refusal` if it returned one, the escalation's own
    `Refusal` if every attempt failed the guards, or a validated `T` on success. `attempts`
    has one full `AgentRunOutcome` per model call actually made, in order — 1 to 3 entries."""

    output: T | Refusal
    attempts: list[AgentRunOutcome[T]] = field(default_factory=list)


async def run_with_escalation[T](
    *,
    agent_name: str,
    agent_factory: Callable[[AgentModel], BuiltAgent[T]],
    build_prompt: PromptBuilder,
    guard_check: GuardCheck[T],
    settings: Settings,
    db: Database,
    prices: PriceTable,
    language: str,
    refusal_code: RefusalCode = RefusalCode.NO_SAFE_PLAN,
) -> EscalationOutcome[T]:
    """Run `agent_name` with escalation (see module docstring). `agent_factory` is one of
    `llm.agents`'s factories (e.g. `plan_revise_agent`) — a callable from a model to a
    `BuiltAgent`, so this stays agnostic to which of the two escalating agents it's driving.
    """
    attempts: list[AgentRunOutcome[T]] = []
    verdicts: Sequence[GuardVerdict] | None = None
    normal_model = model_for(agent_name, settings).model

    for _attempt in range(_MAX_NORMAL_TIER_ATTEMPTS):
        control = await _one_attempt(
            agent_factory=agent_factory,
            model=normal_model,
            model_name=normal_model,
            purpose=agent_name,
            prompt=build_prompt(verdicts),
            guard_check=guard_check,
            db=db,
            prices=prices,
            language=language,
        )
        attempts.append(control.outcome)
        if control.done:
            return EscalationOutcome(output=control.outcome.output, attempts=attempts)
        verdicts = control.verdicts

    large_model = resolve_tier(_LARGE_TIER, settings)
    control = await _one_attempt(
        agent_factory=agent_factory,
        model=large_model,
        model_name=large_model,
        purpose=agent_name,
        prompt=build_prompt(verdicts),
        guard_check=guard_check,
        db=db,
        prices=prices,
        language=language,
    )
    attempts.append(control.outcome)
    if control.done:
        return EscalationOutcome(output=control.outcome.output, attempts=attempts)

    refusal = Refusal(code=refusal_code, message=i18n.t(f"refusal.{refusal_code.value}", language))
    return EscalationOutcome(output=refusal, attempts=attempts)


@dataclass(frozen=True, slots=True)
class _AttemptControl[T]:
    outcome: AgentRunOutcome[T]
    done: bool  # True: a final answer (success or a Refusal); False: guards failed, retry
    verdicts: Sequence[GuardVerdict] | None = None


async def _one_attempt[T](
    *,
    agent_factory: Callable[[AgentModel], BuiltAgent[T]],
    model: AgentModel,
    model_name: str,
    purpose: str,
    prompt: str,
    guard_check: GuardCheck[T],
    db: Database,
    prices: PriceTable,
    language: str,
) -> _AttemptControl[T]:
    outcome = await run_agent(
        agent_factory,
        model,
        prompt,
        purpose=purpose,
        model_name=model_name,
        prices=prices,
        language=language,
    )
    await record_llm_call(db, decision_id=None, record=outcome.record)

    if isinstance(outcome.output, Refusal):
        return _AttemptControl(outcome=outcome, done=True)

    verdicts = guard_check(outcome.output)
    if all(verdict.ok for verdict in verdicts):
        return _AttemptControl(outcome=outcome, done=True)
    return _AttemptControl(outcome=outcome, done=False, verdicts=verdicts)
