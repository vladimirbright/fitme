"""Filters the loaded exercise catalog by location, equipment and screening flags (A§4.4).

Lives in `services/`, not `fitme/catalog/` (the loader), because it calls
`guards.screening.exercise_allowed` — and `fitme.catalog` is meant to be importable from
anywhere, including eventually from `domain/`-adjacent code, without pulling in `guards/`.
"""

from __future__ import annotations

from collections.abc import Sequence

from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import Equipment, Location
from fitme.domain.screening import ScreeningFlagState
from fitme.guards.screening import exercise_allowed


def available_exercises(
    catalog: Catalog,
    location: Location,
    equipment: frozenset[Equipment],
    flags: Sequence[ScreeningFlagState],
) -> list[Exercise]:
    """The catalog exercises usable for this `location` + `equipment` + `flags` combination:
    fits the location, every piece of required equipment is in `equipment`, and no active
    contraindication (`guards.screening.exercise_allowed`) blocks it. Pure: no I/O, no
    caching, callers pass in an already-loaded `catalog` (typically
    `fitme.catalog.load_catalog()`'s result) and the equipment set they resolved (A§5.1: the
    profile's own selection for `home_equipment`, or
    `fitme.domain.catalog.LOCATION_DEFAULT_EQUIPMENT` for every other location).
    """
    return [
        exercise
        for exercise in catalog.exercise
        if location in exercise.locations
        and all(item in equipment for item in exercise.equipment)
        and exercise_allowed(exercise, flags).ok
    ]
