"""controllers/selectors for `decisions`, `decision_outcomes` and `llm_calls` (A§4.3,
AGENTS.md §6)."""

from __future__ import annotations

import sqlite3

import pytest

from fitme.db.connection import Database
from fitme.db.controllers.decisions import (
    insert_decision,
    insert_decision_outcome,
    insert_llm_call,
)
from fitme.db.selectors.decisions import (
    get_decision,
    list_decision_outcomes,
    list_decisions_for_user,
    list_llm_calls_since,
)


async def _insert_test_decision(db: Database, user_id: int) -> int:
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_generate",
            prompt_template="plan_generate",
            prompt_version="v1",
            model="anthropic:claude-opus-5",
            content_version="abc123def456",
            llm_input={"user": user_id, "buckets": ["30_39"]},
            user_report=None,
            proposal={"name": "Plan A"},
            guards_fired=[{"rule": "ceiling", "ok": True, "detail": "no history"}],
        )


async def test_insert_and_get_decision(db: Database, user_id: int) -> None:
    decision_id = await _insert_test_decision(db, user_id)

    async with db.read() as conn:
        record = await get_decision(conn, decision_id)
    assert record is not None
    assert record.kind == "plan_generate"
    assert record.llm_input == {"user": user_id, "buckets": ["30_39"]}
    assert record.guards_fired == [{"rule": "ceiling", "ok": True, "detail": "no history"}]

    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    assert decisions == [record]


async def test_decision_outcome_is_append_only_by_construction(db: Database, user_id: int) -> None:
    decision_id = await _insert_test_decision(db, user_id)

    async with db.transaction() as conn:
        await insert_decision_outcome(conn, decision_id=decision_id, outcome={"applied": True})

    async with db.read() as conn:
        outcomes = await list_decision_outcomes(conn, decision_id)
    assert len(outcomes) == 1
    assert outcomes[0].outcome == {"applied": True}


async def test_decisions_table_rejects_update(db: Database, user_id: int) -> None:
    """0001_init.sql's BEFORE UPDATE trigger makes append-only a DB invariant, not just a
    controller-layer convention."""
    decision_id = await _insert_test_decision(db, user_id)

    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute("UPDATE decisions SET kind = 'refusal' WHERE id = ?", (decision_id,))


async def test_decision_outcomes_table_rejects_update(db: Database, user_id: int) -> None:
    decision_id = await _insert_test_decision(db, user_id)
    async with db.transaction() as conn:
        outcome_id = await insert_decision_outcome(
            conn, decision_id=decision_id, outcome={"applied": True}
        )

    with pytest.raises(sqlite3.IntegrityError):
        await db.raw.execute(
            "UPDATE decision_outcomes SET outcome = '{}' WHERE id = ?", (outcome_id,)
        )


async def test_llm_call_recording_and_listing_since(db: Database, user_id: int) -> None:
    decision_id = await _insert_test_decision(db, user_id)

    async with db.transaction() as conn:
        await insert_llm_call(
            conn,
            decision_id=decision_id,
            purpose="plan_generate",
            model="anthropic:claude-opus-5",
            input_tokens=100,
            output_tokens=200,
            cost_estimate_usd=0.01,
            latency_ms=500,
            ok=True,
        )

    async with db.read() as conn:
        calls = await list_llm_calls_since(conn, "1970-01-01T00:00:00.000000Z")
    assert len(calls) == 1
    assert calls[0].ok is True
    assert calls[0].decision_id == decision_id
