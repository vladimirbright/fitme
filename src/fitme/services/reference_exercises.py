"""Read the operator's local TOML reference library, separate from the planning catalog."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

FILENAME = "fitme-reference-exercises.toml"
LANGUAGES = frozenset({"en", "es", "it", "tr", "ru", "zh"})
REVIEW_STATUSES = frozenset(
    {"possible_catalog_match", "equipment_review_needed", "safety_review_needed"}
)


class ReferenceDataError(ValueError):
    """The local reference TOML is incomplete or malformed."""


@dataclass(frozen=True)
class ReferenceExercise:
    id: str
    name: str
    names: dict[str, str]
    name_sources: dict[str, str]
    category: str
    equipment: str
    target: str
    muscle_group: str
    secondary_muscles: tuple[str, ...]
    instructions: dict[str, str]
    review_status: str
    primary_equipment_hint: str | None
    possible_catalog_id: str | None
    duplicate_source_name: bool


def reference_path(db_path: Path) -> Path:
    """Keep the local TOML beside the operator's database or Docker data volume."""
    return db_path.parent / FILENAME


def _text(row: dict[str, object], key: str, exercise_id: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReferenceDataError(f"{exercise_id}: {key} must be non-empty text")
    return value.strip()


def _languages(row: dict[str, object], key: str, exercise_id: str) -> dict[str, str]:
    value = row.get(key)
    if not isinstance(value, dict) or set(value) != LANGUAGES:
        raise ReferenceDataError(f"{exercise_id}: {key} must cover all six languages")
    return {language: _text(value, language, exercise_id) for language in sorted(LANGUAGES)}


def load_reference(path: Path) -> list[ReferenceExercise]:
    """Load all locally edited reference entries; return an empty list when absent."""
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReferenceDataError(f"cannot read {path}: {exc}") from exc
    rows = data.get("exercise")
    if data.get("planning_status") != "reference_only" or not isinstance(rows, list):
        raise ReferenceDataError("reference TOML must declare reference_only and exercises")
    exercises: list[ReferenceExercise] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ReferenceDataError("exercise entry must be a table")
        row = cast(dict[str, object], raw)
        exercise_id = _text(row, "id", "exercise")
        if len(exercise_id) != 4 or not exercise_id.isascii() or not exercise_id.isdecimal():
            raise ReferenceDataError(f"{exercise_id}: id must be four ASCII digits")
        if exercise_id in seen:
            raise ReferenceDataError(f"duplicate reference id: {exercise_id}")
        seen.add(exercise_id)
        secondary = row.get("secondary_muscles")
        if not isinstance(secondary, list) or any(not isinstance(x, str) for x in secondary):
            raise ReferenceDataError(f"{exercise_id}: secondary_muscles must be text array")
        names = _languages(row, "names", exercise_id)
        review_status = _text(row, "review_status", exercise_id)
        if review_status not in REVIEW_STATUSES:
            raise ReferenceDataError(f"{exercise_id}: unknown review_status {review_status!r}")
        muscle_group = row.get("muscle_group", "")
        if not isinstance(muscle_group, str):
            raise ReferenceDataError(f"{exercise_id}: muscle_group must be text")
        for optional_key in ("primary_equipment_hint", "possible_catalog_id"):
            if optional_key in row and not isinstance(row[optional_key], str):
                raise ReferenceDataError(f"{exercise_id}: {optional_key} must be text")
        duplicate_source_name = row.get("duplicate_source_name", False)
        if not isinstance(duplicate_source_name, bool):
            raise ReferenceDataError(f"{exercise_id}: duplicate_source_name must be boolean")
        exercises.append(
            ReferenceExercise(
                id=exercise_id,
                name=names["en"],
                names=names,
                name_sources=_languages(row, "name_sources", exercise_id),
                category=_text(row, "category", exercise_id),
                equipment=_text(row, "equipment", exercise_id),
                target=_text(row, "target", exercise_id),
                muscle_group=muscle_group,
                secondary_muscles=tuple(secondary),
                instructions=_languages(row, "instructions", exercise_id),
                review_status=review_status,
                primary_equipment_hint=cast(str | None, row.get("primary_equipment_hint")),
                possible_catalog_id=cast(str | None, row.get("possible_catalog_id")),
                duplicate_source_name=duplicate_source_name,
            )
        )
    return exercises


def search_reference(
    exercises: list[ReferenceExercise], query: str, category: str, equipment: str
) -> list[ReferenceExercise]:
    """Filter the local library by translated name, target, category and equipment."""
    needle = query.strip().casefold()
    return [
        item
        for item in exercises
        if (not category or item.category == category)
        and (not equipment or item.equipment == equipment)
        and (
            not needle
            or any(needle in name.casefold() for name in item.names.values())
            or needle in item.target.casefold()
            or needle in item.muscle_group.casefold()
        )
    ]
