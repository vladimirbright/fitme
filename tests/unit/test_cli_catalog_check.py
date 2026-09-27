"""`fitme catalog check` (A§11, IMPLEMENTATION_PLAN M3)."""

from __future__ import annotations

import pathlib
import tomllib

import pytest
from pydantic import ValidationError

from fitme.cli.catalog_check import catalog_check
from fitme.cli.main import main
from fitme.domain.catalog import Catalog

_BROKEN_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "catalog" / "broken_exercises.toml"
)


def _load_broken() -> Catalog:
    data = tomllib.loads(_BROKEN_FIXTURE.read_text(encoding="utf-8"))
    return Catalog.model_validate(data)


def test_catalog_check_passes_on_the_real_shipped_catalog() -> None:
    assert catalog_check() == 0


def test_catalog_check_fails_on_a_broken_fixture() -> None:
    """`_load_broken` raises `ValidationError` itself (an unknown `loads_areas` value):
    proves the fixture is actually broken, and that `catalog_check` catches and reports
    exactly this kind of failure rather than letting it propagate."""
    with pytest.raises(ValidationError):
        _load_broken()

    assert catalog_check(load=_load_broken) == 1


def test_cli_main_reports_content_version_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["catalog", "check"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "content_version:" in out
    assert "catalog OK" in out
    assert "locales OK" in out
