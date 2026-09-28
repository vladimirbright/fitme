"""M11 housekeeping: the personal import file patterns are gitignored, the committed sample
fixture is neither of them (so it stays committable), it parses with the real parser and
catalog, and it carries nothing that looks like personal data."""

from __future__ import annotations

import fnmatch
import re
from datetime import UTC, datetime
from pathlib import Path

from fitme.catalog import load_catalog
from fitme.services.history_import import parse_import

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _ROOT / "tests" / "fixtures"
_SAMPLE = _FIXTURES / "history_sample.toml"
_IMPORT_PATTERNS = ("*.import.toml", "*.import.json")


def _gitignore_patterns() -> list[str]:
    lines = (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_import_file_patterns_are_gitignored() -> None:
    patterns = _gitignore_patterns()
    for pattern in _IMPORT_PATTERNS:
        assert pattern in patterns
    assert any(fnmatch.fnmatch("my.import.toml", pattern) for pattern in patterns)
    assert any(fnmatch.fnmatch("my.import.json", pattern) for pattern in patterns)


def test_no_fixture_matches_the_gitignored_patterns() -> None:
    fixtures = [path for path in _FIXTURES.rglob("*") if path.is_file()]
    assert _SAMPLE in fixtures
    for path in fixtures:
        for pattern in _IMPORT_PATTERNS:
            assert not fnmatch.fnmatch(path.name, pattern), path


def test_sample_fixture_parses_cleanly_and_is_synthetic() -> None:
    text = _SAMPLE.read_text(encoding="utf-8")
    parsed = parse_import(
        text,
        fmt="toml",
        catalog=load_catalog(),
        default_timezone=None,
        now=datetime(2026, 9, 28, tzinfo=UTC),
    )
    assert parsed.rejected == ()
    assert parsed.unknown_exercises == ()
    assert len(parsed.sessions) == 3
    assert len(parsed.plans) == 1
    # Nothing that looks like an identity: no e-mail, no handle, no long numeric id.
    assert "@" not in text
    assert re.search(r"\d{6,}", text) is None
    assert "synthetic" in text.casefold()
