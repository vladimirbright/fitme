"""Full-account export (A§8.3): every row in every table for the one user, grouped by table.

Table names are dynamic here (the identifier, not the value, varies per table), which A§4.6
rule 3 allows only from a hard-coded tuple in the same module — never from user input.
Unlike the rest of `selectors/`, rows here are returned as plain JSON-shaped dicts rather
than a specific typed record, since the whole point is to dump every column, generically,
for every table.
"""

from __future__ import annotations

import aiosqlite

ExportedRow = dict[str, object]

# Kept in this module rather than imported from controllers/account.py (A§4.6 rule 3: a
# hard-coded tuple lives in the *same* module as the query that uses it). The two lists must
# stay in sync; the export/delete integration test derives the full table list from
# sqlite_master and would fail if a table were missing from either.
_TABLES_BY_USER_ID: tuple[str, ...] = (
    "telegram_accounts",
    "login_codes",
    "web_sessions",
    "profiles",
    "setup_progress",
    "screening_flags",
    "screening_notes",
    "health_holds",
    "workout_sessions",
    "checkins",
    "chat_messages",
    "plans",
    "decisions",
)

# No user_id column at all. Single-user instance (ADR 0002): every row already belongs to
# the one user.
_TABLES_WITHOUT_USER_ID_SINGLE_USER_ONLY: tuple[str, ...] = (
    "set_logs",
    "plan_versions",
    "decision_outcomes",
    "llm_calls",
)

_INSTANCE_WIDE_TABLES: tuple[str, ...] = ("activation_codes", "activation_state")

# M9 review ("ALSO" #11): a column omitted from the export entirely, because a SHA-256 hash of
# a one-time code or a session id tells the user nothing about their own account — it's
# meaningless without the original value, which is never stored (A§4.1). Dropping it here is
# plain data minimization (AGENTS.md §5), even though the hash alone can't be used to derive
# the original code or forge a session. Keyed by table name; every table not listed here is
# exported in full.
_OMIT_COLUMNS: dict[str, frozenset[str]] = {
    "login_codes": frozenset({"code_hash"}),
    "web_sessions": frozenset({"id_hash"}),
    "activation_codes": frozenset({"code_hash"}),
}


async def _table_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:  # nosec: hard-coded tuple
        rows = await cursor.fetchall()
    return [str(row[1]) for row in rows]


async def _select_columns(conn: aiosqlite.Connection, table: str) -> str:
    """`table`'s columns, comma-joined, minus anything in `_OMIT_COLUMNS` — `SELECT *` for any
    table with no entry there."""
    omit = _OMIT_COLUMNS.get(table)
    if not omit:
        return "*"
    columns = [name for name in await _table_columns(conn, table) if name not in omit]
    return ", ".join(columns)


async def export_all(conn: aiosqlite.Connection, user_id: int) -> dict[str, list[ExportedRow]]:
    """Every row belonging to this user, keyed by table name.

    Call this inside `Database.read()`, so the whole dump is one consistent snapshot
    (A§4.2: `read()` opens `BEGIN DEFERRED`). `conn.row_factory` is `aiosqlite.Row` for the
    life of the connection (set once in `Database.connect()`), so `dict(row)` works here
    without touching row_factory.
    """
    result: dict[str, list[ExportedRow]] = {}

    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
        result["users"] = [dict(row) async for row in cursor]

    for table in _TABLES_BY_USER_ID:
        columns = await _select_columns(conn, table)
        async with conn.execute(
            f"SELECT {columns} FROM {table} WHERE user_id = ?",
            (user_id,),  # nosec: hard-coded tuple
        ) as cursor:
            result[table] = [dict(row) async for row in cursor]

    # Single-user instance (ADR 0002): every row in these tables already belongs to the
    # one user, since none of them carry a user_id column at all.
    for table in _TABLES_WITHOUT_USER_ID_SINGLE_USER_ONLY + _INSTANCE_WIDE_TABLES:
        columns = await _select_columns(conn, table)
        async with conn.execute(f"SELECT {columns} FROM {table}") as cursor:  # nosec
            result[table] = [dict(row) async for row in cursor]

    return result
