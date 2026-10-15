"""A local TOML reference stays distinct from the guarded planning catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from fitme.catalog import load_catalog
from fitme.services.reference_exercises import (
    ReferenceDataError,
    load_reference,
    search_reference,
)


def _reference(exercise_id: str = "0001") -> str:
    return f'''source = "https://example.invalid/source"
planning_status = "reference_only"

[[exercise]]
id = "{exercise_id}"
category = "chest"
equipment = "barbell"
target = "pectorals"
muscle_group = "triceps"
secondary_muscles = ["shoulders"]
review_status = "safety_review_needed"
primary_equipment_hint = "barbell"

[exercise.names]
en = "Barbell example"
es = "Ejemplo con barra"
it = "Esempio con bilanciere"
tr = "Halter örneği"
ru = "Упражнение со штангой"
zh = "杠铃示例"

[exercise.name_sources]
en = "source"
es = "draft"
it = "draft"
tr = "draft"
ru = "draft"
zh = "source"

[exercise.instructions]
en = "Lift with control."
es = "Levanta con control."
it = "Solleva con controllo."
tr = "Kontrollü kaldır."
ru = "Поднимайте плавно."
zh = "控制动作。"
'''


def test_local_toml_is_searchable_but_not_plan_eligible(tmp_path: Path) -> None:
    path = tmp_path / "fitme-reference-exercises.toml"
    path.write_text(_reference(), encoding="utf-8")
    exercise = load_reference(path)[0]
    assert exercise.names["es"] == "Ejemplo con barra"
    assert exercise.instructions["zh"] == "控制动作。"
    assert exercise.review_status == "safety_review_needed"
    assert load_catalog().by_id(exercise.id) is None
    assert search_reference([exercise], "EJEMPLO", "chest", "barbell") == [exercise]
    assert search_reference([exercise], "", "back", "") == []


def test_bad_reference_toml_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "fitme-reference-exercises.toml"
    path.write_text(_reference(exercise_id="../x"), encoding="utf-8")
    with pytest.raises(ReferenceDataError, match="four ASCII digits"):
        load_reference(path)
    path.write_text(_reference().replace('zh = "杠铃示例"', ""), encoding="utf-8")
    with pytest.raises(ReferenceDataError, match="names must cover all six"):
        load_reference(path)
