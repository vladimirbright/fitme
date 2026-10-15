"""`fitme.catalog` (A§4.4, A§4.8, IMPLEMENTATION_PLAN M3, ADR 0006): loading the
one-file-per-exercise catalog, and the
pure domain-level data (`LOCATION_DEFAULT_EQUIPMENT`, `HOME_SELECTABLE_EQUIPMENT`, the
contraindication-coverage invariant) it validates against. Filtering the catalog
(`available_exercises`) is `services/catalog.py`'s job — see `test_services_catalog.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from fitme.catalog import (
    CatalogLayoutError,
    exercise_files,
    id_range,
    load_catalog,
    read_exercises,
)
from fitme.domain.catalog import (
    GYM_ONLY_EQUIPMENT,
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
    assert 45 <= len(catalog.exercise) <= 2000


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
    assert frozenset(Equipment) - GYM_ONLY_EQUIPMENT == HOME_SELECTABLE_EQUIPMENT
    assert {Equipment.MACHINE, Equipment.CABLE, Equipment.SMITH_MACHINE} <= GYM_ONLY_EQUIPMENT
    assert {Equipment.EZ_BAR, Equipment.STABILITY_BALL, Equipment.DIP_BARS} <= (
        HOME_SELECTABLE_EQUIPMENT
    )


def test_shipped_catalog_has_no_missing_contraindication_coverage() -> None:
    """A§4.4 invariant: every exercise's `contraindicated_by` covers every area it loads.
    None of the shipped exercises are expected to need a `contraindication_exceptions`
    escape hatch."""
    problems = missing_contraindication_coverage(load_catalog())
    assert problems == [], problems


def _catalog_root(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / "exercises" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_id_range_maps_the_first_letter_to_its_folder() -> None:
    assert id_range("barbell_back_squat") == "a-c"
    assert id_range("dumbbell_row") == "d-f"
    assert id_range("pull_up") == "p-r"
    with pytest.raises(CatalogLayoutError):
        id_range("1_bad")


def test_every_shipped_exercise_file_is_named_by_id_and_in_its_range_folder() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "fitme" / "catalog"
    paths = [path for path, _item in exercise_files(root)]
    ids = sorted(exercise.id for exercise in load_catalog().exercise)
    assert sorted(path.rsplit("/", 1)[1].removesuffix(".toml") for path in paths) == ids
    for path in paths:
        exercise_id = path.rsplit("/", 1)[1].removesuffix(".toml")
        assert path == f"exercises/{id_range(exercise_id)}/{exercise_id}.toml"


def test_a_file_whose_name_differs_from_its_id_is_rejected(tmp_path: Path) -> None:
    root = _catalog_root(tmp_path, {"a-c/band_row.toml": 'id = "band_pull"\n'})
    with pytest.raises(CatalogLayoutError, match="must match the file name"):
        read_exercises(root)


def test_a_file_in_the_wrong_range_folder_is_rejected(tmp_path: Path) -> None:
    root = _catalog_root(tmp_path, {"d-f/band_row.toml": 'id = "band_row"\n'})
    with pytest.raises(CatalogLayoutError, match="belongs in exercises/a-c/"):
        read_exercises(root)


def test_a_stray_file_or_folder_is_rejected_not_skipped(tmp_path: Path) -> None:
    with pytest.raises(CatalogLayoutError, match="unexpected catalog entry"):
        read_exercises(_catalog_root(tmp_path / "one", {"a-c/notes.txt": "x"}))
    with pytest.raises(CatalogLayoutError, match="unexpected catalog entry"):
        read_exercises(_catalog_root(tmp_path / "two", {"misc/band_row.toml": 'id = "band_row"\n'}))
