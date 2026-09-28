"""`0003_plan_import.sql` (M8b): the `decisions`/`plan_versions` table rebuilds that grow the
`kind`/`origin` CHECKs. A fresh DB ends up with the new CHECKs; a DB built from the OLD
migrations only (0001 + 0002) with representative rows in every affected and referencing
table upgrades with every row preserved, foreign keys clean, the append-only triggers and
the indexes back in place, and the new values accepted."""

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
_OLD_FILES = ("0001_init.sql", "0002_setup_and_activation.sql")
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
    """A throwaway package holding byte-identical copies of the OLD migration files, so
    their checksums match what the real package records and 0003 is the only pending one."""
    name = "fitme_test_old_migrations_0003"
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
    """Representative rows in every table the rebuild touches or that references one."""
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO users (id, language, timezone, created_at) VALUES (1, 'ru', 'UTC', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO plans (id, user_id, name, is_default, status, created_at) "
            "VALUES (1, 1, 'Old plan', 1, 'active', ?)",
            (_NOW,),
        )
        kinds = ["plan_generate", "plan_confirm", "session_adjust", "refusal", "progression"]
        for index, kind in enumerate(kinds, start=1):
            await conn.execute(
                "INSERT INTO decisions (id, user_id, kind, prompt_template, prompt_version, "
                "model, content_version, llm_input, user_report, proposal, load_changes, "
                "guards_fired, created_at) VALUES (?, 1, ?, ?, ?, ?, 'abc123def456', ?, ?, ?, "
                "?, ?, ?)",
                (
                    index,
                    kind,
                    "plan_generate" if kind == "plan_generate" else None,
                    "1" if kind == "plan_generate" else None,
                    "anthropic:claude-opus-5" if kind == "plan_generate" else None,
                    '{"context": {"user_id": 1}}' if kind == "plan_generate" else None,
                    '{"draft_decision_id": 1}' if kind == "plan_confirm" else None,
                    '{"plan": {"name": "Old plan", "schedule": [], "workouts": []}}',
                    '[{"exercise_id": "barbell_back_squat", "from_kg": 40.0, "to_kg": 42.5}]'
                    if kind == "plan_confirm"
                    else "[]",
                    '[{"rule": "screening.plan_allowed", "ok": true, "detail": "ok"}]',
                    f"2026-09-2{index}T12:00:00.000000Z",
                ),
            )
        for version, origin in enumerate(("llm", "progression", "user_edit"), start=1):
            await conn.execute(
                "INSERT INTO plan_versions (id, plan_id, version, body, origin, decision_id, "
                "created_at) VALUES (?, 1, ?, ?, ?, 2, ?)",
                (
                    version,
                    version,
                    '{"name": "Old plan", "schedule": [], "workouts": []}',
                    origin,
                    _NOW,
                ),
            )
        await conn.execute(
            "INSERT INTO decision_outcomes (id, decision_id, outcome, created_at) "
            "VALUES (1, 1, '{\"confirmed\": true}', ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO llm_calls (id, decision_id, purpose, model, input_tokens, "
            "output_tokens, cost_estimate_usd, latency_ms, ok, created_at) "
            "VALUES (1, 1, 'plan_generate', 'anthropic:claude-opus-5', 10, 20, 0.01, 300, 1, ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO llm_calls (id, decision_id, purpose, model, input_tokens, "
            "output_tokens, cost_estimate_usd, latency_ms, ok, created_at) "
            "VALUES (2, NULL, 'recap', 'anthropic:claude-haiku-4-5', 5, 5, NULL, 100, 0, ?)",
            (_NOW,),
        )
        await conn.execute(
            "INSERT INTO workout_sessions (id, user_id, plan_version_id, workout_key, status, "
            "current_block, started_at, finished_at, halt_reason) "
            "VALUES (1, 1, 3, 'A', 'completed', 2, ?, ?, NULL)",
            (_NOW, _NOW),
        )


_TABLES = ("decisions", "plan_versions", "decision_outcomes", "llm_calls", "workout_sessions")


# The columns these tables had when 0003 was written: a later migration may add one (0006
# added `workout_sessions.import_hash`), and the point here is that 0003 preserves every
# row it copies, not that nothing was ever added afterwards.
_COLUMNS_AT_0003 = {
    "workout_sessions": (
        "id, user_id, plan_version_id, workout_key, status, current_block, started_at, "
        "finished_at, halt_reason"
    ),
}


async def _snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: await _rows(
            db, f"SELECT {_COLUMNS_AT_0003.get(table, '*')} FROM {table} ORDER BY id"
        )
        for table in _TABLES
    }


