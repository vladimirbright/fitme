"""`content_version` (A§4.8): a short hash over the guard-relevant reference-data and
guard-logic files, so a past `decisions` row can be tied back to exactly what the guards saw
at the time (`git log -S <hash>`, or by recomputing this function at a given commit).

Hashed: `catalog/exercises.toml`, `guards/stop_words/*.txt`, `prompts/*.md` (the directory
doesn't exist until M4; absent or empty contributes nothing), `guards/**/*.py`, and
`domain/catalog.py` + `domain/enums.py` (the catalog-logic source that decides what a guard
sees, per the M3-round-2 update to A§4.8 — a change to a guard's Python, not just its data
files, must also change the version). Each file contributes its relative path, byte length
and bytes to the hash, in that order and in sorted-path order overall, so moving the same
content between files (or renaming a file) changes the result. Locales and the model price
table are deliberately excluded: they don't change what the guards allow, only how it's
presented (A§4.8).
"""

from __future__ import annotations

import hashlib
import importlib.resources
from collections.abc import Iterable
from importlib.resources.abc import Traversable

_CATALOG_PACKAGE = "fitme.catalog"
_CATALOG_FILENAME = "exercises.toml"
_STOP_WORDS_PACKAGE = "fitme.guards.stop_words"
_GUARDS_PACKAGE = "fitme.guards"
_DOMAIN_PACKAGE = "fitme.domain"
_ROOT_PACKAGE = "fitme"
_PROMPTS_SUBDIR = "prompts"
_DOMAIN_LOGIC_FILES = ("catalog.py", "enums.py")

_HASH_PREFIX_LENGTH = 12
_SKIP_DIR_NAMES = frozenset({"__pycache__"})


def _walk(root: Traversable, prefix: str, suffix: str) -> list[tuple[str, Traversable]]:
    """Every file under `root` (recursively) whose name ends with `suffix`, paired with a
    `/`-joined path relative to whatever logical root `prefix` names (e.g. `"guards"`). This
    relative path is hash input only, never a real filesystem path, so it stays stable across
    a checkout and an installed wheel. `root` not existing (or not being a directory) yields
    nothing — true for `prompts/` before M4 creates it."""
    if not root.is_dir():
        return []
    found: list[tuple[str, Traversable]] = []
    for item in root.iterdir():
        rel = f"{prefix}/{item.name}" if prefix else item.name
        if item.is_dir():
            if item.name in _SKIP_DIR_NAMES:
                continue
            found.extend(_walk(item, rel, suffix))
        elif item.is_file() and item.name.endswith(suffix):
            found.append((rel, item))
    return found


def _hashed_files() -> Iterable[tuple[str, Traversable]]:
    """Every guard-relevant `(relative_path, item)` pair, unsorted (the caller sorts)."""
    yield (
        f"catalog/{_CATALOG_FILENAME}",
        importlib.resources.files(_CATALOG_PACKAGE) / _CATALOG_FILENAME,
    )
    yield from _walk(importlib.resources.files(_STOP_WORDS_PACKAGE), "guards/stop_words", ".txt")
    yield from _walk(
        importlib.resources.files(_ROOT_PACKAGE) / _PROMPTS_SUBDIR, _PROMPTS_SUBDIR, ".md"
    )
    yield from _walk(importlib.resources.files(_GUARDS_PACKAGE), "guards", ".py")
    domain_root = importlib.resources.files(_DOMAIN_PACKAGE)
    for name in _DOMAIN_LOGIC_FILES:
        yield f"domain/{name}", domain_root / name


def content_version() -> str:
    """The first 12 hex characters of the SHA-256 over every hashed file's
    `path + length + bytes`, in sorted-path order."""
    digest = hashlib.sha256()
    for path, item in sorted(_hashed_files(), key=lambda pair: pair[0]):
        data = item.read_bytes()
        digest.update(path.encode("utf-8"))
        digest.update(len(path).to_bytes(4, "big"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:_HASH_PREFIX_LENGTH]
