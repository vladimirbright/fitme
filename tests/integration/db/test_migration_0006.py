"""`0006_history_import.sql` (M11): `set_logs.source` accepts `'import'` (table rebuilt with
an AUTOINCREMENT id), and `workout_sessions.import_hash` exists with a partial unique index.
A fresh DB gets it directly; a DB built from the OLD migrations (0001-0005) with
representative rows — every existing `source` value included — upgrades with every row and
id preserved, foreign keys clean, both `set_logs` indexes back in place."""

from __future__ import annotations

import importlib
import importlib.resources
import sqlite3
import sys
from pathlib import Path

import pytest

from fitme.db.connection import Database, open_database
from fitme.db.migrate import migrate

_MIGRATIONS = "fitme.db.migrations"
_OLD_FILES = (
    "0001_init.sql",
    "0002_setup_and_activation.sql",
    "0003_plan_import.sql",
    "0004_login_codes_created_at.sql",
    "0005_autoincrement_ids.sql",
)
_NOW = "2026-09-27T12:00:00.000000Z"
_OLD_SESSION_COLUMNS = (
    "id, user_id, plan_version_id, workout_key, status, current_block, started_at, "
    "finished_at, halt_reason"
)
_SET_LOG_COLUMNS = (
    "id, session_id, exercise_id, set_index, planned_load_kg, planned_reps_min, "
    "planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, source, created_at"
)


async def _rows(
    db: Database, sql: str, params: tuple[object, ...] = ()
) -> list[tuple[object, ...]]:
    async with db.read() as conn, conn.execute(sql, params) as cursor:
        return [tuple(row) for row in await cursor.fetchall()]


async def _names(db: Database, kind: str, table: str) -> set[str]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND tbl_name = ?", (kind, table)
        ) as cursor,
    ):
        return {str(row[0]) for row in await cursor.fetchall()}


def _old_migrations_package(tmp_path: Path) -> str:
    name = "fitme_test_old_migrations_0006"
    package_dir = tmp_path / name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    root = importlib.resources.files(_MIGRATIONS)
    for filename in _OLD_FILES:
        (package_dir / filename).write_bytes((root / filename).read_bytes())
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    return name


def _forget(tmp_path: Path, name: str) -> None:
    sys.path.remove(str(tmp_path))
    sys.modules.pop(name, None)


async def _seed_old_rows(db: Database) -> None:
    """A user, a plan/version (RESTRICT FK), three sessions and set rows covering every
    pre-0006 `source` value, with a gap in the set ids (3 is "already deleted")."""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO users (id, language, timezone, created_at) VALUES (1, 'en', 'UTC', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO plans (id, user_id, name, is_default, status, created_at) "
            "VALUES (1, 1, 'P', 1, 'active', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
            "VALUES (1, 1, 'plan_confirm', 'abc123def456', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO plan_versions (id, plan_id, version, body, origin, decision_id, "
            "created_at) VALUES (1, 1, 1, '{}', 'llm', 1, ?)",
            (_NOW,),
        )
        for session_id in (1, 2, 3):
            await conn.execute(
                "INSERT INTO workout_sessions (id, user_id, plan_version_id, workout_key, "
                "status, current_block, started_at, finished_at, halt_reason) "
                "VALUES (?, 1, 1, 'A', 'completed', 2, ?, ?, NULL)",
                (session_id, _NOW, _NOW),
            )
        rows = (
            (1, 1, "button", 60.0, 6, 0),
            (2, 1, "free_text", 60.0, 5, 0),
            (4, 2, "web", None, None, 1),  # a skipped set: actual_* NULL
            (5, 3, "button", 62.5, 8, 0),
        )
        for set_id, session_id, source, actual_kg, actual_reps, skipped in rows:
            await conn.execute(
                "INSERT INTO set_logs (id, session_id, exercise_id, set_index, planned_load_kg, "
                "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, "
                "source, created_at) VALUES (?, ?, 'barbell_back_squat', 1, 60.0, 5, 8, ?, ?, "
                "?, NULL, ?, ?)",
                (set_id, session_id, actual_kg, actual_reps, skipped, source, _NOW),
            )


async def _snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    return {
        "workout_sessions": await _rows(
            db, f"SELECT {_OLD_SESSION_COLUMNS} FROM workout_sessions ORDER BY id"
        ),
        "set_logs": await _rows(db, f"SELECT {_SET_LOG_COLUMNS} FROM set_logs ORDER BY id"),
    }


