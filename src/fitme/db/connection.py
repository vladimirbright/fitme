"""SQLite connection setup and transaction helpers (A§3, A§4.6).

One `Database` owns one `aiosqlite.Connection`, configured with WAL, foreign keys and a busy
timeout. Services get a `Database` and use `transaction()` for read/write work and `read()`
for read-only work; nothing outside `db/` opens a connection or commits directly.

A single SQLite connection is not safe for interleaved use from concurrent asyncio tasks: two
coroutines both issuing `BEGIN` on it race ("cannot start a transaction within a transaction"),
and a `read()` running while a `transaction()` is mid-flight could otherwise see uncommitted
writes. `Database` serializes all access to the connection with one `asyncio.Lock`, held for
the whole body of both `transaction()` and `read()`. `read()` also opens its own
`BEGIN DEFERRED ... COMMIT` so a read sees one consistent snapshot rather than statement by
statement.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

_BUSY_TIMEOUT_MS = 5000


class DatabaseUnavailableError(RuntimeError):
    """The database file can't be opened (e.g. its directory doesn't exist).

    Callers outside `db/` catch this instead of `sqlite3.OperationalError` directly, so
    `sqlite3` stays an implementation detail of `db/` (A§4.6 rule 1).
    """


class Database:
    """Owns one aiosqlite connection for the lifetime of the process."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        # The task currently holding `_lock` through transaction()/read()/exclusive(), or
        # None. `asyncio.Lock` is not reentrant: a task that calls one of these again while
        # it already holds the lock would await its own release forever (a silent deadlock),
        # since nothing else runs on that task to ever call `release()`. Tracking the owner
        # lets us detect that *before* awaiting the lock and raise instead of hanging.
        self._owner: asyncio.Task[object] | None = None

    async def connect(self) -> None:
        """Open the connection and apply the required PRAGMAs."""
        # isolation_level=None: autocommit mode. transaction()/read() below issue BEGIN and
        # COMMIT/ROLLBACK by hand instead of relying on sqlite3's implicit transactions.
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        # Set once, for the life of the connection. Selectors rely on column-name access
        # (and `dict(row)` for the generic export); never swap this per call, since another
        # task could be mid-query on the same shared connection.
        conn.row_factory = aiosqlite.Row
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def raw(self) -> aiosqlite.Connection:
        """The underlying connection. Only `db/` and migration/CLI code should need this."""
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been called yet")
        return self._conn

    def _enter_unit_of_work(self) -> asyncio.Task[object] | None:
        """Return the current task, after checking it isn't the one already holding the
        lock. Called *before* `async with self._lock`, so a same-task re-entry raises
        instead of awaiting a lock it can never release itself (see `__init__`).
        """
        current = asyncio.current_task()
        if current is not None and current is self._owner:
            raise RuntimeError("nested unit of work")
        return current

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """A read/write unit of work: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on error.

        Serialized against every other transaction() and read() on this Database. Raises
        `RuntimeError` instead of deadlocking if the same task calls transaction()/read()/
        exclusive() again while already inside one of them.
        """
        conn = self.raw
        current = self._enter_unit_of_work()
        async with self._lock:
            self._owner = current
            try:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    await conn.execute("ROLLBACK")
                    raise
                else:
                    await conn.execute("COMMIT")
            finally:
                self._owner = None

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """A read-only unit of work: BEGIN DEFERRED, so the whole block sees one consistent
        snapshot, then COMMIT (or ROLLBACK on error; equivalent here, since nothing is
        written). Serialized against every other transaction() and read() on this Database,
        so a read can never observe another task's uncommitted writes. Raises `RuntimeError`
        instead of deadlocking on same-task re-entry (see `transaction()`).
        """
        conn = self.raw
        current = self._enter_unit_of_work()
        async with self._lock:
            self._owner = current
            try:
                await conn.execute("BEGIN DEFERRED")
                try:
                    yield conn
                except BaseException:
                    await conn.execute("ROLLBACK")
                    raise
                else:
                    await conn.execute("COMMIT")
            finally:
                self._owner = None

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[aiosqlite.Connection]:
        """Exclusive access to the connection for a statement that must run outside any
        transaction (`VACUUM`, `PRAGMA wal_checkpoint`), but still needs to be serialized
        against `transaction()`/`read()` rather than racing them on the shared connection.
        Same same-task re-entry check as `transaction()`/`read()`.
        """
        current = self._enter_unit_of_work()
        async with self._lock:
            self._owner = current
            try:
                yield self.raw
            finally:
                self._owner = None


async def open_database(path: str | Path) -> Database:
    """Open and configure a Database. Callers are responsible for calling `close()`.

    Raises `DatabaseUnavailableError` (never a raw `sqlite3` exception) if the file can't be
    opened, e.g. because its directory doesn't exist.
    """
    database = Database(path)
    try:
        await database.connect()
    except sqlite3.OperationalError as exc:
        raise DatabaseUnavailableError(f"can't open the database at {path}: {exc}") from exc
    return database
