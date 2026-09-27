"""Wraps every agent run with latency measurement, cost estimation and `llm_calls`
accounting (A§4.3, A§8.1, A§8.5 rule 5).

`run_agent` builds the agent (via a factory, so construction failures are caught too — B3)
and runs it, returning an `AgentRunOutcome`: the parsed output, a plain DB-shaped
`AgentRunRecord`, and (B2) the exact `PromptInfo` — `template_name`/`version`/the literal
`user_prompt` string sent — read straight off the `BuiltAgent` the factory returned, never
re-rendered. `run_agent` never touches the database itself. `record_llm_call` writes the
record in its own short `db.transaction()`, called **after** `run_agent` returns (A§4.6 rule
4: "never await an LLM ... inside a unit of work" — the two are kept as separate calls
specifically so a caller can't accidentally nest them).

Provider errors, timeouts and provider-side refusals (A§8.5 rule 4), **and** a configuration
error raised while building the agent itself (B3: a bad model string or other misconfiguration
that `pydantic_ai.exceptions.UserError` reports) are all caught here and turned into
`Refusal(code=LLM_UNAVAILABLE)`, logged with `ok=0`. This never falls back to a previous plan
or an unguarded default (A§8.5 rule 4): the caller gets a refusal, nothing else.
"""

from __future__ import annotations

import logging
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.exceptions import AgentRunError, UserError

from fitme import i18n
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_llm_call
from fitme.domain.enums import RefusalCode
from fitme.domain.models import Refusal
from fitme.llm.agents import AgentModel, BuiltAgent

_logger = logging.getLogger(__name__)

_TOKENS_PER_MILLION = 1_000_000

