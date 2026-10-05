"""`0007_plans_equal.sql` ("all plans are equal", A§4.3): `workout_sessions.plan_version_id`
becomes nullable (table rebuilt, AUTOINCREMENT id, `import_hash` and both indexes kept),
imported sessions are detached from the M11 holder plan, the holder plan and its version are
deleted, and every `archived` plan becomes `active`. A fresh DB gets it directly; a DB built
from the OLD migrations (0001-0006) with a holder plan + imported sessions, an archived normal
plan and normal sessions upgrades with everything else preserved, foreign keys and integrity
clean, and the session id sequence continuing past a deleted newest id."""

from __future__ import annotations

import importlib
import importlib.resources
import json
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
    "0006_history_import.sql",
)
_NOW = "2026-09-28T12:00:00.000000Z"
_SESSION_COLUMNS = (
    "id, user_id, plan_version_id, workout_key, status, current_block, started_at, "
    "finished_at, halt_reason, import_hash"
)
_SET_LOG_COLUMNS = (
    "id, session_id, exercise_id, set_index, planned_load_kg, planned_reps_min, "
    "planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, source, created_at"
)
_HOLDER_PLAN_ID = 2
_HOLDER_VERSION_ID = 2
_ARCHIVED_PLAN_ID = 3


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
    name = "fitme_test_old_migrations_0007"
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
    """What an M11 database looks like before 0007: a user; plan 1 (active, default, an
    `llm` version 1 and a `user_edit` version 2); the M11 holder plan 2 (archived, one
    `import` version named by the `history_import` decision outcome); plan 3 (archived, a
    normal plan); sessions 1-2 on plan 1, imported sessions 3-5 on the holder, and set rows
    on all of them; session 6 was deleted (the sequence stands at 6)."""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO users (id, language, timezone, created_at) VALUES (1, 'en', 'UTC', ?)",
            (_NOW,),
        )
        for plan_id, name, is_default, status in (
            (1, "P", 1, "active"),
            (_HOLDER_PLAN_ID, "Imported history", 0, "archived"),
            (_ARCHIVED_PLAN_ID, "Old", 0, "archived"),
        ):
            await conn.execute(
                "INSERT INTO plans (id, user_id, name, is_default, status, created_at) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (plan_id, name, is_default, status, _NOW),
            )
        for decision_id, kind in ((1, "plan_confirm"), (2, "history_import"), (3, "user_edit")):
            await conn.execute(
                "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                "VALUES (?, 1, ?, 'abc123def456', ?)",
                (decision_id, kind, _NOW),
            )
        for version_id, plan_id, version, origin, decision_id in (
            (1, 1, 1, "llm", 1),
            (_HOLDER_VERSION_ID, _HOLDER_PLAN_ID, 1, "import", 2),
            (3, 1, 2, "user_edit", 3),
            (4, _ARCHIVED_PLAN_ID, 1, "llm", 1),
        ):
            await conn.execute(
                "INSERT INTO plan_versions (id, plan_id, version, body, origin, decision_id, "
                "created_at) VALUES (?, ?, ?, '{}', ?, ?, ?)",
                (version_id, plan_id, version, origin, decision_id, _NOW),
            )
        outcome = {"holder_plan_version_id": _HOLDER_VERSION_ID, "session_ids": [3, 4, 5]}
        await conn.execute(
            "INSERT INTO decision_outcomes (id, decision_id, outcome, created_at) "
            "VALUES (1, 2, ?, ?)",
            (json.dumps(outcome), _NOW),
        )
        for session_id, version_id, key, import_hash in (
            (1, 1, "A", None),
            (2, 3, "B", None),
            (3, _HOLDER_VERSION_ID, "import", "h3"),
            (4, _HOLDER_VERSION_ID, "import", "h4"),
            (5, _HOLDER_VERSION_ID, "import", "h5"),
            (6, 3, "A", None),  # deleted below: the sequence must still remember it
        ):
            await conn.execute(
                "INSERT INTO workout_sessions (id, user_id, plan_version_id, workout_key, "
                "status, current_block, started_at, finished_at, halt_reason, import_hash) "
                "VALUES (?, 1, ?, ?, 'completed', 2, ?, ?, NULL, ?)",
                (session_id, version_id, key, _NOW, _NOW, import_hash),
            )
        for set_id, session_id, source in (
            (1, 1, "button"),
            (2, 2, "web"),
            (3, 3, "import"),
            (4, 4, "import"),
            (5, 5, "import"),
        ):
            await conn.execute(
                "INSERT INTO set_logs (id, session_id, exercise_id, set_index, planned_load_kg, "
                "planned_reps_min, planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, "
                "source, created_at) VALUES (?, ?, 'barbell_back_squat', 1, 60.0, 5, 8, 60.0, "
                "6, 0, NULL, ?, ?)",
                (set_id, session_id, source, _NOW),
            )
        await conn.execute(
            "INSERT INTO checkins (id, user_id, session_id, question_key, answer, asked_at) "
            "VALUES (1, 1, 2, 'area:knee', 'unknown', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO health_holds (id, user_id, reason, source_session_id, created_at) "
            "VALUES (1, 1, 'pain_button', 1, ?)",
            (_NOW,),
        )
        await conn.execute("DELETE FROM workout_sessions WHERE id = 6")


