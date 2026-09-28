"""Real implementations for the CLI subcommands: `db upgrade`, `export`, `delete`, `purge`
(M1), `activate` (M5), `serve` (M5, extended in M9 with the website), and the
pending-migrations check `serve` uses before it starts.

`catalog check` and `llm eval` live in their own modules (`cli/catalog_check.py`,
`cli/llm_eval.py`). This module never imports `db/selectors/` or `db/controllers/` directly
(A§2.1: bot/web/cli call into `services/`, not `db/`) — `db/migrate.py`,
`db/connection.py::open_database` and `db/backup.py::create_backup` (M10) are the sanctioned
exceptions the plan calls out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import uvicorn
from aiogram import Bot
from aiogram.exceptions import AiogramError, TelegramUnauthorizedError

from fitme.bot.app import build_dispatcher
from fitme.bot.commands import register_commands
from fitme.config.settings import Settings, SettingsError
from fitme.db.backup import BackupError, create_backup
from fitme.db.connection import Database, DatabaseUnavailableError, open_database
from fitme.db.migrate import migrate, pending_migrations
from fitme.llm import models as llm_models
from fitme.services import history_import as history_import_service
from fitme.services.account import delete_user, export_user, get_single_user
from fitme.services.identity import issue_activation_code
from fitme.services.llm_runtime import LlmRuntime
from fitme.services.operator import clear_holds_from_cli, open_holds_for_cli
from fitme.services.retention import purge as run_retention
from fitme.web.app import create_app
from fitme.web.security import validate_web_config

_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
HOLD_CLEAR_CONFIRMATION = "CLEAR"

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


async def health(settings: Settings) -> int:
    """`fitme health` (A§11, M10): the container healthcheck. Confirms the LLM model
    configuration resolves, the web configuration is valid, the database opens and has no
    pending migrations — the same checks `serve` itself refuses to start without. Prints one
    line and exits 0 (healthy) or 1 (not), same as `serve`'s own startup checks. No network
    call is made: `llm_models.validate_startup` only parses model strings and reads env vars
    (see its own docstring)."""
    try:
        llm_models.validate_startup(settings)
    except SettingsError as exc:
        print(f"unhealthy: invalid LLM model configuration: {exc}", file=sys.stderr)
        return 1

    try:
        validate_web_config(settings)
    except SettingsError as exc:
        print(f"unhealthy: invalid web configuration: {exc}", file=sys.stderr)
        return 1

    if not settings.db_path.exists():
        print("unhealthy: database not initialized", file=sys.stderr)
        return 1

    pending = await pending_migration_names(settings)
    if pending is None:
        return 1  # pending_migration_names already printed a friendly reason
    if pending:
        print(f"unhealthy: pending migrations: {', '.join(pending)}", file=sys.stderr)
        return 1

    print("ok")
    return 0


async def backup(settings: Settings, out_dir: str, *, keep: int) -> int:
    """`fitme backup --out DIR [--keep N]` (A§11, M10): an online, consistent backup via
    SQLite's own backup API, rotating to keep only the newest `keep` (default 14)."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        try:
            result = await create_backup(db, Path(out_dir), keep=keep)
        except BackupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        await db.close()
    print(f"Backup written to {result.path}")
    if result.removed:
        print(f"Rotated out {len(result.removed)} old backup(s).")
    return 0


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


