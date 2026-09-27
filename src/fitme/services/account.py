"""Export and delete a user's data (A§8.3), used by both `/export`/`/delete` and the CLI.

`get_single_user` is the only way anything outside `db/` reads the user row: bot, web and
cli code call into `services/`, never into `db/selectors/` directly (A§2.1).
"""

from __future__ import annotations

from fitme.db.connection import Database
from fitme.db.controllers.account import delete_user_data, vacuum, wal_checkpoint_truncate
from fitme.db.records import UserRecord
from fitme.db.selectors.account import ExportedRow, export_all
from fitme.db.selectors.users import get_the_user


async def get_single_user(db: Database) -> UserRecord | None:
    """The one user row this instance ever has (ADR 0002), or None before activation."""
    async with db.read() as conn:
        return await get_the_user(conn)


async def export_user(db: Database, user_id: int) -> dict[str, list[ExportedRow]]:
    """Every row in every table for this user, grouped by table name."""
    async with db.read() as conn:
        return await export_all(conn, user_id)


async def delete_user(db: Database, user_id: int) -> None:
    """Delete every row for this user, then reclaim the freed space.

    VACUUM cannot run inside a transaction, so it happens after the delete transaction has
    committed, as its own statement. `wal_checkpoint(TRUNCATE)` follows so the deleted data
    doesn't linger in the WAL file either.
    """
    async with db.transaction() as conn:
        await delete_user_data(conn, user_id)
    async with db.exclusive() as conn:
        await vacuum(conn)
        await wal_checkpoint_truncate(conn)