async def _snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    return {
        "workout_sessions": await _rows(
            db, f"SELECT {_SESSION_COLUMNS} FROM workout_sessions ORDER BY id"
        ),
        "set_logs": await _rows(db, f"SELECT {_SET_LOG_COLUMNS} FROM set_logs ORDER BY id"),
        "plans": await _rows(
            db, "SELECT id, user_id, name, is_default, status FROM plans ORDER BY id"
        ),
        "plan_versions": await _rows(
            db, "SELECT id, plan_id, version, origin, decision_id FROM plan_versions ORDER BY id"
        ),
        "decisions": await _rows(db, "SELECT id, kind FROM decisions ORDER BY id"),
        "decision_outcomes": await _rows(
            db, "SELECT id, decision_id, outcome FROM decision_outcomes ORDER BY id"
        ),
        "checkins": await _rows(db, "SELECT id, session_id, answer FROM checkins ORDER BY id"),
        "health_holds": await _rows(
            db, "SELECT id, source_session_id FROM health_holds ORDER BY id"
        ),
    }


def _expected_after(
    before: dict[str, list[tuple[object, ...]]],
) -> dict[str, list[tuple[object, ...]]]:
    """`before` with exactly the 0007 changes applied: the holder plan and its version gone,
    the imported sessions detached, the archived plans active. Nothing else."""
    after = dict(before)
    after["workout_sessions"] = [
        (*row[:2], None, *row[3:]) if row[9] is not None else row
        for row in before["workout_sessions"]
    ]
    after["plans"] = [(*row[:4], "active") for row in before["plans"] if row[0] != _HOLDER_PLAN_ID]
    after["plan_versions"] = [row for row in before["plan_versions"] if row[1] != _HOLDER_PLAN_ID]
    return after


async def _assert_0007_schema(db: Database) -> None:
    assert await _rows(db, "PRAGMA foreign_key_check") == []
    assert await _rows(db, "PRAGMA integrity_check") == [("ok",)]
    assert await _names(db, "table", "workout_sessions") == {"workout_sessions"}
    assert not await _rows(
        db, "SELECT name FROM sqlite_master WHERE name LIKE 'new\\_%' ESCAPE '\\'"
    )
    assert await _names(db, "index", "workout_sessions") == {
        "idx_workout_sessions_user_status",
        "idx_workout_sessions_import_hash",
    }
    columns = {
        str(row[1]): (int(row[3]), row[5])  # notnull, pk
        for row in await _rows(db, "PRAGMA table_info(workout_sessions)")
    }
    assert columns["plan_version_id"] == (0, 0)
    assert columns["user_id"] == (1, 0) and columns["workout_key"] == (1, 0)
    assert columns["id"] == (0, 1) and "import_hash" in columns
    fk = {
        (row[2], row[3], row[4], row[6])
        for row in await _rows(db, "PRAGMA foreign_key_list(workout_sessions)")
    }
    assert ("plan_versions", "plan_version_id", "id", "RESTRICT") in fk
    assert ("users", "user_id", "id", "CASCADE") in fk
    create_sql = (
        await _rows(
            db, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'workout_sessions'"
        )
    )[0][0]
    assert isinstance(create_sql, str) and "AUTOINCREMENT" in create_sql and "STRICT" in create_sql
    assert await _rows(
        db, "SELECT COUNT(*) FROM sqlite_sequence WHERE name = 'workout_sessions'"
    ) == [(1,)]
    assert await _rows(
        db, "SELECT COUNT(*) FROM sqlite_sequence WHERE name = 'new_workout_sessions'"
    ) == [(0,)]