async def test_fresh_db_accepts_plan_import_and_origin_import(tmp_path: Path) -> None:
    db = await open_database(tmp_path / "fresh.db")
    try:
        applied = await migrate(db)
        assert "0003_plan_import.sql" in applied  # later migrations may follow it
        await _seed_old_rows(db)
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                "VALUES (9, 1, 'plan_import', 'abc123def456', ?)",
                (_NOW,),
            )
            await conn.execute(
                "INSERT INTO plan_versions (id, plan_id, version, body, origin, decision_id, "
                "created_at) VALUES (9, 1, 9, '{}', 'import', 9, ?)",
                (_NOW,),
            )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                await conn.execute(
                    "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                    "VALUES (10, 1, 'not_a_kind', 'abc123def456', ?)",
                    (_NOW,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                await conn.execute(
                    "INSERT INTO plan_versions (id, plan_id, version, body, origin, "
                    "decision_id, created_at) VALUES (10, 1, 10, '{}', 'pasted', 9, ?)",
                    (_NOW,),
                )
    finally:
        await db.close()


async def test_upgrade_from_the_old_migrations_preserves_rows_fks_triggers_and_indexes(
    tmp_path: Path,
) -> None:
    package = _old_migrations_package(tmp_path)
    db = await open_database(tmp_path / "operator.db")
    try:
        assert await migrate(db, package=package) == list(_OLD_FILES)
        await _seed_old_rows(db)
        before = await _snapshot(db)
        assert all(before[table] for table in _TABLES)
        # The old CHECKs really reject the new values (so the upgrade is what admits them).
        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                await conn.execute(
                    "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                    "VALUES (9, 1, 'plan_import', 'abc123def456', ?)",
                    (_NOW,),
                )

        applied = await migrate(db)  # the real package: 0001/0002 match by checksum

        assert applied[0] == "0003_plan_import.sql"  # later migrations may follow it
        assert await _snapshot(db) == before
        assert await _rows(db, "PRAGMA foreign_key_check") == []
        assert await _rows(db, "PRAGMA foreign_keys") == [(1,)]
        assert await _rows(db, "PRAGMA integrity_check") == [("ok",)]
        assert await _names(db, "table", "decisions") == {"decisions"}
        assert await _names(db, "table", "plan_versions") == {"plan_versions"}
        assert not await _rows(
            db, "SELECT name FROM sqlite_master WHERE name LIKE 'new\\_%' ESCAPE '\\'"
        )
        assert await _names(db, "index", "decisions") == {"idx_decisions_user_created"}
        assert await _names(db, "trigger", "decisions") == {"trg_decisions_no_update"}
        assert await _names(db, "trigger", "plan_versions") == {"trg_plan_versions_no_update"}
        plan_version_indexes = await _names(db, "index", "plan_versions")
        assert len(plan_version_indexes) == 1  # the UNIQUE (plan_id, version) autoindex
        # Foreign keys still point at the rebuilt tables by name, with the same actions.
        fk_plan_versions = await _rows(db, "PRAGMA foreign_key_list(plan_versions)")
        assert {(row[2], row[3], row[4], row[6]) for row in fk_plan_versions} == {
            ("plans", "plan_id", "id", "CASCADE"),
            ("decisions", "decision_id", "id", "RESTRICT"),
        }
        for table, action in (
            ("decision_outcomes", "CASCADE"),
            ("llm_calls", "CASCADE"),
        ):
            fks = await _rows(db, f"PRAGMA foreign_key_list({table})")
            assert ("decisions", "decision_id", "id", action) in {
                (row[2], row[3], row[4], row[6]) for row in fks
            }
        fk_sessions = await _rows(db, "PRAGMA foreign_key_list(workout_sessions)")
        assert ("plan_versions", "plan_version_id", "id", "RESTRICT") in {
            (row[2], row[3], row[4], row[6]) for row in fk_sessions
        }

        async with db.transaction() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="decisions is append-only"):
                await conn.execute("UPDATE decisions SET kind = 'refusal' WHERE id = 1")
            with pytest.raises(sqlite3.IntegrityError, match="plan_versions is append-only"):
                await conn.execute("UPDATE plan_versions SET origin = 'llm' WHERE id = 2")
            # The new values are accepted now, an unknown one still is not.
            await conn.execute(
                "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                "VALUES (9, 1, 'plan_import', 'abc123def456', ?)",
                (_NOW,),
            )
            await conn.execute(
                "INSERT INTO plan_versions (id, plan_id, version, body, origin, decision_id, "
                "created_at) VALUES (9, 1, 9, '{}', 'import', 9, ?)",
                (_NOW,),
            )
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                await conn.execute(
                    "INSERT INTO decisions (id, user_id, kind, content_version, created_at) "
                    "VALUES (10, 1, 'not_a_kind', 'abc123def456', ?)",
                    (_NOW,),
                )
            # Foreign keys are enforced again: a dangling reference is rejected outright,
            # and RESTRICT still protects a plan version's decision.
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await conn.execute(
                    "INSERT INTO decision_outcomes (id, decision_id, outcome, created_at) "
                    "VALUES (9, 999, '{}', ?)",
                    (_NOW,),
                )
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await conn.execute("DELETE FROM decisions WHERE id = 9")
        assert await migrate(db) == []  # idempotent: nothing pending, checksums intact
    finally:
        await db.close()
        _forget(tmp_path, package)
