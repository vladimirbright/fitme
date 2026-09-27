"""Real implementations for the CLI subcommands: `db upgrade`, `export`, `delete`, `purge`
(M1), `activate` and `serve` (M5), and the pending-migrations check `serve` uses before it
starts.

`catalog check` and `llm eval` live in their own modules (`cli/catalog_check.py`,
`cli/llm_eval.py`); the web app (M9) is still to come. This module never imports
`db/selectors/` or `db/controllers/` directly (A§2.1: bot/web/cli call into `services/`, not
`db/`) — `db/migrate.py` and `db/connection.py::open_database` are the sanctioned exceptions
the plan calls out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys

from aiogram import Bot

from fitme.bot.app import build_dispatcher
from fitme.bot.commands import register_commands
from fitme.config.settings import Settings
from fitme.db.connection import Database, DatabaseUnavailableError, open_database
from fitme.db.migrate import migrate, pending_migrations
from fitme.services.account import delete_user, export_user, get_single_user
from fitme.services.identity import issue_activation_code
from fitme.services.retention import purge as run_retention

_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60

_logger = logging.getLogger(__name__)


async def _try_open_database(settings: Settings) -> Database | None:
    """Open the configured database, printing a friendly message instead of a traceback if
    it can't be opened (most commonly: FITME_DB_PATH's directory doesn't exist)."""
    try:
        return await open_database(settings.db_path)
    except DatabaseUnavailableError as exc:
        print(f"{exc}\nCheck that its directory exists.", file=sys.stderr)
        return None


async def db_upgrade(settings: Settings) -> int:
    """Apply pending SQL migrations (A§11)."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        applied = await migrate(db)
    finally:
        await db.close()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Already up to date.")
    return 0


async def pending_migration_names(settings: Settings) -> list[str] | None:
    """Names of migrations not yet applied, without applying them. Used by `serve` (A§4.7:
    "fitme serve refuses to start while any are pending"). Returns None (after printing a
    friendly error) if the database can't be opened; `serve` itself checks the file exists
    first, so this doesn't create a database file just to answer the question."""
    db = await _try_open_database(settings)
    if db is None:
        return None
    try:
        pending = await pending_migrations(db)
    finally:
        await db.close()
    return [migration.name for migration in pending]


async def export_data(settings: Settings, out_path: str) -> int:
    """Dump all user data as JSON (A§8.3, A§11). The file is written 0600: it holds health
    data (A§5)."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        user = await get_single_user(db)
        if user is None:
            print("No user configured yet; run `fitme activate` first.", file=sys.stderr)
            return 1
        data = await export_user(db, user.id)
    finally:
        await db.close()

    payload = json.dumps(data, indent=2, default=str)
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode passed to os.open() only applies when the file is newly created; overwriting
    # an existing file (e.g. left over at 0644 from outside this command) keeps its old
    # permissions, so fchmod it explicitly. The file holds health data (A§5).
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)

    row_count = sum(len(rows) for rows in data.values())
    print(f"Exported {row_count} row(s) across {len(data)} table(s) to {out_path}")
    return 0


async def delete_account(settings: Settings, *, confirmed: bool) -> int:
    """Delete the user and all data (A§8.3, A§11). Refuses without explicit confirmation."""
    if not confirmed:
        print("Refusing to delete without --yes.", file=sys.stderr)
        return 1
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        user = await get_single_user(db)
        if user is None:
            print("No user configured yet; nothing to delete.", file=sys.stderr)
            return 1
        await delete_user(db, user.id)
    finally:
        await db.close()
    print("Deleted the user and all associated data.")
    return 0


async def purge_now(settings: Settings) -> int:
    """Run the retention job once, immediately (A§8.4, A§11)."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        result = await run_retention(db, chat_retention_days=settings.chat_retention_days)
    finally:
        await db.close()
    print(
        "Purged "
        f"{result.chat_messages_deleted} chat message(s), "
        f"{result.activation_codes_deleted} activation code(s), "
        f"{result.login_codes_deleted} login code(s), "
        f"{result.web_sessions_deleted} web session(s)."
    )
    return 0


async def activate(settings: Settings, *, rebind: bool) -> int:
    """`fitme activate [--rebind]` (A§6.1, A§11): prints a one-time activation code, valid
    for 15 minutes. `--rebind` unlinks the currently bound Telegram account first."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        code = await issue_activation_code(db, rebind=rebind)
    finally:
        await db.close()
    print(f"Activation code (valid 15 minutes): {code}")
    print(f"Send it to the bot: /activate {code}")
    return 0


async def _retention_once(db: Database, *, chat_retention_days: int) -> None:
    """One retention pass. A failure is logged (event name only, never row content — A§10)
    and swallowed rather than propagated: a single bad run (e.g. a transient DB error) must
    not stop the daily job for good."""
    try:
        await run_retention(db, chat_retention_days=chat_retention_days)
    except Exception:
        _logger.exception("retention run failed")


async def _retention_loop(db: Database, *, chat_retention_days: int) -> None:
    """Runs immediately, then every 24 hours (A§3, A§8.4), until cancelled by `serve_async`'s
    shutdown."""
    while True:
        await _retention_once(db, chat_retention_days=chat_retention_days)
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)


async def serve_async(settings: Settings) -> int:
    """`fitme serve` (A§3, A§11): opens the database once (closed in `finally`, so the
    process exits cleanly on error), runs the bot (long polling) and the daily retention job
    in this one event loop, and stops gracefully on SIGINT/SIGTERM (aiogram's
    `start_polling` installs its own handlers for these by default)."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        try:
            dispatcher = build_dispatcher(db, settings)
            await register_commands(bot)
            retention_task = asyncio.create_task(
                _retention_loop(db, chat_retention_days=settings.chat_retention_days)
            )
            try:
                await dispatcher.start_polling(bot)
            finally:
                retention_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await retention_task
        finally:
            await bot.session.close()
    finally:
        await db.close()
    return 0
