"""Migration runner acceptance (M1): applies on an empty file, no-op on a second run,
refuses when an applied file's checksum has changed, and is atomic on failure."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

from fitme.db.connection import open_database
from fitme.db.migrate import MigrationError, migrate, pending_migrations


async def test_migrate_applies_on_an_empty_file(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db)
        assert applied == ["0001_init.sql"]

        async with (
            db.read() as conn,
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        assert row is not None
    finally:
        await db.close()


async def test_migrate_is_a_no_op_on_a_second_run(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        await migrate(db)
        second_run = await migrate(db)
        assert second_run == []
        assert await pending_migrations(db) == []
    finally:
        await db.close()


async def test_migrate_refuses_if_an_applied_file_checksum_changed(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    db = await open_database(db_path)
    try:
        await migrate(db)
        # Simulate a changed migration file by recording a different checksum for version 1.
        await db.raw.execute("UPDATE schema_migrations SET sha256 = 'deadbeef' WHERE version = 1")

        with pytest.raises(MigrationError):
            await pending_migrations(db)
    finally:
        await db.close()


async def test_migrate_is_atomic_on_failure(tmp_path: Path) -> None:
    """B2: a failing migration leaves no partial schema, no schema_migrations row, and
    `in_transaction == False` — proven with a real broken migration file, applied against a
    real temporary package (importlib.resources needs a real importable package)."""
    package_name = "fitme_test_broken_migrations"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "0001_broken.sql").write_text(
        "CREATE TABLE ok_table (id INTEGER PRIMARY KEY) STRICT;\n"
        "CREATE TABLE broken_table (id NOTATYPE) STRICT;\n",
        encoding="utf-8",
    )

    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            await migrate(db, package=package_name)

        assert db.raw.in_transaction is False

        async with (
            db.read() as conn,
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ok_table'"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        assert row is None  # rolled back: no partial schema from the same script

        async with (
            db.read() as conn,
            conn.execute("SELECT COUNT(*) FROM schema_migrations") as cursor,
        ):
            count_row = await cursor.fetchone()
        assert count_row is not None
        assert count_row[0] == 0  # no dangling bookkeeping row either
    finally:
        await db.close()
        sys.path.remove(str(tmp_path))
        sys.modules.pop(package_name, None)
