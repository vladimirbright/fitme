"""`services/decisions.py`: thin unit-of-work wrappers over `db.controllers.decisions`
(A§4.3, AGENTS.md §6)."""

from __future__ import annotations

from fitme.db.connection import Database
from fitme.db.selectors.decisions import get_decision, list_decision_outcomes
from fitme.domain.enums import DecisionKind
from fitme.domain.guard_types import GuardVerdict
from fitme.domain.models import LoadChange
from fitme.services.decisions import record_decision, record_decision_outcome


async def test_record_decision_writes_a_full_row(db: Database, user_id: int) -> None:
    decision_id = await record_decision(
        db,
        user_id=user_id,
        kind=DecisionKind.PLAN_GENERATE,
        prompt_template="plan_generate",
        prompt_version="1",
        model="anthropic:claude-opus-5",
        content_version="abc123def456",
        llm_input={"context": {"user_id": user_id}},
        user_report=None,
        proposal={"name": "Plan A"},
        guards_fired=[GuardVerdict(rule="ceiling.historical_max", ok=True, detail="no history")],
        load_changes=[LoadChange(exercise_id="barbell_back_squat", from_kg=60.0, to_kg=62.5)],
    )

    async with db.read() as conn:
        record = await get_decision(conn, decision_id)
    assert record is not None
    assert record.user_id == user_id
    assert record.kind == "plan_generate"
    assert record.prompt_template == "plan_generate"
    assert record.prompt_version == "1"
    assert record.model == "anthropic:claude-opus-5"
    assert record.content_version == "abc123def456"
    assert record.llm_input == {"context": {"user_id": user_id}}
    assert record.proposal == {"name": "Plan A"}
    assert record.guards_fired == [
        {"rule": "ceiling.historical_max", "ok": True, "detail": "no history"}
    ]
    assert record.load_changes == [
        {"exercise_id": "barbell_back_squat", "from_kg": 60.0, "to_kg": 62.5}
    ]


async def test_record_decision_defaults_have_no_guards_or_load_changes(
    db: Database, user_id: int
) -> None:
    decision_id = await record_decision(
        db,
        user_id=user_id,
        kind=DecisionKind.REFUSAL,
        prompt_template=None,
        prompt_version=None,
        model=None,
        content_version="abc123def456",
        llm_input=None,
        user_report=None,
        proposal=None,
    )

    async with db.read() as conn:
        record = await get_decision(conn, decision_id)
    assert record is not None
    assert record.guards_fired == []
    assert record.load_changes == []
    assert record.prompt_template is None
    assert record.model is None


async def test_record_decision_outcome_links_back_to_its_decision(
    db: Database, user_id: int
) -> None:
    decision_id = await record_decision(
        db,
        user_id=user_id,
        kind=DecisionKind.SESSION_ADJUST,
        prompt_template="session_adjust",
        prompt_version="1",
        model="anthropic:claude-sonnet-5",
        content_version="abc123def456",
        llm_input={"request": "swap the first exercise"},
        user_report=None,
        proposal={"key": "A"},
    )

    outcome_id = await record_decision_outcome(
        db, decision_id=decision_id, outcome={"applied": True}
    )
    assert outcome_id > 0

    async with db.read() as conn:
        outcomes = await list_decision_outcomes(conn, decision_id)
    assert len(outcomes) == 1
    assert outcomes[0].outcome == {"applied": True}
    assert outcomes[0].decision_id == decision_id
