"""Shared fixtures for DB integration tests: a real temporary SQLite file, migrated."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fitme.db.connection import Database, open_database
from fitme.db.controllers.users import insert_user
from fitme.db.migrate import migrate


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """A migrated Database backed by a fresh temporary file."""
    database = await open_database(tmp_path / "fitme-test.db")
    await migrate(database)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def user_id(db: Database) -> int:
    """Seed the one user row this instance ever has (ADR 0002)."""
    async with db.transaction() as conn:
        return await insert_user(conn, language="en", timezone="Europe/Berlin")
