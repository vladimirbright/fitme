"""CLI smoke tests: --help works, and stub subcommands say they aren't implemented yet."""

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
    exit_code = main(["purge"])

    assert exit_code != 0
    assert "not implemented yet" in capsys.readouterr().err


def test_nested_subcommand_stub(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["catalog", "check"])

    assert exit_code != 0
    assert "catalog check" in capsys.readouterr().err
