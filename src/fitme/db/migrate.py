"""Applies `db/migrations/*.sql` in order (A§4.7).

Migration files are plain, forward-only SQL, numbered `NNNN_name.sql`. There are no down
migrations: restore a backup instead. Each file's checksum is recorded in
`schema_migrations`, in the same transaction as the migration itself, so a crash partway
through never leaves an applied-but-unrecorded (or recorded-but-unapplied) migration.

`migrate()` runs once, at process startup (`fitme db upgrade`, or `fitme serve`'s startup
check), before the bot or web server accepts any concurrent work. It reads and writes
`database.raw` directly and never goes through `Database.transaction()`/`read()`/
`exclusive()` — there is deliberately no concurrent access to serialize against yet.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import re
import sqlite3
from dataclasses import dataclass

import aiosqlite

from fitme import clock
from fitme.db.connection import Database

_MIGRATIONS_PACKAGE = "fitme.db.migrations"
_FILENAME_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# A migration file must not contain its own transaction-control statements: `migrate()` wraps
# each file in one transaction of its own (A§4.7), so a file that issues these would either
# nest transactions (another `BEGIN`) or terminate the wrapper's transaction early (`COMMIT`/
# `ROLLBACK`/a stray `END`). Checked per *whole statement* (see `_split_statements`), by its
# first token, not by a regex search over the raw text: a `CASE ... END` expression inside an
# ordinary `CREATE VIEW`/`SELECT` (nothing to do with a transaction) contains the word `END`
# too, but that statement's first token is `CREATE`/`SELECT`, not `END`, so it's never
# flagged. Case-insensitive: SQL keywords aren't case-sensitive, and a mistake spelled
# lowercase is just as real.
_FORBIDDEN_FIRST_TOKENS = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "END"})
_LEADING_TOKEN_RE = re.compile(r"^([A-Za-z]+)")


def _strip_comments(sql: str) -> str:
    """Remove `--` line comments and `/* */` block comments. Comments carry no execution
    meaning, so dropping them is safe; it just keeps `_first_token` from tripping over a
    leading comment line."""
    without_line_comments = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.DOTALL)


def _first_token(statement: str) -> str | None:
    stripped = _strip_comments(statement).strip()
    if not stripped:
        return None
    match = _LEADING_TOKEN_RE.match(stripped)
    return match.group(1).upper() if match else None


def _check_no_forbidden_transaction_statements(statements: list[str], name: str) -> None:
    for statement in statements:
        token = _first_token(statement)
        if token in _FORBIDDEN_FIRST_TOKENS:
            raise MigrationError(
                f"{name} contains a top-level {token} statement; migrations must not manage "
                "their own transactions (`migrate()` wraps each file in one transaction of "
                "its own, A§4.7). A `CREATE TRIGGER ... BEGIN ... END` body is fine (its "
                "first token is `CREATE`); a bare BEGIN/COMMIT/ROLLBACK/END statement is not."
            )


def _split_statements(sql: str, name: str) -> list[str]:
    """Split a migration script into individual statements by accumulating characters until
    `sqlite3.complete_statement()` reports a complete one, then enforcing A§4.7's "one SQL
    statement per line-group; never put two statements on one line".

    This is the stdlib's own wrapper around SQLite's C-level `sqlite3_complete()`: "a
    statement is judged to be complete if it ends with a semicolon token and is not preceded
    by an unmatched BEGIN keyword" (SQLite docs). That means it already understands `'...'`
    string literals and `--`/`/* */` comments (a `;` or `--` inside a string doesn't count),
    *and* it tracks `BEGIN ... END` nesting generically — so a `CREATE TRIGGER ... BEGIN
    ... END` body, whose own statements are `;`-terminated, is correctly read as a single
    statement rather than split apart at its first internal `;` (verified against SQLite's
    real behavior, including a `CASE ... END` nested inside the trigger body, which does not
    prematurely close the outer `BEGIN`).

    Accumulating character by character (not line by line) means two statements terminated on
    the same physical line are still correctly recognized as *two* statements — which is
    exactly what lets this function then reject that case explicitly, instead of handing
    `conn.execute()` a two-statement string and letting it fail with a raw, confusing
    `sqlite3.ProgrammingError` at apply time.

    Fed the raw file text (not comment-stripped): stripping comments first would only risk
    disagreeing with what SQLite itself considers a comment. Raises `MigrationError` if
    anything is left over that isn't just trailing whitespace/comments — most commonly a
    missing final `;`.
    """
    statements: list[str] = []
    spans: list[tuple[int, int]] = []
    buffer = ""
    start = 0
    for offset, ch in enumerate(sql, start=1):
        buffer += ch
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            spans.append((start, offset))
            buffer = ""
            start = offset
    if _strip_comments(buffer).strip():
        raise MigrationError(
            f"{name}: trailing content is not a complete, semicolon-terminated SQL "
            f"statement: {buffer.strip()!r}"
        )

    for (_, prev_end), (next_start, next_end) in zip(spans, spans[1:], strict=False):
        # Skip whitespace/comments between the two statements: what matters is where the
        # *next statement's own content* starts, not the raw boundary (which usually lands
        # on the newline or blank line right after the previous statement's `;`).
        content_start = _skip_whitespace_and_comments(sql, next_start, next_end)
        if content_start >= next_end:
            continue  # defensive; shouldn't happen for a span that closed a statement
        if _line_of(sql, prev_end - 1) == _line_of(sql, content_start):
            raise MigrationError(
                f"{name}: two SQL statements share one line (A§4.7: one statement per "
                "line-group; never put two statements on one line): "
                f"{sql[max(0, content_start - 40) : content_start + 40].strip()!r}"
            )
    return statements


def _line_of(sql: str, offset: int) -> int:
    """1-based line number containing `sql[offset]` (or where `offset` would fall, for an
    offset at end-of-string)."""
    return sql.count("\n", 0, offset) + 1


def _skip_whitespace_and_comments(sql: str, pos: int, end: int) -> int:
    """The offset of the first real (non-whitespace, non-comment) character at or after
    `pos`, within `sql[:end]`. Used to find where a statement's own content actually starts,
    skipping any blank lines or comments left over from the previous statement's boundary."""
    while pos < end:
        if sql[pos] in " \t\r\n":
            pos += 1
        elif sql.startswith("--", pos):
            newline = sql.find("\n", pos)
            pos = end if newline == -1 or newline >= end else newline + 1
        elif sql.startswith("/*", pos):
            close = sql.find("*/", pos + 2)
            pos = end if close == -1 or close + 2 > end else close + 2
        else:
            break
    return pos


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
    statements: tuple[str, ...]


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
        statements = tuple(_split_statements(sql, entry.name))
        _check_no_forbidden_transaction_statements(list(statements), entry.name)
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        files.append(
            MigrationFile(
                version=int(match.group(1)),
                name=entry.name,
                sql=sql,
                sha256=checksum,
                statements=statements,
            )
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

    Each migration runs in its own transaction: `PRAGMA foreign_keys = OFF` (this pragma is a
    no-op inside a transaction, so it has to happen first), `BEGIN`, the file's statements one
    by one, `PRAGMA foreign_key_check`, an INSERT recording the file in `schema_migrations`,
    then `COMMIT` — or `ROLLBACK` if any step fails, including a non-empty
    `foreign_key_check` result. Foreign keys are off for the duration because a STRICT-table
    rebuild (dropping and recreating a table to change a column) would otherwise cascade-
    delete every child row the moment the old table is dropped (A§4.7); `foreign_key_check`
    is the deferred, whole-database integrity check that catches what per-statement
    enforcement would otherwise have caught immediately. `PRAGMA foreign_keys = ON` is
    restored unconditionally afterwards, success or failure, so every other unit of work
    keeps enforcing it as `Database.connect()` set it up to do.
    """
    conn = database.raw
    applied_names: list[str] = []
    for migration in await pending_migrations(database, package=package):
        applied_at = clock.utc_now()
        await conn.execute("PRAGMA foreign_keys = OFF")
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in migration.statements:
                    await conn.execute(statement)

                async with conn.execute("PRAGMA foreign_key_check") as cursor:
                    violations = list(await cursor.fetchall())
                if violations:
                    raise MigrationError(
                        f"{migration.name} leaves {len(violations)} foreign-key "
                        "violation(s); rolled back. Foreign keys are off while a migration "
                        "runs (A§4.7), so a violation is only caught here, not at the "
                        "statement that caused it."
                    )

                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, sha256, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (migration.version, migration.name, migration.sha256, applied_at),
                )
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")
        finally:
            await conn.execute("PRAGMA foreign_keys = ON")
        applied_names.append(migration.name)
    return applied_names