async def history_import(settings: Settings, path: str, *, dry_run: bool) -> int:
    """`fitme history import PATH [--dry-run]` (M11, docs/import-format.md): import past
    trainings and plans from a TOML/JSON file, or from standard input when PATH is `-`
    (`docker compose exec -T fitme fitme history import - < my.import.toml`). One
    transaction; a dry run prints the same report and writes nothing."""
    if path == "-":
        text = sys.stdin.read()
        source = "stdin"
        path_hint: str | None = None
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Cannot read {path}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        source = Path(path).name
        path_hint = path
    fmt = history_import_service.detect_format(text, path_hint)

    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        report = await history_import_service.import_history(
            db, settings, text, fmt=fmt, source=source, dry_run=dry_run
        )
    except history_import_service.ImportFormatError as exc:
        print(f"Import failed, nothing written: {exc}", file=sys.stderr)
        return 1
    except history_import_service.NoUserError:
        print("No user configured yet; run `fitme activate` first.", file=sys.stderr)
        return 1
    finally:
        await db.close()

    label = "Dry run (nothing written)" if report.dry_run else "Imported"
    print(
        f"{label}: {report.sessions_new} new session(s), "
        f"{report.sessions_duplicate} duplicate session(s) skipped, "
        f"{len(report.plans_saved)} plan(s) saved, "
        f"{report.plans_duplicate} duplicate plan(s) skipped, "
        f"{len(report.rejected)} rejected."
    )
    for plan in report.plans_saved:
        print(f"  plan saved: {plan.name} (plan {plan.plan_id}, version {plan.plan_version_id})")
        for line in plan.substitutions:
            print(f'  plan "{plan.name}": {line}')
    print(
        f"  {report.sessions_linked} session(s) linked to a plan workout, "
        f"{report.sessions_unlinked} new session(s) left unlinked."
    )
    for warning in report.link_warnings:
        print(f"  {warning}")
    for exercise_id, kg in report.per_implement_max:
        print(f"  {exercise_id}: highest imported load {kg:g} kg each (per dumbbell/kettlebell)")
    if report.newer_than_last_logged:
        print(
            f"  {report.newer_than_last_logged} session(s) are newer than your last logged "
            "training; they become the current working load (held, not increased, until "
            "you log a session through the app)."
        )
    if report.unknown_exercises:
        print("  unknown exercises (add them to [aliases]): " + ", ".join(report.unknown_exercises))
    for item in report.rejected:
        print(f"  rejected {item.ref}: {item.reason}")
    if report.decision_id is not None:
        print(f"Decision {report.decision_id} recorded (file sha256 {report.file_sha256[:12]}).")
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


