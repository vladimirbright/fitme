"""Online, consistent SQLite backups (A§4.6, M10).

Uses SQLite's own backup API (`sqlite3.Connection.backup`, wrapped by aiosqlite as an
awaitable), so the reference deployment's image needs no `sqlite3` CLI binary, and a backup
can safely run while `fitme serve` has the database open concurrently (the backup API handles
its own locking against the source connection; it does not need our own `transaction()`).

Rotation (keeping only the newest N backups) is plain filesystem housekeeping, not SQL, but it
lives here too, next to the call that produces the files, so `cli/commands.py` only
orchestrates settings/printing (A§2.1: cli calls into `services`/the sanctioned `db`
exceptions, not raw SQL/file-format details of its own).
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fitme.clock import now
from fitme.db.connection import Database

_FILENAME_FORMAT = "fitme-%Y%m%d-%H%M%S.db"
_FILENAME_RE = re.compile(r"^fitme-\d{8}-\d{6}\.db$")


class BackupError(RuntimeError):
    """A backup could not be written (e.g. a filename collision within the same second)."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    removed: tuple[Path, ...]


async def create_backup(db: Database, out_dir: Path, *, keep: int = 14) -> BackupResult:
    """Write a new, consistent backup of the whole database into `out_dir`, mode 0600, then
    rotate old ones so only the newest `keep` remain (default 14). Returns the new file's path
    and the paths removed by rotation."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest_path = out_dir / now().strftime(_FILENAME_FORMAT)

    # os.open with O_CREAT | O_EXCL: the file is created with mode 0600 atomically, and a
    # same-second collision (two backups requested within one second) fails loudly instead of
    # silently overwriting a previous backup. sqlite3.connect() itself has no way to set the
    # mode a newly created file gets.
    try:
        fd = os.open(dest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BackupError(
            f"a backup file already exists at {dest_path} (two backups requested within the "
            "same second?); try again"
        ) from exc
    os.close(fd)

    dest_conn = sqlite3.connect(dest_path)
    try:
        async with db.exclusive() as conn:
            await conn.backup(dest_conn)
    finally:
        dest_conn.close()
    # sqlite3 may have rewritten the file (journal/page writes during the backup); re-assert
    # the mode rather than trust what it left behind.
    os.chmod(dest_path, 0o600)

    removed = _rotate(out_dir, keep=keep)
    return BackupResult(path=dest_path, removed=removed)


def _rotate(out_dir: Path, *, keep: int) -> tuple[Path, ...]:
    """Delete the oldest backups in `out_dir`, keeping only the newest `keep`. The filename
    format sorts lexically in chronological order, so no need to stat each file's mtime."""
    backups = sorted(
        (path for path in out_dir.iterdir() if path.is_file() and _FILENAME_RE.match(path.name)),
        key=lambda path: path.name,
    )
    stale = backups[:-keep] if keep > 0 else tuple(backups)
    for path in stale:
        path.unlink()
    return tuple(stale)


__all__ = ["BackupError", "BackupResult", "create_backup"]
