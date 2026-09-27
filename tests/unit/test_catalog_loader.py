"""`fitme.catalog` (A§4.4, A§4.8, IMPLEMENTATION_PLAN M3): loading `exercises.toml`, and the
pure domain-level data (`LOCATION_DEFAULT_EQUIPMENT`, `HOME_SELECTABLE_EQUIPMENT`, the
contraindication-coverage invariant) it validates against. Filtering the catalog
(`available_exercises`) is `services/catalog.py`'s job — see `test_services_catalog.py`."""

from __future__ import annotations

from fitme.catalog import load_catalog
from fitme.domain.catalog import (
    HOME_SELECTABLE_EQUIPMENT,
    LOCATION_DEFAULT_EQUIPMENT,
    Catalog,
    missing_contraindication_coverage,
)
from fitme.domain.enums import Equipment, Location


def test_load_catalog_returns_a_reasonable_number_of_exercises() -> None:
    """A§4.4/IMPLEMENTATION_PLAN's "40-60 exercises" describes the initial M3 seed; later
    batches (e.g. the M3-round-3 Codex merge, machine/cable coverage) grow it well past 60.
    This is a sanity ceiling against a data bug (an accidental duplication loop, a bad
    merge), not the real content target — the docs' number is intentionally left as-is."""
    catalog = load_catalog()
    assert 45 <= len(catalog.exercise) <= 300


def test_load_catalog_is_cached() -> None:
    assert load_catalog() is load_catalog()


def test_load_catalog_returns_a_catalog_instance() -> None:
    assert isinstance(load_catalog(), Catalog)


def test_location_default_equipment_gym_locations_have_every_equipment_value() -> None:
    for location in (Location.PUBLIC_GYM, Location.STUDIO_GYM):
        assert LOCATION_DEFAULT_EQUIPMENT[location] == frozenset(Equipment)


def test_location_default_equipment_gym_locations_include_machine_and_cable() -> None:
    for location in (Location.PUBLIC_GYM, Location.STUDIO_GYM):
        assert Equipment.MACHINE in LOCATION_DEFAULT_EQUIPMENT[location]
        assert Equipment.CABLE in LOCATION_DEFAULT_EQUIPMENT[location]


def test_location_default_equipment_apartment_has_none() -> None:
    assert LOCATION_DEFAULT_EQUIPMENT[Location.APARTMENT_NO_EQUIPMENT] == frozenset()


def test_location_default_equipment_outdoor_is_pull_up_bar_only() -> None:
    assert LOCATION_DEFAULT_EQUIPMENT[Location.OUTDOOR] == frozenset({Equipment.PULL_UP_BAR})


def test_location_default_equipment_has_no_entry_for_home_equipment() -> None:
    # A§5.1: home-with-equipment has no default; it's the user's own multi-select answer.
    assert Location.HOME_EQUIPMENT not in LOCATION_DEFAULT_EQUIPMENT


def test_home_selectable_equipment_excludes_machine_and_cable() -> None:
    assert Equipment.MACHINE not in HOME_SELECTABLE_EQUIPMENT
    assert Equipment.CABLE not in HOME_SELECTABLE_EQUIPMENT


def test_home_selectable_equipment_includes_everything_else() -> None:
    assert (
        frozenset(Equipment)
        - {
            Equipment.MACHINE,
            Equipment.CABLE,
        }
        == HOME_SELECTABLE_EQUIPMENT
    )


def test_shipped_catalog_has_no_missing_contraindication_coverage() -> None:
    """A§4.4 invariant: every exercise's `contraindicated_by` covers every area it loads.
    None of the shipped exercises are expected to need a `contraindication_exceptions`
    escape hatch."""
    problems = missing_contraindication_coverage(load_catalog())
    assert problems == [], problems
