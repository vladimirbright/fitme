"""SQL never leaves `db/` (A§4.6 rule 1).

Scans every `.py` file under `src/fitme` except `src/fitme/db/` for:
  * any `.execute`/`.executemany`/`.executescript` attribute access (called or not);
  * `import sqlite3` / `import aiosqlite` (or a `from ... import ...` of either);
  * a string literal that starts with a SQL keyword and looks like a SQL statement.
Any one outside `db/` is a rule violation.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "fitme"
_DB_ROOT = _SRC_ROOT / "db"
_EXECUTE_METHOD_NAMES = {"execute", "executemany", "executescript"}
_DB_MODULE_NAMES = {"sqlite3", "aiosqlite"}
_SQL_KEYWORDS = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "REPLACE",
    "CREATE",
    "DROP",
    "ALTER",
    "PRAGMA",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "WITH",
)
# A leading keyword alone is too easily a false positive: ordinary English prose often
# starts a sentence with "With", "Update", "Begin", "Select", ... This project's own SQL is
# always written with uppercase keywords (a plain style convention, not enforced elsewhere),
# so matching is exact-case: "Select a plan from the list" doesn't match "SELECT" (mixed
# case), while "SELECT id FROM ..." does. A second, exact-case marker word is also required,
# for the same reason (a sentence capitalized "With ..." never later says "... FROM ...").
_SQL_MARKER_WORDS = (
    "FROM",
    "WHERE",
    "VALUES",
    "SET",
    "INTO",
    "TABLE",
    "INDEX",
    "REFERENCES",
    "JOIN",
)


def _iter_source_files() -> list[Path]:
    return [path for path in _SRC_ROOT.rglob("*.py") if _DB_ROOT not in path.parents]


def _unwrap_sql_literal_prefix(text: str) -> str:
    """Strip leading `--` comment lines and leading `(` (a subquery-shaped literal), so the
    keyword check still recognizes e.g. `"-- note\\nSELECT ..."` or `"(SELECT 1)"`.
    """
    stripped = text.strip()
    while stripped:
        if stripped.startswith("--"):
            _, _, rest = stripped.partition("\n")
            stripped = rest.strip()
            continue
        if stripped.startswith("("):
            stripped = stripped[1:].strip()
            continue
        break
    return stripped


def _looks_like_sql(text: str) -> bool:
    stripped = _unwrap_sql_literal_prefix(text)
    if not stripped:
        return False
    first_word = stripped.split(None, 1)[0].rstrip(";")
    if first_word not in _SQL_KEYWORDS:  # exact case: see _SQL_MARKER_WORDS comment above
        return False
    if "?" in stripped:
        return True
    return any(re.search(rf"\b{marker}\b", stripped) for marker in _SQL_MARKER_WORDS)


def _import_root_module(name: str | None) -> str | None:
    if not name:
        return None
    return name.split(".", 1)[0]


def _violations_in_source(source: str, label: str) -> list[str]:
    """The rule-1 violations found in `source` (Python code as text). `label` is only used
    to build readable messages (a file path when scanning the repo, a description in tests).
    """
    tree = ast.parse(source, filename=label)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _EXECUTE_METHOD_NAMES:
            violations.append(f"{label}:{node.lineno}: attribute access `.{node.attr}`")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _import_root_module(alias.name) in _DB_MODULE_NAMES:
                    violations.append(f"{label}:{node.lineno}: `import {alias.name}`")
        elif isinstance(node, ast.ImportFrom):
            if _import_root_module(node.module) in _DB_MODULE_NAMES:
                violations.append(f"{label}:{node.lineno}: `from {node.module} import ...`")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _looks_like_sql(node.value)
        ):
            violations.append(f"{label}:{node.lineno}: SQL-shaped string literal")
    return violations


def _violations_in_file(path: Path) -> list[str]:
    return _violations_in_source(path.read_text(encoding="utf-8"), str(path))


def test_no_sql_outside_db_package() -> None:
    files = _iter_source_files()
    assert files, "expected to find at least one source file outside src/fitme/db"

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(_violations_in_file(path))

    assert not all_violations, "SQL found outside src/fitme/db:\n" + "\n".join(all_violations)


# ---------------------------------------------------------------------------
# Self-tests for the checker itself: known-bad snippets must be caught, and known-good
# prose that merely resembles SQL vocabulary must not be flagged.
# ---------------------------------------------------------------------------


def test_checker_catches_an_execute_call() -> None:
    violations = _violations_in_source("conn.execute('SELECT 1')", "<bad-call>")
    assert violations


def test_checker_catches_bare_attribute_access_without_a_call() -> None:
    violations = _violations_in_source("runner = conn.executescript", "<bad-attr>")
    assert violations


def test_checker_catches_import_sqlite3() -> None:
    assert _violations_in_source("import sqlite3", "<bad-import>")


def test_checker_catches_import_aiosqlite_submodule() -> None:
    assert _violations_in_source("import aiosqlite.core", "<bad-import-submodule>")


def test_checker_catches_from_aiosqlite_import() -> None:
    assert _violations_in_source("from aiosqlite import connect", "<bad-from-import>")


def test_checker_catches_a_raw_sql_string_literal() -> None:
    violations = _violations_in_source(
        'query = "SELECT id FROM users WHERE id = ?"', "<bad-literal>"
    )
    assert violations


def test_checker_catches_a_sql_literal_behind_a_comment_or_parenthesis() -> None:
    assert _violations_in_source(
        'q = "-- a note\\nSELECT id FROM users WHERE id = ?"', "<bad-literal-comment>"
    )
    assert _violations_in_source('q = "(SELECT id FROM users WHERE id = ?)"', "<bad-literal-paren>")


def test_checker_does_not_flag_prose_that_starts_with_a_sql_keyword() -> None:
    # "Select" reads as an ordinary English imperative here, not a SQL statement; it has no
    # FROM/WHERE/VALUES/... marker, so it must not be flagged.
    assert not _violations_in_source('help_text = "Select a plan from the list"', "<good-prose>")


def test_checker_does_not_flag_other_common_prose_starters() -> None:
    for sentence in (
        "With no history, only a calibration load is allowed.",
        "Update the profile once setup finishes.",
        "Begin the workout when you're ready.",
        "Create a new plan from the menu.",
    ):
        assert not _violations_in_source(f'text = "{sentence}"', "<good-prose>"), sentence


def test_checker_does_not_flag_ordinary_non_db_imports() -> None:
    assert not _violations_in_source("import json\nimport asyncio", "<good-import>")
