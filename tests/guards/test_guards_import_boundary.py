"""A§7 / A§2.1: `guards/` imports only `fitme.domain` and the stdlib. An AST scan (not a
runtime import trace) so it catches an unused-but-present import too."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_GUARDS_ROOT = Path(__file__).resolve().parents[2] / "src" / "fitme" / "guards"

_STDLIB_MODULE_NAMES = sys.stdlib_module_names

# I/O-capable stdlib modules are explicitly denied even though they're stdlib: guards must
# have NO I/O (A§7's own words), and "it's stdlib" isn't the same guarantee as "it can't
# touch the filesystem/network/DB/process". `importlib` is not on this list: `stop_words`
# legitimately reads its word lists via `importlib.resources`, which is package-data access,
# not filesystem/network I/O keyed on caller-controlled paths.
_DENIED_IO_MODULES = frozenset(
    {
        "sqlite3",
        "socket",
        "urllib",
        "http",
        "subprocess",
        "os",
        "pathlib",
        "asyncio",
        "io",
        "shutil",
        "tempfile",
        "threading",
        "multiprocessing",
        "ftplib",
        "smtplib",
    }
)


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def _is_allowed(dotted: str) -> bool:
    """A dotted module name is allowed if it's stdlib (and not an I/O-capable module denied
    above), or `fitme.domain`/`fitme.guards` (any depth): guards may import domain and its
    own sibling modules, nothing else."""
    root = _root_module(dotted)
    if root != "fitme":
        return root in _STDLIB_MODULE_NAMES and root not in _DENIED_IO_MODULES
    return any(
        dotted == allowed or dotted.startswith(allowed + ".")
        for allowed in ("fitme.domain", "fitme.guards")
    )


def _violations_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_allowed(alias.name):
                    violations.append(f"{path}:{node.lineno}: `import {alias.name}`")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 and node.module is None:
                continue  # a bare relative import (`from . import x`) inside guards/ itself
            module = node.module or ""
            if not _is_allowed(module):
                violations.append(f"{path}:{node.lineno}: `from {module} import ...`")
    return violations


def test_guards_package_imports_nothing_outside_domain_and_stdlib() -> None:
    files = sorted(p for p in _GUARDS_ROOT.rglob("*.py"))
    assert files, "expected to find at least one module under src/fitme/guards"

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(_violations_in_file(path))

    assert not all_violations, "disallowed import(s) in guards/:\n" + "\n".join(all_violations)


# --- Self-tests for the checker: an I/O-capable stdlib module must be denied even though
# it's technically "stdlib", and importlib.resources (as stop_words uses it) must be allowed.


def test_checker_denies_sqlite3() -> None:
    assert not _is_allowed("sqlite3")


def test_checker_denies_os() -> None:
    assert not _is_allowed("os")


def test_checker_denies_pathlib() -> None:
    assert not _is_allowed("pathlib")


def test_checker_denies_subprocess_submodule() -> None:
    assert not _is_allowed("subprocess.run")


@pytest.mark.parametrize(
    "module", ["io", "shutil", "tempfile", "threading", "multiprocessing", "ftplib", "smtplib"]
)
def test_checker_denies_additional_io_modules(module: str) -> None:
    assert not _is_allowed(module)


def test_checker_allows_importlib_resources() -> None:
    assert _is_allowed("importlib.resources")


def test_checker_allows_ordinary_stdlib_modules() -> None:
    assert _is_allowed("re")
    assert _is_allowed("dataclasses")
    assert _is_allowed("math")
    assert _is_allowed("functools")