def _prompt_on_tty(prompt: str) -> str | None:
    """Read one line from an interactive terminal, or return None when stdin isn't one
    (a cron job or a pipe can't type a confirmation)."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except EOFError:
        return None


async def hold_clear(
    settings: Settings,
    *,
    confirmed: bool,
    prompt: Callable[[str], str | None] = _prompt_on_tty,
) -> int:
    """`fitme hold clear [--yes]` (A§6.6): the operator's only bypass for a health hold, meant
    for stop-word false positives. Lists the open holds, then clears them all, each logged as
    a `hold_clear` decision with `source=operator_cli`. Without `--yes` it asks the operator
    to type `CLEAR`; non-interactive without `--yes` refuses."""
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        holds = await open_holds_for_cli(db)
        if not holds:
            print("No open holds; nothing to clear.")
            return 0
        print(f"{len(holds)} open hold(s):")
        for hold in holds:
            print(f"  hold {hold.id}: reason={hold.reason}, created {hold.created_at}")
        if not confirmed:
            answer = prompt(f"Type {HOLD_CLEAR_CONFIRMATION} to clear them all: ")
            if answer is None:
                print(
                    "Refusing to clear holds: not an interactive terminal and no --yes.",
                    file=sys.stderr,
                )
                return 1
            if answer.strip() != HOLD_CLEAR_CONFIRMATION:
                print("Refusing to clear holds: confirmation did not match.", file=sys.stderr)
                return 1
        cleared = await clear_holds_from_cli(db)
    finally:
        await db.close()
    for item in cleared:
        print(f"Cleared hold {item.hold_id} (decision {item.decision_id}, source=operator_cli).")
    print(f"Cleared {len(cleared)} hold(s).")
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


_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class _SilentSignalsServer(uvicorn.Server):
    """`uvicorn.Server.capture_signals()` installs its own SIGINT/SIGTERM handlers, and,
    critically, **restores the original handler and re-raises the signal** the moment its own
    `should_exit` loop notices the signal and returns (see the uvicorn source: `for
    captured_signal in reversed(self._captured_signals): signal.raise_signal(captured_signal)`).
    For SIGTERM, the original handler is the process default — terminate — so the re-raised
    signal kills the process immediately, skipping every `finally` in `serve_async` (no DB
    close, the bot polling task never gets a chance to be cancelled): M9 review fix B3.

    This override makes signal handling entirely `serve_async`'s job instead: `capture_signals`
    becomes a no-op, and `serve_async` installs its own handlers via
    `loop.add_signal_handler`, which just set `should_exit` directly and never re-raise
    anything.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _serve_concurrently(
    *,
    polling: Coroutine[Any, Any, None],
    web_serve: Coroutine[Any, Any, None],
    retention_task: asyncio.Task[None],
) -> None:
    """A§3 "one process": the bot (long polling) and the website run as two tasks in this one
    event loop. Whichever exits first (normally: `serve_async`'s own signal handler setting
    `server.should_exit`, since the bot polling is started with `handle_signals=False`
    precisely so the two don't fight over the same signal) stops the other, then the
    retention loop — graceful shutdown of all three, in that order. A genuine crash in either
    (not a cancellation) propagates via `task.result()`."""
    polling_task = asyncio.create_task(polling)
    web_task = asyncio.create_task(web_serve)
    try:
        done, pending = await asyncio.wait(
            {polling_task, web_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        _logger.info("bot polling stopped")
        _logger.info("web server stopped")
        for task in done:
            task.result()
    finally:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        _logger.info("retention loop stopped")


async def serve_async(settings: Settings) -> int:
    """`fitme serve` (A§3, A§9, A§11): opens the database once (closed last, so the process
    exits cleanly on a signal or an error), then runs the bot (long polling), the website
    (uvicorn) and the daily retention job in this one event loop (`_serve_concurrently`).

    Signal handling (M9 review fix B3) is installed here, once, via
    `loop.add_signal_handler`: SIGINT/SIGTERM just set `server.should_exit = True`, which
    `_SilentSignalsServer` (see its docstring) never turns into a re-raised signal. Setting
    `should_exit` makes `server.serve()` return on its own, which `_serve_concurrently`
    already treats like any other "one of the two finished" case: the bot polling task (never
    given its own signal handling, `handle_signals=False`) gets cancelled right alongside it.
    """
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # A§10; also set at CLI entry
    db = await _try_open_database(settings)
    if db is None:
        return 1
    try:
        bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        try:
            # M10 review (B4): a rejected token must fail fast with one clear line, not a
            # crash-loop — the same "refuse to start" shape `_serve()` already uses for an
            # invalid LLM/web config, so a bad FITME_TELEGRAM_BOT_TOKEN under Docker Compose's
            # `restart: unless-stopped` doesn't just spam tracebacks forever. `bot.me()` (not
            # the raw `get_me()`) so the result is cached: `dispatcher.start_polling` below
            # would otherwise repeat the exact same call. A transient network error here is
            # NOT fatal — aiogram's own polling loop retries those — only a definitive
            # rejection of the token itself is.
            try:
                await bot.me()
            except TelegramUnauthorizedError:
                print(
                    "Refusing to start: FITME_TELEGRAM_BOT_TOKEN was rejected by Telegram "
                    "(Unauthorized); fix .env and restart",
                    file=sys.stderr,
                )
                return 1
            except AiogramError:
                _logger.warning("could not verify the Telegram bot token at startup")

            llm = LlmRuntime.from_settings(settings)
            dispatcher = build_dispatcher(db, settings, llm)
            await register_commands(bot)

            async def send_code(chat_id: int, text: str) -> None:
                await bot.send_message(chat_id, text)

            web_app = create_app(db=db, settings=settings, llm=llm, send_code=send_code)
            server = _SilentSignalsServer(
                uvicorn.Config(
                    web_app,
                    host=settings.web_host,
                    port=settings.web_port,
                    log_config=None,
                    # M10: only meaningful behind the optional `caddy` reverse proxy —
                    # `forwarded_allow_ips` is the peer address(es)/CIDR uvicorn trusts to set
                    # X-Forwarded-*; see `Settings.forwarded_allow_ips` for the default and
                    # compose.yaml for how the `tls` profile sets it.
                    proxy_headers=True,
                    forwarded_allow_ips=settings.forwarded_allow_ips,
                )
            )

            loop = asyncio.get_running_loop()
            installed_signals: list[signal.Signals] = []
            for sig in _SHUTDOWN_SIGNALS:
                try:
                    loop.add_signal_handler(sig, _request_shutdown, server, sig)
                except NotImplementedError:
                    break  # e.g. Windows: no event-loop signal handlers: default handling
                installed_signals.append(sig)

            retention_task = asyncio.create_task(
                _retention_loop(db, chat_retention_days=settings.chat_retention_days)
            )
            try:
                await _serve_concurrently(
                    polling=dispatcher.start_polling(bot, handle_signals=False),
                    web_serve=server.serve(),
                    retention_task=retention_task,
                )
            finally:
                for sig in installed_signals:
                    loop.remove_signal_handler(sig)
        finally:
            await bot.session.close()
    finally:
        await db.close()
        _logger.info("database closed")
    return 0


def _request_shutdown(server: uvicorn.Server, sig: signal.Signals) -> None:
    _logger.info("received shutdown signal", extra={"signal": sig.name})
    server.should_exit = True
