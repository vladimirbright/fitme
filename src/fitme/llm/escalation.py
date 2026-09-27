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

Bug fix: an attempt whose `Refusal` is `run_agent`'s own `LLM_UNAVAILABLE` one (the underlying
agent run raised, `outcome.record.ok is False`) is only eligible for the large-tier escalation
when `outcome.record.error_cause == usage.CAUSE_OUTPUT_VALIDATION` — the model's structured
output kept failing our own pydantic schema through every output retry, which is exactly the
kind of thing a bigger model has a real shot at fixing. A `CAUSE_PROVIDER_ERROR` refusal
(timeout, HTTP error, rate limit, ...) is never escalated (A§8.5 rule 4: no retry on a
provider failure); a `Refusal` the model itself returned (`ok is True`) is never escalated
either — it's a real answer, not a failure. See `_one_attempt`/`_AttemptControl` below.

Every attempt's full `AgentRunOutcome` (B2: the exact `user_prompt` sent, the `template_name`/
`version` that built it, and the `llm_calls`-shaped record) is kept on `EscalationOutcome.
attempts`, so a caller (M6/M7) can write one `decisions` row per round with `llm_input` stored
verbatim, not just a final summary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
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
from fitme.llm.usage import (
    CAUSE_OUTPUT_VALIDATION,
    AgentRunOutcome,
    PriceTable,
    record_llm_call,
    run_agent,
)

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

AttemptHook = Callable[[AgentRunOutcome[T], Sequence[GuardVerdict]], Awaitable[int | None]]
"""Called once per attempt, right after the model returned and the guards ran, *before* the
attempt's `llm_calls` row is written. M6's `/plan` uses it to write the attempt's `decisions`
row (A§6.4: "every attempt is logged") and returns its id, which is then stored on the
`llm_calls` row as `decision_id`. It is awaited outside any unit of work; the hook opens its
own short transaction. Returning `None` leaves `llm_calls.decision_id` NULL."""


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
    on_attempt: AttemptHook[T] | None = None,
) -> EscalationOutcome[T]:
    """Run `agent_name` with escalation (see module docstring). `agent_factory` is one of
    `llm.agents`'s factories (e.g. `plan_revise_agent`) — a callable from a model to a
    `BuiltAgent`, so this stays agnostic to which of the two escalating agents it's driving.
    `on_attempt` (optional) is awaited once per attempt; see `AttemptHook`.
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
            on_attempt=on_attempt,
        )
        attempts.append(control.outcome)
        if control.done:
            return EscalationOutcome(output=control.outcome.output, attempts=attempts)
        if control.escalate_immediately:
            # An output-validation failure: no guard feedback to retry the normal tier with,
            # so skip straight to the one large-tier attempt instead of resending the same
            # prompt to the same tier again. `verdicts` is left as whatever the previous
            # iteration set (possibly still `None`), so a large-tier attempt after an earlier
            # guard-feedback retry still gets that feedback.
            break
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
        on_attempt=on_attempt,
    )
    attempts.append(control.outcome)
    if control.done:
        return EscalationOutcome(output=control.outcome.output, attempts=attempts)

    refusal = Refusal(code=refusal_code, message=i18n.t(f"refusal.{refusal_code.value}", language))
    return EscalationOutcome(output=refusal, attempts=attempts)


@dataclass(frozen=True, slots=True)
class _AttemptControl[T]:
    outcome: AgentRunOutcome[T]
    done: bool  # True: a final answer (success or a Refusal); False: retry
    verdicts: Sequence[GuardVerdict] | None = None
    # Bug fix: an output-validation failure (the model's structured output kept failing our
    # own pydantic schema through every output retry, `usage.CAUSE_OUTPUT_VALIDATION`) has no
    # guard feedback to hand back for a same-tier retry — there's nothing to fix "exactly
    # this", the whole output never parsed. Rather than loop the normal tier again with an
    # unchanged prompt, treat it as exhausting the normal-tier attempts and go straight to the
    # one large-tier attempt (A§8.5 rule 3), the same place a run of exhausted guard-feedback
    # retries ends up. A provider error (`usage.CAUSE_PROVIDER_ERROR`) is not eligible: it
    # still maps straight to a refusal with no retry (A§8.5 rule 4, unchanged).
    escalate_immediately: bool = False


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
    on_attempt: AttemptHook[T] | None,
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
    verdicts: Sequence[GuardVerdict] = ()
    if not isinstance(outcome.output, Refusal):
        verdicts = guard_check(outcome.output)

    decision_id = None if on_attempt is None else await on_attempt(outcome, verdicts)
    await record_llm_call(db, decision_id=decision_id, record=outcome.record)

    if isinstance(outcome.output, Refusal):
        if not outcome.record.ok and outcome.record.error_cause == CAUSE_OUTPUT_VALIDATION:
            return _AttemptControl(outcome=outcome, done=False, escalate_immediately=True)
        return _AttemptControl(outcome=outcome, done=True)
    if all(verdict.ok for verdict in verdicts):
        return _AttemptControl(outcome=outcome, done=True)
    return _AttemptControl(outcome=outcome, done=False, verdicts=verdicts)
