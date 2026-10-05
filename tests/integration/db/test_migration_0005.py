"""`0005_autoincrement_ids.sql` (M9 review fix B2): `workout_sessions`/`checkins` get
`AUTOINCREMENT` ids. A fresh DB gets it directly; a DB built from the OLD migrations
(0001-0004) with representative rows upgrades with every row preserved, foreign keys clean,
the indexes back in place — and, the whole point, a deleted row's id is never reused."""

from __future__ import annotations

import importlib
import importlib.resources
import sys
from pathlib import Path

from fitme.db.connection import Database, open_database
from fitme.db.migrate import migrate

_MIGRATIONS = "fitme.db.migrations"
_OLD_FILES = (
    "0001_init.sql",
    "0002_setup_and_activation.sql",
    "0003_plan_import.sql",
    "0004_login_codes_created_at.sql",
)
_NOW = "2026-09-27T12:00:00.000000Z"


async def _rows(db: Database, sql: str) -> list[tuple[object, ...]]:
    async with db.read() as conn, conn.execute(sql) as cursor:
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
    name = "fitme_test_old_migrations_0005"
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
    """Sessions and check-ins with a gap in their ids (5 and 2 are skipped, as if an earlier
    delete had already happened under the old schema), plus a plan/plan_version to satisfy
    the RESTRICT foreign key."""
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
        for session_id in (1, 3, 4, 6):  # 2 and 5 are "already deleted"
            await conn.execute(
                "INSERT INTO workout_sessions (id, user_id, plan_version_id, workout_key, "
                "status, current_block, started_at, finished_at, halt_reason) "
                "VALUES (?, 1, 1, 'A', 'completed', 2, ?, ?, NULL)",
                (session_id, _NOW, _NOW),
            )
        for checkin_id, session_id in ((1, 1), (3, 3)):  # 2 is "already deleted"
            await conn.execute(
                "INSERT INTO checkins (id, user_id, session_id, question_key, answer, "
                "asked_at, answered_at) VALUES (?, 1, ?, 'area:knee', 'fine', ?, ?)",
                (checkin_id, session_id, _NOW, _NOW),
            )


_TABLES = ("workout_sessions", "checkins")


# The columns these tables had when 0005 was written: 0006 added `workout_sessions.
# import_hash`, and the point here is that 0005 preserves every row it copies.
_COLUMNS_AT_0005 = {
    "workout_sessions": (
        "id, user_id, plan_version_id, workout_key, status, current_block, started_at, "
        "finished_at, halt_reason"
    ),
}


async def _snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: await _rows(
            db, f"SELECT {_COLUMNS_AT_0005.get(table, '*')} FROM {table} ORDER BY id"
        )
        for table in _TABLES
    }


async def test_fresh_db_has_autoincrement_and_never_reuses_a_deleted_id(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db)
        assert "0005_autoincrement_ids.sql" in applied
        await _seed_old_rows(db)

        async with db.transaction() as conn:
            # Delete the newest session (A§9.4's hard delete), then insert a fresh one: it
            # must NOT reuse id 6, even though 6 no longer exists in the table.
            await conn.execute("DELETE FROM workout_sessions WHERE id = 6")
            cursor = await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, "
                "status, current_block) VALUES (1, 1, 'A', 'draft', 0)"
            )
            new_id = cursor.lastrowid
        assert new_id is not None and new_id > 6

        async with db.transaction() as conn:
            await conn.execute("DELETE FROM checkins WHERE id = 3")
            cursor = await conn.execute(
                "INSERT INTO checkins (user_id, session_id, question_key, answer, asked_at) "
                "VALUES (1, 1, 'area:hip', 'unknown', ?)",
                (_NOW,),
            )
            new_checkin_id = cursor.lastrowid
        assert new_checkin_id is not None and new_checkin_id > 3
    finally:
        await db.close()


async def test_upgrade_from_old_migrations_preserves_ids_and_never_reuses_a_deleted_one(
    tmp_path: Path,
) -> None:
    package = _old_migrations_package(tmp_path)
    db = await open_database(tmp_path / "operator.db")
    try:
        assert await migrate(db, package=package) == list(_OLD_FILES)
        await _seed_old_rows(db)
        before = await _snapshot(db)
        assert before["workout_sessions"]
        assert before["checkins"]

        applied = await migrate(db)  # the real package: 0001-0004 match by checksum

        assert applied == [
            "0005_autoincrement_ids.sql",
            "0006_history_import.sql",
            "0007_plans_equal.sql",
            "0008_conversations.sql",
        ]
        assert await _snapshot(db) == before  # every row, every id, preserved verbatim
        assert await _rows(db, "PRAGMA foreign_key_check") == []
        assert await _rows(db, "PRAGMA foreign_keys") == [(1,)]
        assert await _rows(db, "PRAGMA integrity_check") == [("ok",)]
        assert await _names(db, "table", "workout_sessions") == {"workout_sessions"}
        assert await _names(db, "table", "checkins") == {"checkins"}
        assert not await _rows(
            db, "SELECT name FROM sqlite_master WHERE name LIKE 'new\\_%' ESCAPE '\\'"
        )
        # 0006 adds `idx_workout_sessions_import_hash` on top; 0005's own index must be back.
        assert "idx_workout_sessions_user_status" in await _names(db, "index", "workout_sessions")
        assert await _names(db, "index", "checkins") == {"idx_checkins_user_question"}
        fk_sessions = await _rows(db, "PRAGMA foreign_key_list(workout_sessions)")
        assert ("plan_versions", "plan_version_id", "id", "RESTRICT") in {
            (row[2], row[3], row[4], row[6]) for row in fk_sessions
        }
        fk_checkins = await _rows(db, "PRAGMA foreign_key_list(checkins)")
        assert ("workout_sessions", "session_id", "id", "SET NULL") in {
            (row[2], row[3], row[4], row[6]) for row in fk_checkins
        }

        # The whole point: deleting the newest session/check-in and inserting a fresh one
        # never reuses the deleted id, even though the table's own MAX(id) would otherwise
        # suggest it (the classic plain-rowid pitfall this migration closes).
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM workout_sessions WHERE id = 6")
            cursor = await conn.execute(
                "INSERT INTO workout_sessions (user_id, plan_version_id, workout_key, "
                "status, current_block) VALUES (1, 1, 'A', 'draft', 0)"
            )
            new_session_id = cursor.lastrowid
        assert new_session_id is not None and new_session_id > 6

        async with db.transaction() as conn:
            await conn.execute("DELETE FROM checkins WHERE id = 3")
            cursor = await conn.execute(
                "INSERT INTO checkins (user_id, session_id, question_key, answer, asked_at) "
                "VALUES (1, 1, 'area:hip', 'unknown', ?)",
                (_NOW,),
            )
            new_checkin_id = cursor.lastrowid
        assert new_checkin_id is not None and new_checkin_id > 3

        assert await migrate(db) == []  # idempotent
    finally:
        await db.close()
        _forget(tmp_path, package)
