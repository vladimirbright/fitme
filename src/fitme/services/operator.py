"""Operator-only actions run from the server shell (A§6.6): the one bypass for a health hold.

`fitme hold clear` exists for stop-word false positives. It clears every open hold and logs
each clearing as a `hold_clear` decision with `source=operator_cli`, so the audit trail
(AGENTS.md §6) shows the operator did it, not the user. The bot has no such path: the user
clears a hold only the next day, through the button flow in `services.safety`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fitme.config.content import content_version
from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.training import clear_health_hold
from fitme.db.records import HealthHoldRecord
from fitme.db.selectors.training import list_open_health_holds
from fitme.db.selectors.users import get_the_user
from fitme.domain.enums import DecisionKind

OPERATOR_CLI_SOURCE = "operator_cli"


@dataclass(frozen=True, slots=True)
class ClearedHold:
    hold_id: int
    decision_id: int


async def open_holds_for_cli(db: Database) -> list[HealthHoldRecord]:
    """Every open hold of the instance's single user (ADR 0002); empty before activation."""
    async with db.read() as conn:
        user = await get_the_user(conn)
        if user is None:
            return []
        return await list_open_health_holds(conn, user.id)


async def clear_holds_from_cli(db: Database) -> list[ClearedHold]:
    """Clear every open hold, writing one `hold_clear` decision per hold with
    `user_report={"source": "operator_cli", "hold_id": ...}`, all in one transaction: either
    every hold is cleared and logged, or nothing changes. Returns what was cleared (empty
    when there was nothing open)."""
    holds = await open_holds_for_cli(db)
    if not holds:
        return []
    version = content_version()
    cleared: list[ClearedHold] = []
    async with db.transaction() as conn:
        for hold in holds:
            await clear_health_hold(conn, hold.id)
            decision_id = await insert_decision(
                conn,
                user_id=hold.user_id,
                kind=DecisionKind.HOLD_CLEAR.value,
                prompt_template=None,
                prompt_version=None,
                model=None,
                content_version=version,
                llm_input=None,
                user_report={"source": OPERATOR_CLI_SOURCE, "hold_id": hold.id},
                proposal=None,
                guards_fired=[],
            )
            cleared.append(ClearedHold(hold_id=hold.id, decision_id=decision_id))
    return cleared
