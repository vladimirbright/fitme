"""Write-side access for `decisions`, `decision_outcomes` and `llm_calls` (A§4.3, AGENTS.md
§6).

`decisions` and `decision_outcomes` are append-only (A§4.6 rule 5): insert functions only,
and the schema also RAISE(ABORT)s on UPDATE (0001_init.sql). Timestamps default to
`clock.utc_now()` internally (A§4.2).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import aiosqlite

from fitme import clock
from fitme.domain.models import LoadChange


async def insert_decision(
    conn: aiosqlite.Connection,
    *,
    user_id: int,
    kind: str,
    prompt_template: str | None,
    prompt_version: str | None,
    model: str | None,
    content_version: str,
    llm_input: dict[str, object] | None,
    user_report: dict[str, object] | None,
    proposal: dict[str, object] | None,
    guards_fired: list[dict[str, object]],
    load_changes: Sequence[LoadChange] = (),
) -> int:
    """`load_changes` (A§4.3, B5) is every load change this decision applied, of any kind;
    `progression.check_weekly_increment`'s `increases_7d` sums the positive deltas across
    every decision's `load_changes` for an exercise (`selectors.decisions.
    recent_increase_deltas`), not from `set_logs` (A§9.4)."""
    cursor = await conn.execute(
        "INSERT INTO decisions (user_id, kind, prompt_template, prompt_version, model, "
        "content_version, llm_input, user_report, proposal, load_changes, guards_fired, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            kind,
            prompt_template,
            prompt_version,
            model,
            content_version,
            None if llm_input is None else json.dumps(llm_input),
            None if user_report is None else json.dumps(user_report),
            None if proposal is None else json.dumps(proposal),
            json.dumps([change.model_dump() for change in load_changes]),
            json.dumps(guards_fired),
            clock.utc_now(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def insert_decision_outcome(
    conn: aiosqlite.Connection, *, decision_id: int, outcome: dict[str, object]
) -> int:
    cursor = await conn.execute(
        "INSERT INTO decision_outcomes (decision_id, outcome, created_at) VALUES (?, ?, ?)",
        (decision_id, json.dumps(outcome), clock.utc_now()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def insert_llm_call(
    conn: aiosqlite.Connection,
    *,
    decision_id: int | None,
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_estimate_usd: float | None,
    latency_ms: int,
    ok: bool,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO llm_calls (decision_id, purpose, model, input_tokens, output_tokens, "
        "cost_estimate_usd, latency_ms, ok, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            purpose,
            model,
            input_tokens,
            output_tokens,
            cost_estimate_usd,
            latency_ms,
            int(ok),
            clock.utc_now(),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
