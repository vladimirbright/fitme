"""Structured plan editing (A§9.1 `GET/POST /app/plans/{id}/edit`), website-only.

A user's own manual edit to a plan's sets/reps/loads/exercise choice is saved as a new
`plan_versions` row with `origin='user_edit'` and a `decision(kind=user_edit)` carrying
`load_changes` (AGENTS.md §6, A§4.3).

**What's overridable and what isn't (A§9.1, M9 review fix B5).** Two guard failures are
*warnings*: the weekly cap (`progression.weekly_cap`) and the historical-max ceiling
(`ceiling.historical_max`). An explicit user edit is deliberately allowed to cross these, with
a confirmation checkbox, because it is the user's own program, not an automated proposal — the
load engine and the LLM agents still can't cross them; a person editing their own plan by hand
can, same as they could write whatever they want on paper. Everything else blocks the save
outright, checkbox or not:

- the check-in gate (`checkins.increase_allowed`): an unknown, worse or pain answer on a
  flagged area blocks an increase for anyone, not just the automated engine;
- a kg load on a non-kg-loadable exercise (`plan.kg_loadable`) or a non-finite/garbage
  reference (`plan.reference_load`) — these are data-shape problems, not judgment calls;
- the absolute plausibility bounds (`domain.models.MAX_TOTAL_KG`/`MAX_IMPLEMENT_KG`): a kg
  value this large is a typo, never a real lift, exactly like `guards.plausibility` treats a
  parsed training result (A§6.5.1);
- every structural rule (catalog id, contraindication, equipment/location fit) — the
  exercise-swap dropdown only ever offers `services.catalog.available_exercises`, so this is
  defense in depth, not a real UI path.

An over-cap edit's `load_changes` are tagged in the decision's `user_report.
over_cap_exercises`, so `selectors.decisions.applied_to_kg_by_exercise` (A§7) knows never to
treat a user's manual over-cap edit as a new, higher baseline the guards build on — it still
counts towards the trailing-7-day `increases_7d` total the weekly cap reads.

`save_edit` runs the same gate as `/plan` (`planning.gate`) first: an open health hold or
incomplete screening blocks editing at all, exactly like the bot.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

from fitme.catalog import load_catalog
from fitme.config.content import content_version
from fitme.config.settings import Settings
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import insert_plan_version
from fitme.db.selectors.plans import get_latest_plan_version
from fitme.domain.catalog import Exercise
from fitme.domain.enums import DecisionKind
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import MAX_IMPLEMENT_KG, MAX_TOTAL_KG, LoadChange, Plan
from fitme.guards.context import GuardContext
from fitme.guards.plan import prescription_verdicts
from fitme.services import planning
from fitme.services.catalog import available_exercises

# A§9.1/B5: only these two load-rule failures are overridable warnings. Every other guard
# failure (structural, check-in gate, kg_loadable, a garbage reference) blocks unconditionally
# — see the module docstring.
_OVERRIDABLE_LOAD_RULES = frozenset({"ceiling.historical_max", "progression.weekly_cap"})


def load_cap_warnings(plan: Plan, ctx: GuardContext) -> dict[str, list[str]]:
    """`{exercise_id: [detail, ...]}` for every prescription whose load fails an *overridable*
    load rule (the weekly cap or the ceiling) — shown as a warning on the edit form, with a
    confirm checkbox. Every other failure is reported by `blocking_errors` instead, and always
    blocks the save."""
    warnings: dict[str, list[str]] = {}
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                failures = [
                    verdict.detail
                    for verdict in prescription_verdicts(prescription, ctx)
                    if not verdict.ok and verdict.rule in _OVERRIDABLE_LOAD_RULES
                ]
                if failures:
                    warnings.setdefault(prescription.exercise_id, []).extend(failures)
    return warnings


def _plausibility_bound_errors(plan: Plan, ctx: GuardContext) -> list[str]:
    """B5: an absolute bound on any kg prescription, exactly like `guards.plausibility`'s
    bounds on a parsed training result (A§6.5.1) — a non-finite value or one far beyond any
    real lift is a data-entry error, never a judgment call a checkbox should be able to
    override."""
    errors: list[str] = []
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                kg = prescription.load.kg
                if prescription.load.kind != "kg" or kg is None:
                    continue
                if not math.isfinite(kg) or kg <= 0:
                    errors.append(f"{prescription.exercise_id}: {kg!r} is not a finite kg value")
                    continue
                exercise = ctx.catalog.by_id(prescription.exercise_id)
                per_implement = exercise is not None and exercise.load_unit != "total"
                bound = MAX_IMPLEMENT_KG if per_implement else MAX_TOTAL_KG
                if kg > bound:
                    errors.append(
                        f"{prescription.exercise_id}: {kg:g} kg exceeds the plausible bound "
                        f"of {bound:g} kg"
                    )
    return errors


def blocking_errors(plan: Plan, ctx: GuardContext) -> list[str]:
    """Every non-overridable failure (A§9.1/B5): structural guard failures (catalog id,
    location, equipment, contraindication), the check-in gate, a kg load on a non-kg-loadable
    exercise, a garbage reference load, and the absolute plausibility bounds. These always
    block the save, warning checkbox or not."""
    errors: list[str] = []
    for workout in plan.workouts:
        for block in workout.blocks:
            for prescription in block.items:
                for verdict in prescription_verdicts(prescription, ctx):
                    if not verdict.ok and verdict.rule not in _OVERRIDABLE_LOAD_RULES:
                        errors.append(verdict.detail)
    errors.extend(_plausibility_bound_errors(plan, ctx))
    return errors


# Kept for backward compatibility with anything importing the old name.
structural_errors = blocking_errors


async def allowed_exercises_for_edit(db: Database, user_id: int) -> list[Exercise] | None:
    """The catalog exercises the edit form's swap dropdown may offer (A§9.1: "swap exercise
    from the allowed catalog list") — the same `location`/`equipment`/screening filter
    `/plan` uses. `None` if the profile isn't complete (nothing to filter by)."""
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    if snapshot.profile is None:
        return None
    return available_exercises(
        load_catalog(), snapshot.profile.location, snapshot.profile.equipment, snapshot.flags
    )


