"""controllers/selectors for `plans` and `plan_versions` (A§4.3)."""

from __future__ import annotations

import sqlite3

import pytest

from fitme.db.connection import Database
from fitme.db.controllers.decisions import insert_decision
from fitme.db.controllers.plans import (
    PlanNotOwnedError,
    insert_plan,
    insert_plan_version,
    rename_plan,
    set_default_plan,
)
from fitme.db.selectors.plans import (
    get_default_plan,
    get_latest_plan_version,
    get_plan,
    get_plan_version,
    list_plan_versions,
    list_plans_for_user,
)


async def _insert_decision(db: Database, user_id: int) -> int:
    async with db.transaction() as conn:
        return await insert_decision(
            conn,
            user_id=user_id,
            kind="plan_generate",
            prompt_template=None,
            prompt_version=None,
            model=None,
            content_version="abc123def456",
            llm_input=None,
            user_report=None,
            proposal=None,
            guards_fired=[],
        )


async def test_plan_and_plan_version_insert_and_read(db: Database, user_id: int) -> None:
    decision_id = await _insert_decision(db, user_id)
    async with db.transaction() as conn:
        plan_id = await insert_plan(
            conn, user_id=user_id, name="My plan", is_default=True, status="draft"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "My plan", "workouts": []},
            origin="llm",
            decision_id=decision_id,
        )

    async with db.read() as conn:
        plan = await get_plan(conn, plan_id)
        version = await get_plan_version(conn, version_id)
        latest = await get_latest_plan_version(conn, plan_id)
        all_versions = await list_plan_versions(conn, plan_id)
        default_plan = await get_default_plan(conn, user_id)

    assert plan is not None
    assert plan.is_default is True
    assert version is not None
    assert version.body == {"name": "My plan", "workouts": []}
    assert latest == version
    assert all_versions == [version]
    assert default_plan == plan


async def test_set_default_plan_clears_the_previous_default(db: Database, user_id: int) -> None:
    async with db.transaction() as conn:
        first = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="draft"
        )
        second = await insert_plan(
            conn, user_id=user_id, name="Plan B", is_default=False, status="draft"
        )
        await set_default_plan(conn, user_id, second)

    async with db.read() as conn:
        plans = {p.id: p for p in await list_plans_for_user(conn, user_id)}

    assert plans[first].is_default is False
    assert plans[second].is_default is True


async def test_set_default_plan_rejects_an_unowned_plan_id(db: Database, user_id: int) -> None:
    """This instance is single-user (ADR 0002), so there's no second real user to own a
    plan with; a plan_id that was never inserted exercises the same ownership check (it
    "doesn't belong to user_id" exactly as a stranger's plan wouldn't)."""
    async with db.transaction() as conn:
        own_plan = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="draft"
        )
        nonexistent_plan_id = own_plan + 1000

        with pytest.raises(PlanNotOwnedError):
            await set_default_plan(conn, user_id, nonexistent_plan_id)

    async with db.read() as conn:
        plans = {p.id: p for p in await list_plans_for_user(conn, user_id)}
    # The existing default must not have been cleared by the rejected call.
    assert plans[own_plan].is_default is True


async def test_plan_versions_table_rejects_update(db: Database, user_id: int) -> None:
    """0001_init.sql's BEFORE UPDATE trigger makes plan_versions append-only a DB invariant,
    not just a controller-layer convention (A§4.6 rule 5)."""
    decision_id = await _insert_decision(db, user_id)
    async with db.transaction() as conn:
        plan_id = await insert_plan(
            conn, user_id=user_id, name="Plan A", is_default=True, status="draft"
        )
        version_id = await insert_plan_version(
            conn,
            plan_id=plan_id,
            version=1,
            body={"name": "Plan A", "workouts": []},
            origin="llm",
            decision_id=decision_id,
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        await db.raw.execute(
            "UPDATE plan_versions SET origin = 'user_edit' WHERE id = ?", (version_id,)
        )


async def test_rename_plan_updates_the_name_and_rejects_an_unowned_plan_id(
    db: Database, user_id: int
) -> None:
    """`plans.name` is a plain column: a rename updates it in place. A plan_id that isn't
    this user's own (here: one that was never inserted, ADR 0002) raises and changes
    nothing."""
    async with db.transaction() as conn:
        plan_id = await insert_plan(
            conn, user_id=user_id, name="P", is_default=True, status="active"
        )
        await rename_plan(conn, user_id, plan_id, "Q")
    async with db.read() as conn:
        plan = await get_plan(conn, plan_id)
    assert plan is not None and plan.name == "Q" and plan.is_default

    with pytest.raises(PlanNotOwnedError):
        async with db.transaction() as conn:
            await rename_plan(conn, user_id, plan_id + 1000, "R")
    with pytest.raises(PlanNotOwnedError):
        async with db.transaction() as conn:
            await rename_plan(conn, user_id + 1, plan_id, "R")
    async with db.read() as conn:
        plan = await get_plan(conn, plan_id)
    assert plan is not None and plan.name == "Q"
