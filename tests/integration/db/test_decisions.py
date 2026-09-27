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
    applied_to_kg_by_exercise,
    get_decision,
    get_latest_plan_round_decision,
    list_decision_outcomes,
    list_decisions_for_user,
    list_llm_calls_since,
    recent_increase_deltas,
    recent_increase_deltas_by_exercise,
)
from fitme.domain.models import LoadChange


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
    assert record.load_changes == []

    async with db.read() as conn:
        decisions = await list_decisions_for_user(conn, user_id)
    assert decisions == [record]


async def test_insert_decision_stores_load_changes(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        decision_id = await insert_decision(
            conn,
            user_id=user_id,
            kind="progression",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
            load_changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=102.5)],
        )

    async with db.read() as conn:
        record = await get_decision(conn, decision_id)
    assert record is not None
    assert record.load_changes == [
        {"exercise_id": "barbell_back_squat", "from_kg": 100.0, "to_kg": 102.5}
    ]


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


async def _insert_load_change_decision(
    db: Database, user_id: int, *, kind: str = "progression", changes: list[LoadChange]
) -> None:
    async with db.transaction() as conn:
        await insert_decision(
            conn,
            user_id=user_id,
            kind=kind,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
            load_changes=changes,
        )


async def test_recent_increase_deltas_filters_by_user_exercise_and_window(
    db: Database, user_id: int
) -> None:
    await _insert_load_change_decision(
        db,
        user_id,
        changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=102.5)],
    )
    await _insert_load_change_decision(
        db, user_id, changes=[LoadChange(exercise_id="deadlift", from_kg=100.0, to_kg=105.0)]
    )
    # A decision with no load_changes at all must not be picked up either.
    await _insert_test_decision(db, user_id)

    async with db.read() as conn:
        deltas = await recent_increase_deltas(
            conn, user_id, "barbell_back_squat", since="1970-01-01T00:00:00.000000Z"
        )
    assert deltas == [2.5]


async def test_recent_increase_deltas_excludes_decisions_before_the_window(
    db: Database, user_id: int
) -> None:
    await _insert_load_change_decision(
        db,
        user_id,
        changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=102.5)],
    )

    async with db.read() as conn:
        deltas = await recent_increase_deltas(
            conn, user_id, "barbell_back_squat", since="2999-01-01T00:00:00.000000Z"
        )
    assert deltas == []


async def test_recent_increase_deltas_sums_across_every_decision_kind(
    db: Database, user_id: int
) -> None:
    """B5: a load change counts towards the weekly cap regardless of which decision kind
    produced it (A§7 table: progression, plan_revise, session_adjust, user_edit, ...)."""
    await _insert_load_change_decision(
        db,
        user_id,
        kind="progression",
        changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=102.5)],
    )
    await _insert_load_change_decision(
        db,
        user_id,
        kind="user_edit",
        changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=102.5, to_kg=105.0)],
    )

    async with db.read() as conn:
        deltas = await recent_increase_deltas(
            conn, user_id, "barbell_back_squat", since="1970-01-01T00:00:00.000000Z"
        )
    assert sorted(deltas) == [2.5, 2.5]


async def test_recent_increase_deltas_excludes_decreases(db: Database, user_id: int) -> None:
    """A decrease (`to_kg < from_kg`) is not an "increase" for cap purposes."""
    await _insert_load_change_decision(
        db,
        user_id,
        changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=100.0, to_kg=90.0)],
    )

    async with db.read() as conn:
        deltas = await recent_increase_deltas(
            conn, user_id, "barbell_back_squat", since="1970-01-01T00:00:00.000000Z"
        )
    assert deltas == []


async def _insert_kind(
    db: Database, user_id: int, kind: str, load_changes: list[LoadChange] | None = None
) -> int:
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind=kind,
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
            load_changes=load_changes or [],
        )


async def test_recent_increase_deltas_by_exercise_groups_positive_deltas(
    db: Database, user_id: int
) -> None:
    await _insert_kind(
        db,
        user_id,
        "progression",
        [
            LoadChange(exercise_id="barbell_back_squat", from_kg=40.0, to_kg=42.5),
            LoadChange(exercise_id="pushup", from_kg=10.0, to_kg=8.0),  # a decrease
        ],
    )
    await _insert_kind(
        db,
        user_id,
        "plan_revise",
        [LoadChange(exercise_id="barbell_back_squat", from_kg=42.5, to_kg=45.0)],
    )
    await _insert_kind(db, user_id, "hold_clear")

    async with db.read() as conn:
        by_exercise = await recent_increase_deltas_by_exercise(
            conn, user_id, since="1970-01-01T00:00:00.000000Z"
        )
        squat_only = await recent_increase_deltas(
            conn, user_id, "barbell_back_squat", since="1970-01-01T00:00:00.000000Z"
        )
        later = await recent_increase_deltas_by_exercise(
            conn, user_id, since="2999-01-01T00:00:00.000000Z"
        )

    assert by_exercise == {"barbell_back_squat": [2.5, 2.5]}
    assert by_exercise["barbell_back_squat"] == squat_only
    assert later == {}


async def test_applied_to_kg_by_exercise_takes_the_highest_applied_to_kg_in_the_window(
    db: Database, user_id: int
) -> None:
    await _insert_kind(
        db,
        user_id,
        "plan_confirm",
        [
            LoadChange(exercise_id="barbell_back_squat", from_kg=40.0, to_kg=42.5),
            LoadChange(exercise_id="pushup", from_kg=10.0, to_kg=8.0),  # a decrease counts too
        ],
    )
    await _insert_kind(
        db,
        user_id,
        "progression",
        [LoadChange(exercise_id="barbell_back_squat", from_kg=42.5, to_kg=45.0)],
    )
    await _insert_kind(db, user_id, "plan_generate")  # a draft: load_changes == []

    async with db.read() as conn:
        applied = await applied_to_kg_by_exercise(
            conn, user_id, since="1970-01-01T00:00:00.000000Z"
        )
        later = await applied_to_kg_by_exercise(conn, user_id, since="2999-01-01T00:00:00.000000Z")
    assert applied == {"barbell_back_squat": 45.0, "pushup": 8.0}
    assert later == {}


async def test_get_latest_plan_round_decision_ignores_other_kinds(
    db: Database, user_id: int
) -> None:
    async with db.read() as conn:
        assert await get_latest_plan_round_decision(conn, user_id) is None

    first = await _insert_kind(db, user_id, "plan_generate")
    await _insert_kind(db, user_id, "session_halt")
    async with db.read() as conn:
        latest = await get_latest_plan_round_decision(conn, user_id)
    assert latest is not None and latest.id == first

    second = await _insert_kind(db, user_id, "plan_revise")
    third = await _insert_kind(db, user_id, "refusal")
    await _insert_kind(db, user_id, "hold_clear")
    async with db.read() as conn:
        latest = await get_latest_plan_round_decision(conn, user_id)
    assert latest is not None and latest.id == third and latest.id > second

    fourth = await _insert_kind(db, user_id, "plan_confirm")  # a confirm ends the round too
    async with db.read() as conn:
        latest = await get_latest_plan_round_decision(conn, user_id)
    assert latest is not None and latest.id == fourth
