"""Write `decisions` and `decision_outcomes` (A§4.3, AGENTS.md §6).

Thin wrappers around `db.controllers.decisions`: each function opens exactly one
`db.transaction()`, so a caller never has to. These never call an LLM or run a guard
themselves — by the time a caller reaches here, the LLM has already returned and the guards
have already run (A§4.6 rule 4: never await an LLM inside a unit of work). Guard verdicts come
in as `domain.guard_types.GuardVerdict` and are serialized to plain dicts here, the same way
JSON columns are always handled at the controller/selector boundary (A§4.6 rule 6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict

from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision, insert_decision_outcome
from fitme.domain.enums import DecisionKind
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import LoadChange


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


async def record_decision(
    db: Database,
    *,
    user_id: int,
    kind: DecisionKind,
    prompt_template: str | None,
    prompt_version: str | None,
    model: str | None,
    content_version: str,
    llm_input: dict[str, object] | None,
    user_report: dict[str, object] | None,
    proposal: dict[str, object] | None,
    guards_fired: Sequence[GuardVerdict] = (),
    load_changes: Sequence[LoadChange] = (),
) -> int:
    """Insert one immutable `decisions` row (A§4.3, AGENTS.md §6) and return its id."""
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind=kind.value,
            prompt_template=prompt_template,
            prompt_version=prompt_version,
            model=model,
            content_version=content_version,
            llm_input=llm_input,
            user_report=user_report,
            proposal=proposal,
            guards_fired=[verdict.model_dump() for verdict in guards_fired],
            load_changes=load_changes,
        )


async def record_decision_outcome(
    db: Database, *, decision_id: int, outcome: dict[str, object]
) -> int:
    """Insert one immutable `decision_outcomes` row for an earlier `decisions` row (A§4.3):
    "what the user actually did", which can arrive after the decision itself was logged."""
    async with db.transaction() as conn:
        return await insert_decision_outcome(conn, decision_id=decision_id, outcome=outcome)
