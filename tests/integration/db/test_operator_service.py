"""`services.operator.clear_holds_from_cli` (A§6.6): the operator's hold bypass clears every
open hold and logs one `hold_clear` decision per hold with `source=operator_cli`."""

from __future__ import annotations

from fitme.db.connection import Database
from fitme.db.controllers.training import insert_health_hold
from fitme.db.selectors.decisions import list_decisions_for_user
from fitme.db.selectors.training import list_open_health_holds
from fitme.services.operator import clear_holds_from_cli, open_holds_for_cli


async def _open_hold(db: Database, user_id: int, *, reason: str) -> int:
    async with db.transaction() as conn:
        return await insert_health_hold(
            conn, user_id=user_id, reason=reason, source_session_id=None
        )


async def test_clear_holds_from_cli_clears_every_open_hold_and_logs_each(
    db: Database, user_id: int
) -> None:
    first = await _open_hold(db, user_id, reason="stop_word")
    second = await _open_hold(db, user_id, reason="pain_button")

    cleared = await clear_holds_from_cli(db)

    assert sorted(item.hold_id for item in cleared) == sorted([first, second])
    assert await open_holds_for_cli(db) == []
    async with db.read() as conn:
        assert await list_open_health_holds(conn, user_id) == []
        decisions = await list_decisions_for_user(conn, user_id)
    hold_clears = [d for d in decisions if d.kind == "hold_clear"]
    assert len(hold_clears) == 2
    assert {d.id for d in hold_clears} == {item.decision_id for item in cleared}
    for decision in hold_clears:
        assert decision.user_report is not None
        assert decision.user_report["source"] == "operator_cli"
        assert decision.user_report["hold_id"] in (first, second)
        assert decision.content_version
        assert decision.model is None  # no LLM involved (A§6.6: a deterministic bypass)


async def test_clear_holds_from_cli_is_a_no_op_with_nothing_open(
    db: Database, user_id: int
) -> None:
    assert await clear_holds_from_cli(db) == []
    async with db.read() as conn:
        assert await list_decisions_for_user(conn, user_id) == []


async def test_clear_holds_from_cli_is_a_no_op_before_activation(db: Database) -> None:
    """No user row yet (ADR 0002): nothing to list, nothing to clear, no error."""
    assert await open_holds_for_cli(db) == []
    assert await clear_holds_from_cli(db) == []
