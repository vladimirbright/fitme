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
        assert applied == [
            "0001_init.sql",
            "0002_setup_and_activation.sql",
            "0003_plan_import.sql",
            "0004_login_codes_created_at.sql",
            "0005_autoincrement_ids.sql",
            "0006_history_import.sql",
        ]

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


def _make_migration_package(tmp_path: Path, name: str, files: dict[str, str]) -> str:
    package_dir = tmp_path / name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    for filename, contents in files.items():
        (package_dir / filename).write_text(contents, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    return name


def _forget_package(tmp_path: Path, name: str) -> None:
    sys.path.remove(str(tmp_path))
    sys.modules.pop(name, None)


async def test_migrate_rejects_a_file_with_its_own_begin_commit(tmp_path: Path) -> None:
    """A migration file must not manage its own transaction: `migrate()` already wraps the
    whole file in one (A§4.7); a file that adds its own BEGIN/COMMIT would either nest a
    transaction or commit the wrapper's early."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_self_transacting_migration",
        {
            "0001_bad.sql": (
                "BEGIN;\nCREATE TABLE ok_table (id INTEGER PRIMARY KEY) STRICT;\nCOMMIT;\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(MigrationError, match="BEGIN"):
            await pending_migrations(db, package=package_name)
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_rejects_a_bare_rollback_statement(tmp_path: Path) -> None:
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_rollback_migration",
        {"0001_bad.sql": ("CREATE TABLE ok_table (id INTEGER PRIMARY KEY) STRICT;\nROLLBACK;\n")},
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(MigrationError, match="ROLLBACK"):
            await pending_migrations(db, package=package_name)
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_accepts_a_create_trigger_begin_end_body(tmp_path: Path) -> None:
    """A `CREATE TRIGGER ... BEGIN ... END` body is SQLite's required trigger syntax, not
    transaction control (A§4.7), and must not be rejected. This also proves the trigger's own
    body (which contains its own `;`-terminated statement) runs as a single statement."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_trigger_migration",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT;\n"
                "CREATE TRIGGER trg_widgets_no_update\n"
                "BEFORE UPDATE ON widgets\n"
                "BEGIN\n"
                "    SELECT RAISE(ABORT, 'widgets is append-only');\n"
                "END;\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]

        async with db.transaction() as conn:
            await conn.execute("INSERT INTO widgets (id, name) VALUES (1, 'a')")
            # RAISE(ABORT, ...) undoes only the aborting statement, not the whole
            # transaction, so the surrounding transaction() block can still commit normally.
            with pytest.raises(sqlite3.IntegrityError, match="widgets is append-only"):
                await conn.execute("UPDATE widgets SET name = 'b' WHERE id = 1")
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_handles_a_semicolon_inside_a_string_literal(tmp_path: Path) -> None:
    """B7: the splitter uses `sqlite3.complete_statement()`, which understands `'...'`
    string literals; a `;` inside one must not be mistaken for a statement terminator."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_semicolon_in_string",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, x TEXT) STRICT;\n"
                "INSERT INTO widgets (id, x) VALUES (1, 'a;b');\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]
        async with db.read() as conn, conn.execute("SELECT x FROM widgets") as cursor:
            rows = await cursor.fetchall()
        assert [row[0] for row in rows] == ["a;b"]  # not silently dropped/truncated
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_handles_a_dashdash_inside_a_string_literal(tmp_path: Path) -> None:
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_dashdash_in_string",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, x TEXT) STRICT;\n"
                "INSERT INTO widgets (id, x) VALUES (1, 'a--b');\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]
        async with db.read() as conn, conn.execute("SELECT x FROM widgets") as cursor:
            rows = await cursor.fetchall()
        assert [row[0] for row in rows] == ["a--b"]  # not treated as a comment
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_handles_a_block_comment_inside_a_string_literal(tmp_path: Path) -> None:
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_blockcomment_in_string",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, x TEXT) STRICT;\n"
                "INSERT INTO widgets (id, x) VALUES (1, '/* x */');\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]
        async with db.read() as conn, conn.execute("SELECT x FROM widgets") as cursor:
            rows = await cursor.fetchall()
        assert [row[0] for row in rows] == ["/* x */"]
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_rejects_two_statements_on_one_line(tmp_path: Path) -> None:
    """A§4.7: "one SQL statement per line-group; never put two statements on one line". Also
    proves the splitter no longer hands `conn.execute()` a two-statement string, which used
    to fail with a raw, confusing `sqlite3.ProgrammingError` at apply time instead of a clear
    `MigrationError` at load time."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_two_statements_one_line",
        {
            "0001_bad.sql": (
                "CREATE TABLE a (x INTEGER) STRICT; CREATE TABLE c (z INTEGER) STRICT;\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(MigrationError, match="one line"):
            await pending_migrations(db, package=package_name)
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_rejects_a_file_missing_its_final_semicolon(tmp_path: Path) -> None:
    """B7: a statement with no terminating `;` must be rejected loudly (raised at load time,
    before anything runs), not silently dropped."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_missing_semicolon",
        {
            "0001_bad.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY) STRICT;\n"
                "CREATE INDEX idx_widgets ON widgets (id)\n"  # no trailing `;`
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(MigrationError, match="idx_widgets"):
            await pending_migrations(db, package=package_name)
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_does_not_flag_a_case_end_expression_outside_a_trigger(
    tmp_path: Path,
) -> None:
    """B7 regression: a `CASE ... END` expression in an ordinary `CREATE VIEW`/`SELECT` (not
    a trigger) must not be mistaken for a top-level `END` statement — that statement's first
    token is `CREATE`, not `END`."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_case_outside_trigger",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, x INTEGER) STRICT;\n"
                "CREATE VIEW widget_signs AS "
                "SELECT CASE WHEN x > 0 THEN 1 ELSE 0 END AS positive FROM widgets;\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_accepts_a_trigger_with_a_case_end_inside_its_body(
    tmp_path: Path,
) -> None:
    """B7 regression: a `CASE ... END` expression *inside* a trigger body must not be
    mistaken for the trigger's own closing `END`, which would split one trigger into two
    broken statements."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_case_inside_trigger",
        {
            "0001_ok.sql": (
                "CREATE TABLE widgets (id INTEGER PRIMARY KEY, x INTEGER) STRICT;\n"
                "CREATE TABLE widget_log (id INTEGER PRIMARY KEY, flag INTEGER) STRICT;\n"
                "CREATE TRIGGER trg_widgets_log\n"
                "AFTER INSERT ON widgets\n"
                "BEGIN\n"
                "    INSERT INTO widget_log (flag) "
                "VALUES (CASE WHEN NEW.x > 0 THEN 1 ELSE 0 END);\n"
                "END;\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db, package=package_name)
        assert applied == ["0001_ok.sql"]

        async with db.transaction() as conn:
            await conn.execute("INSERT INTO widgets (id, x) VALUES (1, 5)")
        async with db.read() as conn, conn.execute("SELECT flag FROM widget_log") as cursor:
            rows = await cursor.fetchall()
        assert [row[0] for row in rows] == [1]
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


async def test_migrate_rolls_back_on_a_foreign_key_violation(tmp_path: Path) -> None:
    """Foreign keys are off while a migration runs (A§4.7), so a violating INSERT succeeds at
    statement time; `PRAGMA foreign_key_check` must still catch it before COMMIT and roll the
    whole migration back."""
    package_name = _make_migration_package(
        tmp_path,
        "fitme_test_fk_violation_migration",
        {
            "0001_bad.sql": (
                "CREATE TABLE parent (id INTEGER PRIMARY KEY) STRICT;\n"
                "CREATE TABLE child (\n"
                "    id INTEGER PRIMARY KEY,\n"
                "    parent_id INTEGER NOT NULL REFERENCES parent (id)\n"
                ") STRICT;\n"
                "INSERT INTO child (id, parent_id) VALUES (1, 999);\n"
            )
        },
    )
    db = await open_database(tmp_path / "fresh.db")
    try:
        with pytest.raises(MigrationError, match="foreign-key"):
            await migrate(db, package=package_name)

        assert db.raw.in_transaction is False

        async with (
            db.read() as conn,
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'child'"
            ) as cursor,
        ):
            row = await cursor.fetchone()
        assert row is None  # rolled back: no partial schema

        async with (
            db.read() as conn,
            conn.execute("SELECT COUNT(*) FROM schema_migrations") as cursor,
        ):
            count_row = await cursor.fetchone()
        assert count_row is not None
        assert count_row[0] == 0

        # Foreign key enforcement is restored afterwards, even though the migration failed.
        async with db.read() as conn, conn.execute("PRAGMA foreign_keys") as cursor:
            fk_row = await cursor.fetchone()
        assert fk_row is not None
        assert fk_row[0] == 1
    finally:
        await db.close()
        _forget_package(tmp_path, package_name)


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