@dataclass(frozen=True, slots=True)
class PromptMeta:
    """ADR 0003: when an edit was *interpreted* by the `assistant` agent from the owner's
    message, the decision records which prompt and model did it (AGENTS.md §6)."""

    template_name: str
    version: int
    model: str
    llm_input: Mapping[str, object] | None = None


class PromptColumns(TypedDict):
    prompt_template: str | None
    prompt_version: str | None
    model: str | None
    llm_input: dict[str, object] | None


def prompt_columns(prompt: PromptMeta | None) -> PromptColumns:
    """`insert_decision`'s prompt/model columns for an edit — all `None` for a manual edit."""
    if prompt is None:
        return {"prompt_template": None, "prompt_version": None, "model": None, "llm_input": None}
    return {
        "prompt_template": prompt.template_name,
        "prompt_version": str(prompt.version),
        "model": prompt.model,
        "llm_input": None if prompt.llm_input is None else dict(prompt.llm_input),
    }


@dataclass(frozen=True, slots=True)
class SaveEditResult:
    ok: bool
    plan_id: int | None = None
    version: int | None = None
    decision_id: int | None = None
    warnings: dict[str, list[str]] | None = None
    errors: list[str] | None = None


async def save_edit(
    db: Database,
    settings: Settings,
    user_id: int,
    plan_id: int,
    edited: Plan,
    *,
    confirmed: bool,
    report_extra: Mapping[str, object] | None = None,
    prompt: PromptMeta | None = None,
) -> SaveEditResult:
    """Save `edited` as a new version of `plan_id` (A§9.1). `confirmed` is the form's explicit
    "I understand this is above the configured cap" checkbox: an overridable warning (weekly
    cap / ceiling) without it re-shows the form (`ok=False`, `warnings` set, nothing saved); a
    blocking error always refuses, checkbox or not. Runs the same gate as `/plan`
    (`planning.gate`) first, for parity with the bot: an open health hold or incomplete
    screening blocks editing entirely.

    `report_extra` is merged into the decision's `user_report` (e.g. `source`, the
    assistant's ops); `prompt` fills the decision's prompt/model columns (ADR 0003).
    """
    catalog = load_catalog()
    async with db.read() as conn:
        snapshot = await planning.read_snapshot(conn, user_id)
    if snapshot.profile is None:
        return SaveEditResult(ok=False, errors=["profile is incomplete"])

    gate_failure = planning.gate(snapshot)
    if gate_failure is not None:
        _code, verdict = gate_failure
        return SaveEditResult(ok=False, errors=[verdict.detail])

    inputs = planning.build_inputs(catalog, snapshot, settings, user_id)

    errors = blocking_errors(edited, inputs.ctx)
    if errors:
        return SaveEditResult(ok=False, errors=errors)

    warnings = load_cap_warnings(edited, inputs.ctx)
    if warnings and not confirmed:
        return SaveEditResult(ok=False, warnings=warnings)

    load_changes: list[LoadChange] = planning.load_changes_for(edited, inputs.ctx)
    fired: list[GuardVerdict] = []
    for detail_list in warnings.values():
        fired.extend(
            GuardVerdict(rule="plan_edit.over_cap", ok=True, detail=detail)
            for detail in detail_list
        )

    async with db.transaction() as conn:
        latest = await get_latest_plan_version(conn, plan_id)
        version = 1 if latest is None else latest.version + 1
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind=DecisionKind.USER_EDIT.value,
            **prompt_columns(prompt),
            content_version=content_version(),
            user_report={
                **(report_extra or {}),
                "plan_id": plan_id,
                "confirmed_over_cap": confirmed,
                # B5: these exercises' load_changes must never lift the reference later
                # (selectors.decisions.applied_to_kg_by_exercise) — they still count towards
                # increases_7d (the weekly cap), which is unaffected by this tag.
                "over_cap_exercises": sorted(warnings) if warnings else [],
            },
            proposal={"plan": edited.model_dump(mode="json")},
            guards_fired=[verdict.model_dump() for verdict in fired],
            load_changes=load_changes,
        )
        await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=version,
            body=edited.model_dump(mode="json"),
            origin="user_edit",
            decision_id=decision_id,
        )
    return SaveEditResult(ok=True, plan_id=plan_id, version=version, decision_id=decision_id)


__all__ = [
    "PromptMeta",
    "SaveEditResult",
    "prompt_columns",
    "allowed_exercises_for_edit",
    "blocking_errors",
    "load_cap_warnings",
    "save_edit",
    "structural_errors",
]
