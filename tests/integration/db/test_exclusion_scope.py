"""ADR 0010 (migration 0012): an exclusion's scope — one per scope and plan, a plan's go with
the plan, a new plan's draft's become the saved plan's."""

from __future__ import annotations

import pytest

from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.db.controllers.plans import delete_plan, insert_plan
from fitme.db.controllers.profile import attach_new_plan_exclusions
from fitme.services import exclusions
from fitme.services.exclusions import Scope


async def _plan(db: Database, user_id: int) -> int:
    async with db.transaction() as conn:
        return await insert_plan(conn, user_id=user_id, name="A", is_default=False, status="active")


async def test_the_same_exclusion_is_one_row_per_scope_and_plan(db: Database, user_id: int) -> None:
    catalog = load_catalog()
    first, second = await _plan(db, user_id), await _plan(db, user_id)

    async def add(scope: Scope, plan_id: int | None = None) -> bool:
        return await exclusions.add(
            db, user_id, "name", "squat", catalog, scope=scope, plan_id=plan_id
        )

    assert await add(Scope.ALL) is True
    assert await add(Scope.ALL) is False  # NULL plan_id still collides
    assert await add(Scope.PLAN, first) is True
    assert await add(Scope.PLAN, first) is False
    assert await add(Scope.PLAN, second) is True
    assert await add(Scope.NEW_PLAN) is True
    assert await add(Scope.NEW_PLAN) is False
    assert len(await exclusions.list_for_user(db, user_id)) == 4
    assert [row.scope for row in await exclusions.list_for_all_plans(db, user_id)] == ["all"]


async def test_a_plan_exclusion_goes_with_its_plan(db: Database, user_id: int) -> None:
    plan_id = await _plan(db, user_id)
    await exclusions.add(
        db, user_id, "name", "lunge", load_catalog(), scope=Scope.PLAN, plan_id=plan_id
    )
    async with db.transaction() as conn:
        await delete_plan(conn, user_id, plan_id)
    assert await exclusions.list_for_user(db, user_id) == []


async def test_saving_a_new_plan_attaches_its_drafts_exclusions(db: Database, user_id: int) -> None:
    catalog = load_catalog()
    await exclusions.add(db, user_id, "name", "lunge", catalog, scope=Scope.NEW_PLAN)
    plan_id = await _plan(db, user_id)
    async with db.transaction() as conn:
        await attach_new_plan_exclusions(conn, user_id=user_id, plan_id=plan_id)
    [row] = await exclusions.list_for_user(db, user_id)
    assert (row.scope, row.plan_id, row.value) == ("plan", plan_id, "lunge")


async def test_a_plan_exclusion_needs_its_plan(db: Database, user_id: int) -> None:
    with pytest.raises(exclusions.InvalidExclusionError):
        await exclusions.add(db, user_id, "name", "lunge", load_catalog(), scope=Scope.PLAN)