# Config-time failures building the agent (a bad/unknown model string reaching this point
# despite startup validation, a missing provider dependency, ...) alongside run-time provider
# failures (timeouts, HTTP errors, exhausted retries) — both become a refusal, never a crash
# or a silent fallback (A§8.5 rule 4, B3).
_AGENT_FAILURE_EXCEPTIONS = (AgentRunError, UserError)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens, in and out (`FITME_PRICES_FILE`)."""

    input_per_million_usd: float
    output_per_million_usd: float


PriceTable = Mapping[str, ModelPrice]


def load_price_table(path: Path | None) -> PriceTable:
    """Load `FITME_PRICES_FILE` (TOML, `[models."<resolved model string>"]` sections). A
    missing/unset file, or a model with no entry, means "cost unknown" (`estimate_cost_usd`
    returns `None`) rather than a startup failure — cost estimation is a `/system` nicety,
    not a safety guard. Logs a warning either way, per A§8.5 rule 5."""
    if path is None:
        _logger.warning("FITME_PRICES_FILE is not set; LLM cost estimates will be unavailable")
        return {}
    if not path.exists():
        _logger.warning(
            "FITME_PRICES_FILE %s does not exist; cost estimates will be unavailable", path
        )
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw_models = data.get("models", {})
    table: dict[str, ModelPrice] = {}
    for model_name, entry in raw_models.items():
        try:
            table[model_name] = ModelPrice(
                input_per_million_usd=float(entry["input_per_million_usd"]),
                output_per_million_usd=float(entry["output_per_million_usd"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _logger.warning(
                "skipping malformed price entry for %r in %s: %s", model_name, path, exc
            )
    return table


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int, prices: PriceTable
) -> float | None:
    """`None` (plus a warning) if `model` has no entry in `prices` (A§8.5 rule 5: "/system
    shows 'cost unknown' for that model, and startup logs a warning" — this is the per-call
    half of that; `load_price_table` above covers the file-level half)."""
    price = prices.get(model)
    if price is None:
        _logger.warning("no price entry for model %r; cost estimate unavailable", model)
        return None
    return (input_tokens / _TOKENS_PER_MILLION) * price.input_per_million_usd + (
        output_tokens / _TOKENS_PER_MILLION
    ) * price.output_per_million_usd


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    """Everything `record_llm_call` needs to write one `llm_calls` row. No prompt content
    (A§4.3: "No prompt content here"; that lives in `PromptInfo`/`decisions.llm_input`
    instead). `ok=False` on a caught agent-construction or provider failure;
    `input_tokens`/`output_tokens`/`cost_estimate_usd` are then 0/0/`None` (there is no usage
    to report)."""

    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_estimate_usd: float | None
    latency_ms: int
    ok: bool


@dataclass(frozen=True, slots=True)
class PromptInfo:
    """B2 "one rendering path": exactly what was sent and which versioned prompt file built
    the agent that received it — read off the `BuiltAgent` the factory returned, never
    re-rendered. `None` on `AgentRunOutcome.prompt` only in the rare case where agent
    *construction itself* failed (B3), before any prompt info could be read at all."""

    template_name: str
    version: int
    user_prompt: str


@dataclass(frozen=True, slots=True)
class AgentRunOutcome[T]:
    """One `run_agent` call's full result: the parsed output (a `Refusal` on any failure,
    even for an agent whose own output type has no `Refusal` member — `ParsedResults`,
    `Recap`), the `llm_calls`-shaped `record`, and the `prompt` actually sent (B2), for a
    caller to store verbatim as `decisions.llm_input`."""

    output: T | Refusal
    record: AgentRunRecord
    prompt: PromptInfo | None


async def run_agent[T](
    agent_factory: Callable[[AgentModel], BuiltAgent[T]],
    model: AgentModel,
    user_prompt: str,
    *,
    purpose: str,
    model_name: str,
    prices: PriceTable,
    language: str,
) -> AgentRunOutcome[T]:
    """Build the agent (via `agent_factory(model)`) and run it once, measuring both. `model`
    is what's actually passed to the factory (a resolved model string in production, a test
    double in tests); `model_name` is the string recorded on `AgentRunRecord`/logged with
    `PromptInfo` — usually `str(model)`, kept separate so a test can label a `FunctionModel`
    run with a friendly name. Does **not** write to the database; call `record_llm_call` with
    the returned `record` afterwards.
    """
    built: BuiltAgent[T] | None = None
    start = time.monotonic()
    try:
        built = agent_factory(model)
        result = await built.agent.run(user_prompt)
    except _AGENT_FAILURE_EXCEPTIONS as exc:
        latency_ms = round((time.monotonic() - start) * 1000)
        _logger.warning(
            "llm call failed",
            extra={"purpose": purpose, "model": model_name, "error_type": type(exc).__name__},
        )
        record = AgentRunRecord(
            purpose=purpose,
            model=model_name,
            input_tokens=0,
            output_tokens=0,
            cost_estimate_usd=None,
            latency_ms=latency_ms,
            ok=False,
        )
        refusal = Refusal(
            code=RefusalCode.LLM_UNAVAILABLE,
            message=i18n.t("refusal.llm_unavailable", language),
        )
        prompt_info = (
            PromptInfo(
                template_name=built.template_name, version=built.version, user_prompt=user_prompt
            )
            if built is not None
            else None
        )
        return AgentRunOutcome(output=refusal, record=record, prompt=prompt_info)

    latency_ms = round((time.monotonic() - start) * 1000)
    usage = result.usage
    cost = estimate_cost_usd(model_name, usage.input_tokens, usage.output_tokens, prices)
    record = AgentRunRecord(
        purpose=purpose,
        model=model_name,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_estimate_usd=cost,
        latency_ms=latency_ms,
        ok=True,
    )
    prompt_info = PromptInfo(
        template_name=built.template_name, version=built.version, user_prompt=user_prompt
    )
    return AgentRunOutcome(output=result.output, record=record, prompt=prompt_info)


async def record_llm_call(db: Database, *, decision_id: int | None, record: AgentRunRecord) -> int:
    """Write one `llm_calls` row, in its own short transaction, **after** the call it
    describes has already returned (A§4.6 rule 4) — never call this from inside a unit of
    work that also awaits an LLM."""
    async with db.transaction() as conn:
        return await insert_llm_call(
            conn,
            decision_id=decision_id,
            purpose=record.purpose,
            model=record.model,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost_estimate_usd=record.cost_estimate_usd,
            latency_ms=record.latency_ms,
            ok=record.ok,
        )
