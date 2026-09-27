"""Full-account delete (A§8.3). The one exception to "append-only": this is the only place
allowed to delete rows from `decisions`/`decision_outcomes`/`plan_versions` (A§4.6 rule 5).

Order matters: `plan_versions.decision_id` and `workout_sessions.plan_version_id` are
`ON DELETE RESTRICT` (a plan edit must never silently wipe training history), so those two
edges don't cascade. True leaves go first, then `workout_sessions`, then `plan_versions`,
then `decisions`, then everything else, then `users`. Every table name below is a literal in
the module's own source, not built from a variable, so there is no dynamic-identifier
concern here (contrast `db/selectors/account.py`, which does loop over a hard-coded tuple).
"""

from __future__ import annotations

import aiosqlite


async def delete_user_data(conn: aiosqlite.Connection, user_id: int) -> None:
    """Delete every row for this user, across every table (A§8.3)."""
    # Leaves with no dependents of their own: safe to remove first regardless of FK
    # configuration. Single-user instance (ADR 0002): these tables have no user_id column,
    # so every row already belongs to the one user; activation_codes has no user link at all.
    await conn.execute("DELETE FROM set_logs")
    await conn.execute("DELETE FROM llm_calls")
    await conn.execute("DELETE FROM decision_outcomes")
    await conn.execute("DELETE FROM activation_codes")

    # workout_sessions has nothing RESTRICTing against it (health_holds/checkins are
    # SET NULL, chat_messages is CASCADE), so it can go next, before plan_versions.
    await conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM checkins WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM health_holds WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM workout_sessions WHERE user_id = ?", (user_id,))

    # Now nothing references plan_versions (its RESTRICT from workout_sessions is
    # satisfied), so it can go, then decisions (its RESTRICT from plan_versions is
    # satisfied too).
    await conn.execute("DELETE FROM plan_versions")
    await conn.execute("DELETE FROM plans WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM decisions WHERE user_id = ?", (user_id,))

    await conn.execute("DELETE FROM screening_notes WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM screening_flags WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM login_codes WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM telegram_accounts WHERE user_id = ?", (user_id,))
    await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


async def vacuum(conn: aiosqlite.Connection) -> None:
    """Reclaim freed space after a delete (A§8.3). Must run outside any transaction."""
    await conn.execute("VACUUM")


async def wal_checkpoint_truncate(conn: aiosqlite.Connection) -> None:
    """Fold the WAL file back into the main db file and truncate it to zero bytes, so
    deleted data doesn't linger on disk in the -wal file after a delete."""
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
