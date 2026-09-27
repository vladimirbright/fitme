"""`fitme llm eval` business logic (A§8.5 rule 7, A§11).

A manual, real-money tool: pick a tier before spending on it for real (A§8.5 rule 7). Never
run against a real provider in tests — `run_eval`'s `model_for_agent`/`model_name_for_agent`
callbacks are exactly what let a test inject a `FunctionModel` per agent while the CLI
(`cli/llm_eval.py`) wires up real resolved model strings instead.

Scope: `plan_generate` and `plan_revise` run against the fixtures and are checked with the
real guards (`guards.plan.validate_plan`), per A§8.5 rule 7. `plan_revise` needs a plan to
revise, so for each fixture it revises whatever `plan_generate` just produced for the same
fixture — this only runs when `plan_generate` is also in the requested agent list and
succeeded. `session_adjust`, `result_parse` and `recap` take a specific in-progress workout
or free-text report as input, which a bare profile fixture doesn't carry; requesting them is
accepted (so `--agent` still recognizes every agent name) but reported as `skipped`, with a
clear reason, rather than fed a fabricated input.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fitme.catalog import load_catalog
from fitme.domain.catalog import Catalog
from fitme.domain.enums import Equipment, Location
from fitme.domain.models import Plan, Refusal
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.context import GuardContext
from fitme.guards.plan import validate_plan
from fitme.llm.agents import AGENT_FACTORIES, AgentModel
from fitme.llm.context import (
    ExerciseHistorySummary,
    FlagSummary,
    UserContext,
    build_user_context,
    render_user_prompt,
)
from fitme.llm.usage import AgentRunRecord, PriceTable, run_agent
from fitme.services.catalog import available_exercises

# Agents this eval tool can actually exercise against a bare profile fixture (see module
# docstring). The other two names in `AGENT_FACTORIES` are still valid `--agent` values; they
# just always come back `skipped`.
_SUPPORTED_AGENTS: tuple[str, ...] = ("plan_generate", "plan_revise")

Outcome = Literal["ok", "refusal", "guard_fail", "provider_error", "skipped"]


@dataclass(frozen=True, slots=True)
class EvalResult:
    fixture_name: str
    agent: str
    outcome: Outcome
    record: AgentRunRecord | None = None
    detail: str = ""


def load_fixtures(directory: Path) -> list[dict[str, Any]]:
    """Every `*.json` fixture in `directory` (`tests/fixtures/llm_eval/`), as a plain dict
    keyed the way `build_user_context`'s parameters are named (plus `name`, `flags` and
    `history`, unpacked by `_build_context`/`_guard_context` below)."""
    fixtures: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        fixtures.append(json.loads(path.read_text(encoding="utf-8")))
    return fixtures


def _flag_states(fixture: Mapping[str, Any]) -> list[ScreeningFlagState]:
    return [ScreeningFlagState.model_validate(entry) for entry in fixture["flags"]]


def _build_context(fixture: Mapping[str, Any], catalog: Catalog) -> UserContext:
    location = Location(fixture["location"])
    equipment = frozenset(Equipment(item) for item in fixture["equipment"])
    flag_states = _flag_states(fixture)
    allowed_ids = [
        exercise.id for exercise in available_exercises(catalog, location, equipment, flag_states)
    ]
    history = [ExerciseHistorySummary.model_validate(entry) for entry in fixture.get("history", [])]
    return build_user_context(
        user_id=fixture["user_id"],
        language=fixture["language"],
        age_bucket=fixture["age_bucket"],
        weight_bucket=fixture["weight_bucket"],
        experience=fixture["experience"],
        barbell_experience=fixture["barbell_experience"],
        preferences=fixture["preferences"],
        focus=fixture["focus"],
        location=location,
        equipment=sorted(equipment, key=lambda item: item.value),
        sessions_per_week=fixture["sessions_per_week"],
        session_minutes=fixture["session_minutes"],
        flags=[FlagSummary(flag=state.flag, value=state.value) for state in flag_states],
        allowed_exercise_ids=allowed_ids,
        history=history,
    )


def _guard_context(fixture: Mapping[str, Any], catalog: Catalog) -> GuardContext:
    return GuardContext(
        catalog=catalog,
        flags=_flag_states(fixture),
        equipment=frozenset(Equipment(item) for item in fixture["equipment"]),
        location=Location(fixture["location"]),
        sessions_per_week=fixture["sessions_per_week"],
    )


def _classify_plan_output(
    fixture_name: str,
    agent_name: str,
    output: Plan | Refusal,
    record: AgentRunRecord,
    fixture: Mapping[str, Any],
    catalog: Catalog,
) -> EvalResult:
    if not record.ok:
        return EvalResult(
            fixture_name, agent_name, "provider_error", record, "provider call failed"
        )
    if isinstance(output, Refusal):
        return EvalResult(fixture_name, agent_name, "refusal", record, output.code.value)
    verdicts = validate_plan(output, _guard_context(fixture, catalog))
    failed = [verdict for verdict in verdicts if not verdict.ok]
    if failed:
        detail = "; ".join(verdict.detail for verdict in failed[:3])
        return EvalResult(fixture_name, agent_name, "guard_fail", record, detail)
    return EvalResult(fixture_name, agent_name, "ok", record)


async def run_eval(
    *,
    agents: Sequence[str],
    fixtures_dir: Path,
    model_for_agent: Callable[[str], AgentModel],
    model_name_for_agent: Callable[[str], str],
    prices: PriceTable,
) -> list[EvalResult]:
    """Run every requested agent over every fixture in `fixtures_dir`. `model_for_agent`
    resolves the model to actually pass to `Agent.run` (a real model string in production, a
    `FunctionModel`/`TestModel` in a test); `model_name_for_agent` is the string recorded on
    each `AgentRunRecord` (so a test can assert on it without needing a real model string)."""
    catalog = load_catalog()
    fixtures = load_fixtures(fixtures_dir)
    results: list[EvalResult] = []

    for fixture in fixtures:
        fixture_name = str(fixture.get("name", "<unnamed>"))
        context = _build_context(fixture, catalog)
        plan_output: Plan | None = None

        if "plan_generate" in agents:
            rendered = render_user_prompt(context)
            outcome = await run_agent(
                AGENT_FACTORIES["plan_generate"],
                model_for_agent("plan_generate"),
                rendered.text,
                purpose="plan_generate",
                model_name=model_name_for_agent("plan_generate"),
                prices=prices,
                language=context.language,
            )
            result = _classify_plan_output(
                fixture_name, "plan_generate", outcome.output, outcome.record, fixture, catalog
            )
            results.append(result)
            if result.outcome == "ok":
                assert isinstance(outcome.output, Plan)
                plan_output = outcome.output

        if "plan_revise" in agents:
            if plan_output is None:
                results.append(
                    EvalResult(
                        fixture_name,
                        "plan_revise",
                        "skipped",
                        detail="no plan_generate output to revise for this fixture "
                        "(include plan_generate in --agent, or run it first)",
                    )
                )
            else:
                rendered = render_user_prompt(
                    context,
                    current_plan=plan_output,
                    request="Keep everything the same; this is a no-op revision check.",
                )
                outcome = await run_agent(
                    AGENT_FACTORIES["plan_revise"],
                    model_for_agent("plan_revise"),
                    rendered.text,
                    purpose="plan_revise",
                    model_name=model_name_for_agent("plan_revise"),
                    prices=prices,
                    language=context.language,
                )
                results.append(
                    _classify_plan_output(
                        fixture_name,
                        "plan_revise",
                        outcome.output,
                        outcome.record,
                        fixture,
                        catalog,
                    )
                )

        for agent_name in agents:
            if agent_name in _SUPPORTED_AGENTS:
                continue
            results.append(
                EvalResult(
                    fixture_name,
                    agent_name,
                    "skipped",
                    detail=f"{agent_name} needs a specific in-progress workout or free-text "
                    "report as input, which a profile fixture doesn't carry; not evaluated",
                )
            )

    return results


def summarize(results: Sequence[EvalResult]) -> str:
    """A human-readable report: guard pass rate, refusal rate, tokens and estimated cost, per
    agent (A§8.5 rule 7)."""
    by_agent: dict[str, list[EvalResult]] = {}
    for result in results:
        by_agent.setdefault(result.agent, []).append(result)

    lines: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0
    cost_known = True

    for agent_name in sorted(by_agent):
        items = by_agent[agent_name]
        skipped = [item for item in items if item.outcome == "skipped"]
        runnable = [item for item in items if item.outcome != "skipped"]
        if not runnable:
            lines.append(f"{agent_name}: {len(skipped)} skipped ({skipped[0].detail})")
            continue

        refusals = sum(1 for item in runnable if item.outcome == "refusal")
        provider_errors = sum(1 for item in runnable if item.outcome == "provider_error")
        guard_checked = [item for item in runnable if item.outcome in ("ok", "guard_fail")]
        guard_passes = sum(1 for item in guard_checked if item.outcome == "ok")

        line = (
            f"{agent_name}: {len(runnable)} run(s), "
            f"{refusals} refusal(s), {provider_errors} provider error(s)"
        )
        if guard_checked:
            pass_rate = 100 * guard_passes / len(guard_checked)
            line += f", guard pass rate {guard_passes}/{len(guard_checked)} ({pass_rate:.0f}%)"
        lines.append(line)

        for item in runnable:
            if item.record is None:
                continue
            total_input_tokens += item.record.input_tokens
            total_output_tokens += item.record.output_tokens
            if item.record.cost_estimate_usd is None:
                cost_known = False
            else:
                total_cost_usd += item.record.cost_estimate_usd

    lines.append(f"tokens: {total_input_tokens} in / {total_output_tokens} out")
    lines.append(
        f"estimated cost: ${total_cost_usd:.4f}"
        if cost_known
        else "estimated cost: unknown (a model in this run has no FITME_PRICES_FILE entry)"
    )
    return "\n".join(lines)
