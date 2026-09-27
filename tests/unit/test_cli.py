"""CLI smoke tests: --help works, and still-stubbed subcommands say they aren't implemented
yet. `db upgrade`, `export`, `delete`, `purge` and the `serve` migration check are real as of
M1 and are covered in tests/integration/test_cli_commands.py instead. `catalog check` is real
as of M3 and is covered in tests/unit/test_cli_catalog_check.py instead."""

from __future__ import annotations

import pytest

from fitme.cli.main import main


def test_help_lists_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for subcommand in ("serve", "db", "activate", "export", "delete", "purge", "catalog", "llm"):
        assert subcommand in output


def test_unimplemented_subcommand_reports_clearly(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["activate"])

    assert exit_code != 0
    assert "not implemented yet" in capsys.readouterr().err


def test_nested_subcommand_stub(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["llm", "eval"])

    assert exit_code != 0
    assert "llm eval" in capsys.readouterr().err


def test_catalog_check_is_no_longer_a_stub(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["catalog", "check"])

    assert exit_code == 0
    assert "content_version" in capsys.readouterr().out