async def _assert_0006_schema(db: Database) -> None:
    assert await _rows(db, "PRAGMA foreign_key_check") == []
    assert await _rows(db, "PRAGMA integrity_check") == [("ok",)]
    assert await _names(db, "table", "set_logs") == {"set_logs"}
    assert not await _rows(
        db, "SELECT name FROM sqlite_master WHERE name LIKE 'new\\_%' ESCAPE '\\'"
    )
    assert await _names(db, "index", "set_logs") == {
        "idx_set_logs_session",
        "idx_set_logs_exercise",
    }
    assert "idx_workout_sessions_import_hash" in await _names(db, "index", "workout_sessions")
    fk = await _rows(db, "PRAGMA foreign_key_list(set_logs)")
    assert ("workout_sessions", "session_id", "id", "CASCADE") in {
        (row[2], row[3], row[4], row[6]) for row in fk
    }
    columns = {str(row[1]) for row in await _rows(db, "PRAGMA table_info(workout_sessions)")}
    assert "import_hash" in columns
    assert ("set_logs",) in await _rows(db, "SELECT name FROM sqlite_sequence")


async def _assert_0006_behaviour(db: Database) -> None:
    # 'import' is now a valid source; anything else is still rejected by the CHECK.
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO set_logs (session_id, exercise_id, set_index, planned_load_kg, "
            "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, "
            "source, created_at) VALUES (1, 'barbell_back_squat', 2, 75.0, 5, 5, 75.0, 5, 0, "
            "NULL, 'import', ?)",
            (_NOW,),
        )
        imported_id = cursor.lastrowid
    assert imported_id is not None and imported_id > 5  # AUTOINCREMENT: past the old max
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO set_logs (session_id, exercise_id, set_index, skipped, source, "
                "created_at) VALUES (1, 'barbell_back_squat', 3, 0, 'bogus', ?)",
                (_NOW,),
            )
    # The skipped/actual_* invariant survived the rebuild.
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO set_logs (session_id, exercise_id, set_index, actual_reps, skipped, "
                "source, created_at) VALUES (1, 'barbell_back_squat', 3, 5, 1, 'import', ?)",
                (_NOW,),
            )
    # A deleted set id is never reused.
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM set_logs WHERE id = ?", (imported_id,))
        cursor = await conn.execute(
            "INSERT INTO set_logs (session_id, exercise_id, set_index, skipped, source, "
            "created_at) VALUES (1, 'barbell_back_squat', 4, 1, 'import', ?)",
            (_NOW,),
        )
        next_id = cursor.lastrowid
    assert next_id is not None and next_id > imported_id

    # import_hash: NULL for app sessions (any number of them), unique when set.
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
            "current_block, started_at, finished_at, import_hash) "
            "VALUES (1, 1, 'import', 'completed', 0, ?, ?, 'hash-a')",
            (_NOW, _NOW),
        )
        await conn.execute(
            "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
            "current_block) VALUES (1, 1, 'A', 'draft', 0)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
                "current_block, started_at, finished_at, import_hash) "
                "VALUES (1, 1, 'import', 'completed', 0, ?, ?, 'hash-a')",
                (_NOW, _NOW),
            )
    null_hashes = await _rows(db, "SELECT COUNT(*) FROM workout_sessions WHERE import_hash IS NULL")
    assert null_hashes == [(4,)]


async def test_fresh_db_has_the_0006_schema(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db)
        assert "0006_history_import.sql" in applied
        await _seed_old_rows(db)
        await _assert_0006_schema(db)
        await _assert_0006_behaviour(db)
    finally:
        await db.close()


async def test_upgrade_from_0005_preserves_every_row_and_id(tmp_path: Path) -> None:
    package = _old_migrations_package(tmp_path)
    db = await open_database(tmp_path / "operator.db")
    try:
        assert await migrate(db, package=package) == list(_OLD_FILES)
        await _seed_old_rows(db)
        before = await _snapshot(db)
        assert len(before["set_logs"]) == 4

        applied = await migrate(db)  # the real package: 0001-0005 match by checksum

        assert applied == [
            "0006_history_import.sql",
            "0007_plans_equal.sql",
            "0008_conversations.sql",
        ]
        assert await _snapshot(db) == before  # every row, every id, every source preserved
        assert await _rows(db, "SELECT import_hash FROM workout_sessions") == [(None,)] * 3
        assert await _rows(db, "PRAGMA foreign_keys") == [(1,)]
        await _assert_0006_schema(db)
        await _assert_0006_behaviour(db)
        assert await migrate(db) == []  # idempotent
    finally:
        await db.close()
        _forget(tmp_path, package)
