"""Loads the exercise catalog from `exercises/<range>/<id>.toml` (A§4.4, ADR 0006).

Only the loader lives here: it's reference-data I/O, so it can't live in `domain/` (pure,
no I/O). Filtering the loaded catalog by location/equipment/screening flags
(`available_exercises`) lives in `services/catalog.py` instead, one layer up — it calls
`guards.screening.exercise_allowed`, and this package is meant to be safe for *anything* to
import (including `domain/` indirectly through `fitme.domain.catalog.Catalog`), so it must
not itself depend on `guards/`.

One exercise per file, its top-level keys being one `Exercise`. The file name must be the
exercise's `id`, and the file must sit in the range folder for the id's first letter
(`id_range`); a misplaced or misnamed file fails the load rather than being silently skipped.
"""

from __future__ import annotations

import importlib.resources
import tomllib
from functools import cache
from importlib.resources.abc import Traversable

from fitme.domain.catalog import Catalog

_CATALOG_PACKAGE = "fitme.catalog"
EXERCISES_DIR = "exercises"
# The range folders, by an id's first letter. Ids are lowercase snake_case.
ID_RANGES: tuple[str, ...] = ("a-c", "d-f", "g-i", "j-l", "m-o", "p-r", "s-u", "v-x", "y-z")


class CatalogLayoutError(ValueError):
    """A catalog file is misnamed, misplaced, or not a TOML file of its own."""


def id_range(exercise_id: str) -> str:
    """The range folder an exercise id belongs in, e.g. `barbell_back_squat` -> `a-c`."""
    first = exercise_id[:1]
    for folder in ID_RANGES:
        if folder[0] <= first <= folder[-1]:
            return folder
    raise CatalogLayoutError(f"{exercise_id!r}: an id must start with a lowercase ASCII letter")


def exercise_files(root: Traversable) -> list[tuple[str, Traversable]]:
    """Every `(relative_path, file)` under the catalog's `exercises/` folder, sorted by path.
    Anything other than `<range>/<name>.toml` is a layout error."""
    exercises = root / EXERCISES_DIR
    found: list[tuple[str, Traversable]] = []
    for folder in exercises.iterdir():
        if folder.name == "README.md":
            continue
        if not folder.is_dir() or folder.name not in ID_RANGES:
            raise CatalogLayoutError(f"unexpected catalog entry: {EXERCISES_DIR}/{folder.name}")
        for item in folder.iterdir():
            if not item.is_file() or not item.name.endswith(".toml"):
                raise CatalogLayoutError(
                    f"unexpected catalog entry: {EXERCISES_DIR}/{folder.name}/{item.name}"
                )
            found.append((f"{EXERCISES_DIR}/{folder.name}/{item.name}", item))
    return sorted(found, key=lambda pair: pair[0])


def read_exercises(root: Traversable) -> list[dict[str, object]]:
    """The raw exercise tables, each checked against its file name and range folder."""
    exercises: list[dict[str, object]] = []
    for path, item in exercise_files(root):
        data = tomllib.loads(item.read_text(encoding="utf-8"))
        exercise_id = data.get("id")
        if not isinstance(exercise_id, str) or f"{exercise_id}.toml" != item.name:
            raise CatalogLayoutError(f"{path}: id {exercise_id!r} must match the file name")
        if not path.startswith(f"{EXERCISES_DIR}/{id_range(exercise_id)}/"):
            raise CatalogLayoutError(f"{path}: belongs in {EXERCISES_DIR}/{id_range(exercise_id)}/")
        exercises.append(data)
    return exercises


@cache
def load_catalog() -> Catalog:
    """Read and validate every exercise file, shipped as package data (A§4.8) and found the
    same way from a checkout or an installed wheel. Cached: the files never change at
    runtime."""
    return Catalog.model_validate(
        {"exercise": read_exercises(importlib.resources.files(_CATALOG_PACKAGE))}
    )
