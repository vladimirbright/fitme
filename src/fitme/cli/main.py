"""The `fitme` command.

Subcommands mirror docs/ARCHITECTURE.md §11. Every subcommand is a stub in M0: it parses its
arguments and reports that it isn't implemented yet. Later milestones replace each stub body
with real behavior; the argument parser shape is meant to stay stable.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch to a (currently stubbed) subcommand."""
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _stub("serve")
    if args.command == "db":
        return _stub(f"db {args.db_command}")
    if args.command == "activate":
        return _stub("activate")
    if args.command == "export":
        return _stub("export")
    if args.command == "delete":
        return _stub("delete")
    if args.command == "purge":
        return _stub("purge")
    if args.command == "catalog":
        return _stub(f"catalog {args.catalog_command}")
    if args.command == "llm":
        return _stub(f"llm {args.llm_command}")

    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable: parser.error() raises SystemExit


if __name__ == "__main__":
    sys.exit(main())
