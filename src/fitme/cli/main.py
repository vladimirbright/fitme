"""The `fitme` command.

Subcommands mirror docs/ARCHITECTURE.md §11. `db upgrade`, `export`, `delete`, `purge`,
`catalog check`, `activate` and `serve` are implemented (M1, M3, M5); `llm eval` is
implemented too (M4), and `hold clear` is the operator's hold bypass (A§6.6). `db <other>`
and `catalog <other>` subcommands remain stubs, since no other subcommand under those groups
exists yet.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fitme.cli import commands, llm_eval
from fitme.cli.catalog_check import catalog_check
from fitme.config.settings import Settings, SettingsError, load_settings
from fitme.llm import models as llm_models
from fitme.log import configure_logging

_NOT_IMPLEMENTED = "not implemented yet"


def _stub(command: str) -> int:
    print(f"fitme {command}: {_NOT_IMPLEMENTED}", file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fitme",
        description="Self-hosted, single-user training plan generator and training log.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="Run bot + web + retention job.")

    db_parser = subparsers.add_parser("db", help="Database maintenance.")
    db_subparsers = db_parser.add_subparsers(dest="db_command", required=True)
    db_subparsers.add_parser("upgrade", help="Apply pending SQL migrations.")

    activate_parser = subparsers.add_parser(
        "activate", help="Print a one-time Telegram activation code."
    )
    activate_parser.add_argument(
        "--rebind", action="store_true", help="Unlink the current account first."
    )

    export_parser = subparsers.add_parser("export", help="Dump all user data as JSON.")
    export_parser.add_argument("--out", required=True, help="Output file path.")

    delete_parser = subparsers.add_parser("delete", help="Delete the user and all data.")
    delete_parser.add_argument(
        "--yes", action="store_true", help="Confirm the irreversible delete."
    )

    subparsers.add_parser("purge", help="Run the retention job now.")

    hold_parser = subparsers.add_parser("hold", help="Health hold maintenance (operator).")
    hold_subparsers = hold_parser.add_subparsers(dest="hold_command", required=True)
    hold_clear_parser = hold_subparsers.add_parser(
        "clear",
        help="Clear every open health hold (the operator bypass for false positives).",
    )
    hold_clear_parser.add_argument(
        "--yes", action="store_true", help="Skip the typed confirmation."
    )

    catalog_parser = subparsers.add_parser("catalog", help="Exercise catalog maintenance.")
    catalog_subparsers = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    catalog_subparsers.add_parser(
        "check", help="Validate exercises.toml and locales; print content_version."
    )

    llm_parser = subparsers.add_parser("llm", help="LLM evaluation tools.")
    llm_subparsers = llm_parser.add_subparsers(dest="llm_command", required=True)
    eval_parser = llm_subparsers.add_parser(
        "eval",
        help="Run the LLM eval fixtures against a model. Spends money; never run in CI.",
    )
    eval_parser.add_argument("--agent", help="Limit the run to one agent.")
    eval_parser.add_argument("--model", help="Model string to evaluate.")
    eval_parser.add_argument(
        "--yes", action="store_true", help="Confirm spending real money on a live provider call."
    )

    return parser


# pydantic-ai prints a promotional banner on a process's first agent run unless told not to
# (A§10: no telemetry, and the operator owns the output). It reads `os.environ` lazily, at
# that first run, so setting it at CLI entry covers `serve` and `llm eval` alike.
PYDANTIC_AI_NO_BANNER_ENV = "PYDANTIC_AI_NO_BANNER"


def suppress_pydantic_ai_banner() -> None:
    os.environ.setdefault(PYDANTIC_AI_NO_BANNER_ENV, "1")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to a subcommand."""
    suppress_pydantic_ai_banner()
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(parser)
    if args.command == "db":
        if args.db_command == "upgrade":
            return _with_settings(parser, commands.db_upgrade)
        return _stub(f"db {args.db_command}")
    if args.command == "activate":
        return _with_settings(
            parser, lambda settings: commands.activate(settings, rebind=args.rebind)
        )
    if args.command == "export":
        return _with_settings(parser, lambda settings: commands.export_data(settings, args.out))
    if args.command == "delete":
        return _with_settings(
            parser, lambda settings: commands.delete_account(settings, confirmed=args.yes)
        )
    if args.command == "purge":
        return _with_settings(parser, commands.purge_now)
    if args.command == "hold":
        if args.hold_command == "clear":
            return _with_settings(
                parser, lambda settings: commands.hold_clear(settings, confirmed=args.yes)
            )
        return _stub(f"hold {args.hold_command}")
    if args.command == "catalog":
        if args.catalog_command == "check":
            return catalog_check()
        return _stub(f"catalog {args.catalog_command}")
    if args.command == "llm":
        if args.llm_command == "eval":
            return _with_settings(
                parser,
                lambda settings: llm_eval.run(
                    settings, agent=args.agent, model=args.model, confirmed=args.yes
                ),
            )
        return _stub(f"llm {args.llm_command}")

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable: parser.error() raises SystemExit


def _with_settings(
    parser: argparse.ArgumentParser, coro_factory: Callable[[Settings], Coroutine[Any, Any, int]]
) -> int:
    """Load settings, failing fast with a readable message, then run an async subcommand."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        parser.exit(1, f"{exc}\n")
        raise AssertionError("unreachable") from exc  # parser.exit() always raises SystemExit
    return asyncio.run(coro_factory(settings))


def _serve(parser: argparse.ArgumentParser) -> int:
    """`fitme serve` refuses to start while any migration is pending (A§4.7) or the LLM model
    configuration is invalid (A§8.5 rule 1). Once those checks pass, `commands.serve_async`
    runs the bot (the web app is still to come, M9)."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        parser.exit(1, f"{exc}\n")
        raise AssertionError("unreachable") from exc  # parser.exit() always raises SystemExit

    try:
        llm_models.validate_startup(settings)
    except SettingsError as exc:
        print(f"Refusing to start: invalid LLM model configuration: {exc}", file=sys.stderr)
        return 1

    # Don't create a database file just to answer "are migrations pending?" — open_database
    # would do exactly that. If the file is missing, there's nothing to check yet.
    if not settings.db_path.exists():
        print(
            "Refusing to start: database not initialized, run `fitme db upgrade`.",
            file=sys.stderr,
        )
        return 1

    pending = asyncio.run(commands.pending_migration_names(settings))
    if pending is None:
        return 1  # commands.pending_migration_names already printed a friendly error
    if pending:
        print(
            "Refusing to start: pending migrations: "
            + ", ".join(pending)
            + ". Run `fitme db upgrade` first.",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(commands.serve_async(settings))


if __name__ == "__main__":
    sys.exit(main())
