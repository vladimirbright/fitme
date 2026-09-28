"""CLI smoke tests: --help works, and an unknown subcommand under a group (`db`, `catalog`,
`llm`) is rejected by argparse itself, since every group now has exactly one real subcommand
registered (M1, M3, M4, M5) and no `_stub()` path is reachable through normal parsing any
more. `db upgrade`, `export`, `delete`, `purge`, `activate` and the `serve` migration check
are real as of M1/M5 and are covered in tests/integration/test_cli_commands.py instead.
`catalog check` is real as of M3 and is covered in tests/unit/test_cli_catalog_check.py
instead. `llm eval` is real as of M4 and is covered in tests/integration/test_cli_commands.py
(it needs `Settings`, so it can't run env-free the way the tests in this file do)."""

from __future__ import annotations

import os

import pytest

from fitme.cli.main import PYDANTIC_AI_NO_BANNER_ENV, main


def test_help_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for subcommand in (
        "serve",
        "db",
        "activate",
        "export",
        "delete",
        "purge",
        "catalog",
        "llm",
        "health",
        "backup",
        "history",
    ):
        assert subcommand in output


def test_unknown_db_subcommand_is_rejected_by_argparse() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["db", "not-a-real-subcommand"])

    assert exc_info.value.code == 2


def test_catalog_check_is_no_longer_a_stub(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["catalog", "check"])

    assert exit_code == 0
    assert "content_version" in capsys.readouterr().out


def test_cli_entry_suppresses_the_pydantic_ai_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A§10: `fitme` sets `PYDANTIC_AI_NO_BANNER=1` at entry (before any agent run, which is
    when pydantic-ai reads it), so `serve` and `llm eval` never print the promotional
    banner. An operator's explicit value is left alone."""
    monkeypatch.delenv(PYDANTIC_AI_NO_BANNER_ENV, raising=False)
    with pytest.raises(SystemExit):
        main(["--help"])
    assert os.environ.get(PYDANTIC_AI_NO_BANNER_ENV) == "1"

    monkeypatch.setenv(PYDANTIC_AI_NO_BANNER_ENV, "yes")
    with pytest.raises(SystemExit):
        main(["--help"])
    assert os.environ.get(PYDANTIC_AI_NO_BANNER_ENV) == "yes"
