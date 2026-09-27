"""Loads `exercises.toml` (A§4.4).

Only the loader lives here: it's reference-data I/O, so it can't live in `domain/` (pure,
no I/O). Filtering the loaded catalog by location/equipment/screening flags
(`available_exercises`) lives in `services/catalog.py` instead, one layer up — it calls
`guards.screening.exercise_allowed`, and this package is meant to be safe for *anything* to
import (including `domain/` indirectly through `fitme.domain.catalog.Catalog`), so it must
not itself depend on `guards/`.
"""

from __future__ import annotations

import importlib.resources
import tomllib
from functools import cache

from fitme.domain.catalog import Catalog

_CATALOG_PACKAGE = "fitme.catalog"
_CATALOG_FILENAME = "exercises.toml"


@cache
def load_catalog() -> Catalog:
    """Read and validate `exercises.toml`, shipped as package data (A§4.8) and found the same
    way from a checkout or an installed wheel. Cached: the file never changes at runtime."""
    resource = importlib.resources.files(_CATALOG_PACKAGE) / _CATALOG_FILENAME
    data = tomllib.loads(resource.read_text(encoding="utf-8"))
    return Catalog.model_validate(data)
