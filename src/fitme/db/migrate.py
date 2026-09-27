"""Applies `db/migrations/*.sql` in order (A§4.7).

Migration files are plain, forward-only SQL, numbered `NNNN_name.sql`. There are no down
migrations: restore a backup instead. Each file's checksum is recorded in
`schema_migrations`, in the same transaction as the migration itself, so a crash partway
through never leaves an applied-but-unrecorded (or recorded-but-unapplied) migration.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
from dataclasses import dataclass

import aiosqlite

from fitme import clock
from fitme.db.connection import Database

_MIGRATIONS_PACKAGE = "fitme.db.migrations"
_FILENAME_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Created before any versioned migration runs; this table is infrastructure for the
# migration runner itself, not part of the application schema in 0001_init.sql.
_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;
"""


class MigrationError(RuntimeError):
    """A migration file is malformed, or an applied file's checksum no longer matches."""


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    sql: str
    sha256: str


def _load_migration_files(package: str) -> list[MigrationFile]:
    """Read every `NNNN_name.sql` file bundled with `package`, sorted by version."""
    package_files = importlib.resources.files(package)
    files: list[MigrationFile] = []
    for entry in package_files.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _FILENAME_PATTERN.match(entry.name)
        if match is None:
            raise MigrationError(
                f"migration file name must match {_FILENAME_PATTERN.pattern}: {entry.name}"
            )
        sql = entry.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        files.append(
            MigrationFile(version=int(match.group(1)), name=entry.name, sql=sql, sha256=checksum)
        )
    files.sort(key=lambda f: f.version)
    return files


async def _ensure_schema_migrations_table(conn: aiosqlite.Connection) -> None:
    await conn.execute(_SCHEMA_MIGRATIONS_DDL)


async def _applied_checksums(conn: aiosqlite.Connection) -> dict[int, str]:
    applied: dict[int, str] = {}
    async with conn.execute("SELECT version, sha256 FROM schema_migrations") as cursor:
        async for version, sha256 in cursor:
            applied[version] = sha256
    return applied


async def pending_migrations(
    database: Database, *, package: str = _MIGRATIONS_PACKAGE
) -> list[MigrationFile]:
    """Return migration files not yet applied, after verifying already-applied checksums."""
    conn = database.raw
    await _ensure_schema_migrations_table(conn)
    applied = await _applied_checksums(conn)

    pending: list[MigrationFile] = []
    for migration in _load_migration_files(package):
        recorded = applied.get(migration.version)
        if recorded is None:
            pending.append(migration)
        elif recorded != migration.sha256:
            raise MigrationError(
                f"{migration.name} has changed since it was applied (recorded checksum "
                f"{recorded[:12]}..., file checksum {migration.sha256[:12]}...); never edit "
                "an applied migration, write a new one instead"
            )
    return pending


async def migrate(database: Database, *, package: str = _MIGRATIONS_PACKAGE) -> list[str]:
    """Apply every pending migration file, in order. Returns the names applied.

    Each migration runs as one script: `BEGIN`, the file's own SQL, an INSERT recording it
    in `schema_migrations`, then `COMMIT` — all inside the single `executescript()` call, so
    a failure partway through leaves neither a partial schema change nor a dangling
    `schema_migrations` row. `executescript()` cannot bind parameters, so the INSERT's
    values are spliced into the script text directly; this is safe because none of them are
    caller-controlled: `version` is an int parsed from the filename, `name` matched the
    strict `_FILENAME_PATTERN` (digits, lowercase letters, underscores only — no quote or
    statement-separator characters are even possible), `sha256` is a hex digest from
    `hashlib`, and `applied_at` comes from `fitme.clock` (digits, `-`, `:`, `.`, `T`, `Z`
    only). On failure, the transaction is rolled back before the error propagates.
    """
    conn = database.raw
    applied_names: list[str] = []
    for migration in await pending_migrations(database, package=package):
        applied_at = clock.utc_now()
        script = (
            "BEGIN;\n"
            f"{migration.sql}\n"
            "INSERT INTO schema_migrations (version, name, sha256, applied_at) VALUES "
            f"({migration.version}, '{migration.name}', '{migration.sha256}', '{applied_at}');\n"
            "COMMIT;"
        )
        try:
            await conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise
        applied_names.append(migration.name)
    return applied_names