async def _assert_0007_behaviour(db: Database, *, next_id_above: int) -> None:
    # A session with no plan can be inserted (an import); the CHECKs still hold; a deleted
    # id is never reused; the unique import hash still is.
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
            "current_block, started_at, finished_at, import_hash) "
            "VALUES (1, NULL, 'import', 'completed', 0, ?, ?, 'hash-new')",
            (_NOW, _NOW),
        )
        new_id = cursor.lastrowid
    assert new_id is not None and new_id > next_id_above
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
                "current_block) VALUES (1, NULL, 'A', 'bogus', 0)"
            )
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
                "current_block, started_at, finished_at, import_hash) "
                "VALUES (1, NULL, 'import', 'completed', 0, ?, ?, 'hash-new')",
                (_NOW, _NOW),
            )
    with pytest.raises(sqlite3.IntegrityError):  # the RESTRICT FK is still enforced
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
                "current_block) VALUES (1, 999, 'A', 'draft', 0)"
            )
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM workout_sessions WHERE id = ?", (new_id,))
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
            "current_block) VALUES (1, 1, 'A', 'draft', 0)"
        )
        next_id = cursor.lastrowid
    assert next_id is not None and next_id > new_id
    # 'archived' is still accepted by the CHECK (schema compatibility), but nothing writes it.
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO plans (user_id, name, is_default, status, created_at) "
            "VALUES (1, 'Kept', 0, 'archived', ?)",
            (_NOW,),
        )


async def test_fresh_db_has_the_0007_schema(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db)
        assert "0007_plans_equal.sql" in applied
        await _seed_old_rows(db)  # the seed rows are still valid under the new schema
        await _assert_0007_schema(db)
        await _assert_0007_behaviour(db, next_id_above=6)
    finally:
        await db.close()


async def test_upgrade_from_0006_detaches_imports_drops_the_holder_and_unarchives(
    tmp_path: Path,
) -> None:
    package = _old_migrations_package(tmp_path)
    db = await open_database(tmp_path / "operator.db")
    try:
        assert await migrate(db, package=package) == list(_OLD_FILES)
        await _seed_old_rows(db)
        before = await _snapshot(db)
        assert len(before["workout_sessions"]) == 5
        assert await _rows(
            db, "SELECT seq FROM sqlite_sequence WHERE name = 'workout_sessions'"
        ) == [(6,)]

        applied = await migrate(db)  # the real package: 0001-0006 match by checksum

        assert applied == ["0007_plans_equal.sql", "0008_conversations.sql"]
        after = await _snapshot(db)
        assert after == _expected_after(before)
        # Spelled out: the holder is gone, the imported sessions belong to no plan, the
        # archived normal plan is active and everything else is untouched.
        assert [row[0] for row in after["plans"]] == [1, _ARCHIVED_PLAN_ID]
        assert {row[4] for row in after["plans"]} == {"active"}
        assert [row[0] for row in after["plan_versions"]] == [1, 3, 4]
        assert [row[2] for row in after["workout_sessions"]] == [1, 3, None, None, None]
        assert after["set_logs"] == before["set_logs"]
        assert after["decisions"] == before["decisions"]
        assert after["decision_outcomes"] == before["decision_outcomes"]  # append-only
        assert after["checkins"] == before["checkins"]
        assert after["health_holds"] == before["health_holds"]
        # The id sequence continues past the deleted session 6, not from MAX(id) = 5.
        assert await _rows(
            db, "SELECT seq FROM sqlite_sequence WHERE name = 'workout_sessions'"
        ) == [(6,)]
        assert await _rows(db, "PRAGMA foreign_keys") == [(1,)]
        await _assert_0007_schema(db)
        await _assert_0007_behaviour(db, next_id_above=6)
        assert await migrate(db) == []  # idempotent
    finally:
        await db.close()
        _forget(tmp_path, package)


async def test_upgrade_refuses_to_orphan_a_real_session_on_a_holder_version(
    tmp_path: Path,
) -> None:
    """Defense in depth: a session without an `import_hash` that references the holder
    version (impossible through the app) would be orphaned by deleting the holder, so the
    RESTRICT FK trips `foreign_key_check` and the whole migration rolls back."""
    from fitme.db.migrate import MigrationError

    package = _old_migrations_package(tmp_path)
    db = await open_database(tmp_path / "operator.db")
    try:
        await migrate(db, package=package)
        await _seed_old_rows(db)
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, status, "
                "current_block) VALUES (1, ?, 'import', 'draft', 0)",
                (_HOLDER_VERSION_ID,),
            )
        before = await _snapshot(db)

        with pytest.raises(MigrationError, match="foreign-key violation"):
            await migrate(db)

        assert await _snapshot(db) == before
        assert await _rows(db, "SELECT version FROM schema_migrations ORDER BY version") == [
            (version,) for version in range(1, 7)
        ]
    finally:
        await db.close()
        _forget(tmp_path, package)
