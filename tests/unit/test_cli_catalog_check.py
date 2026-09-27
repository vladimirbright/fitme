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


def test_catalog_check_fails_on_a_dumbbell_exercise_without_load_unit() -> None:
    """A§4.4: a dumbbell/kettlebell exercise must not default `load_unit` silently — the
    model refuses it, and `catalog check` reports that as a failure."""
    data = tomllib.loads(_BROKEN_FIXTURE.read_text(encoding="utf-8"))
    entry = dict(data["exercise"][0])
    entry.update(
        {
            "id": "dumbbell_press_no_unit",
            "equipment": ["dumbbells"],
            "loads_areas": ["shoulder"],
            "contraindicated_by": ["shoulder_injury_current"],
            "start": {"kind": "kg", "kg": 2.0},
        }
    )
    entry.pop("load_unit", None)

    def load_missing_unit() -> Catalog:
        return Catalog.model_validate({"exercise": [entry]})

    with pytest.raises(ValidationError, match="load_unit"):
        load_missing_unit()
    assert catalog_check(load=load_missing_unit) == 1


def test_every_shipped_dumbbell_or_kettlebell_exercise_declares_its_load_unit() -> None:
    from fitme.catalog import load_catalog

    for exercise in load_catalog().exercise:
        uses_hand_implement = any(
            item.value in ("dumbbells", "kettlebell") for item in exercise.equipment
        )
        if uses_hand_implement:
            assert exercise.load_unit in ("per_implement", "single_implement"), exercise.id
        else:
            assert exercise.load_unit == "total", exercise.id


def test_catalog_check_reports_a_kg_loadable_override_on_a_bodyweight_move() -> None:
    from fitme.domain.catalog import kg_loadable_problems

    data = tomllib.loads(_BROKEN_FIXTURE.read_text(encoding="utf-8"))
    entry = dict(data["exercise"][0])
    entry.update(
        {
            "id": "weighted_jog",
            "kind": "cardio",
            "pattern": "conditioning",
            "loads_areas": ["knee"],
            "contraindicated_by": ["knee_injury_current"],
            "start": {"kind": "bodyweight"},
            "kg_loadable": True,
        }
    )
    catalog = Catalog.model_validate({"exercise": [entry]})
    problems = kg_loadable_problems(catalog)
    assert problems and "weighted_jog" in problems[0]
    assert catalog_check(load=lambda: catalog) == 1


def test_every_shipped_exercise_has_a_sensible_kg_loadable() -> None:
    from fitme.catalog import load_catalog
    from fitme.domain.catalog import kg_loadable_problems

    catalog = load_catalog()
    assert kg_loadable_problems(catalog) == []
    by_id = {e.id: e for e in catalog.exercise}
    assert by_id["barbell_back_squat"].kg_loadable and by_id["machine_leg_press"].kg_loadable
    assert by_id["kettlebell_swing"].kg_loadable  # explicit override: a real kettlebell load
    for exercise_id in ("pushup", "plank", "machine_rower", "easy_jog", "mobility_cat_cow"):
        assert not by_id[exercise_id].kg_loadable, exercise_id
    band_only = [
        e.id
        for e in catalog.exercise
        if "resistance_bands" in {i.value for i in e.equipment}
        and not {i.value for i in e.equipment}
        & {"barbell", "dumbbells", "kettlebell", "machine", "cable"}
    ]
    assert "band_squat" in band_only and "band_lat_pulldown" in band_only
    assert all(not by_id[exercise_id].kg_loadable for exercise_id in band_only), band_only


def test_cli_main_reports_content_version_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["catalog", "check"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "content_version:" in out
    assert "catalog OK" in out
    assert "locales OK" in out
