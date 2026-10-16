"""ADR 0009: hard exercise exclusions — "no squats", "no lunges", "never the leg press".

An exclusion is a training preference the user states, stored per user
(`exercise_exclusions`) and enforced in code, never left to a prompt:

- `pattern`: every catalog exercise of one movement pattern (`ExercisePattern`, e.g.
  `squat`, which the catalog uses for squats, lunges and step-ups alike);
- `exercise`: one catalog exercise id;
- `name`: every exercise whose id, names or search terms contain a word ("lunge",
  "выпад"), so a family the patterns don't separate can be excluded as a whole.

`resolve` turns the stored rows into the excluded id set; `services.catalog.
available_exercises` removes it from what any model or tool is offered, and the
`plan.excluded` guard rejects a plan that still names one. A preference is not a medical
fact: nothing here touches screening flags or contraindications.

ADR 0010: each exclusion has a `Scope`. "No squats" said while working on a plan is about
that plan (`plan`, or `new_plan` for the new plan being drafted, which becomes `plan` when
it is saved and is deleted when the draft is closed unsaved); only what the user says about
every plan, or adds on /profile, is `all`. `applicable` picks the rows for one plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from fitme import i18n
from fitme.db.connection import Database
from fitme.db.controllers.profile import (
    delete_exclusion,
    delete_new_plan_exclusions,
    insert_exclusion,
)
from fitme.db.records import ExclusionRecord
from fitme.db.selectors.profile import list_exclusions
from fitme.domain.catalog import Catalog, Exercise
from fitme.domain.enums import ExercisePattern

NAME_MIN_LENGTH = 3
NAME_MAX_LENGTH = 40


class Scope(StrEnum):
    ALL = "all"
    PLAN = "plan"
    NEW_PLAN = "new_plan"


class ExclusionKind(StrEnum):
    PATTERN = "pattern"
    EXERCISE = "exercise"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class ExclusionPreview:
    """What one exclusion would remove: the matching ids, in catalog order."""

    kind: ExclusionKind
    value: str
    exercise_ids: tuple[str, ...]


class InvalidExclusionError(ValueError):
    """An exclusion that names nothing real (an unknown pattern or id, a too-short word)."""


def _fold(text: str) -> str:
    return text.casefold().replace("ё", "е").replace("_", " ")


def _matches_name(exercise: Exercise, word: str) -> bool:
    haystack = [exercise.id, *exercise.names.values(), *exercise.search.values()]
    return any(word in _fold(text) for text in haystack)


def normalize(kind: str, value: str, catalog: Catalog) -> tuple[ExclusionKind, str]:
    """The stored form of one exclusion, or `InvalidExclusionError`."""
    try:
        parsed = ExclusionKind(kind)
    except ValueError as exc:
        raise InvalidExclusionError(f"unknown exclusion kind {kind!r}") from exc
    text = value.strip()
    if parsed == ExclusionKind.PATTERN:
        try:
            return parsed, ExercisePattern(text).value
        except ValueError as exc:
            patterns = ", ".join(pattern.value for pattern in ExercisePattern)
            raise InvalidExclusionError(f"unknown pattern {text!r}; one of: {patterns}") from exc
    if parsed == ExclusionKind.EXERCISE:
        if catalog.by_id(text) is None:
            raise InvalidExclusionError(f"{text!r} is not a catalog exercise id")
        return parsed, text
    word = _fold(text)
    if not NAME_MIN_LENGTH <= len(word) <= NAME_MAX_LENGTH:
        raise InvalidExclusionError(
            f"a name word must be {NAME_MIN_LENGTH}-{NAME_MAX_LENGTH} characters"
        )
    return parsed, word


def matching_ids(kind: ExclusionKind, value: str, catalog: Catalog) -> tuple[str, ...]:
    if kind == ExclusionKind.PATTERN:
        return tuple(e.id for e in catalog.exercise if e.pattern.value == value)
    if kind == ExclusionKind.EXERCISE:
        return (value,) if catalog.by_id(value) is not None else ()
    return tuple(e.id for e in catalog.exercise if _matches_name(e, value))


def preview(kind: str, value: str, catalog: Catalog) -> ExclusionPreview:
    parsed, stored = normalize(kind, value, catalog)
    return ExclusionPreview(parsed, stored, matching_ids(parsed, stored, catalog))


def label(kind: str, value: str, catalog: Catalog, lang: str) -> str:
    """How one exclusion reads to the user: a pattern's description, an exercise's name, or
    the quoted word."""
    if kind == ExclusionKind.PATTERN:
        return i18n.t(f"enum.pattern.{value}", lang)
    if kind == ExclusionKind.EXERCISE:
        exercise = catalog.by_id(value)
        if exercise is not None:
            return exercise.names.get(lang) or exercise.names.get("en", value)
    return f"“{value}”"


def applicable(
    records: Iterable[ExclusionRecord], *, plan_id: int | None, new_plan: bool = False
) -> list[ExclusionRecord]:
    """The rows that apply while working on one plan: every `all` row, plus that plan's own
    (`plan_id`), or the new-plan draft's (`new_plan`)."""
    return [
        record
        for record in records
        if record.scope == Scope.ALL
        or (record.scope == Scope.PLAN and plan_id is not None and record.plan_id == plan_id)
        or (record.scope == Scope.NEW_PLAN and new_plan)
    ]


def resolve(records: Iterable[ExclusionRecord], catalog: Catalog) -> frozenset[str]:
    """Every catalog id the stored exclusions remove. A row that no longer matches anything
    (e.g. an exercise removed from the catalog) simply contributes nothing."""
    excluded: set[str] = set()
    for record in records:
        try:
            kind = ExclusionKind(record.kind)
        except ValueError:
            continue
        excluded.update(matching_ids(kind, record.value, catalog))
    return frozenset(excluded)


async def list_for_user(db: Database, user_id: int) -> list[ExclusionRecord]:
    """Every exclusion of the user, in any scope."""
    async with db.read() as conn:
        return await list_exclusions(conn, user_id)


async def list_for_all_plans(db: Database, user_id: int) -> list[ExclusionRecord]:
    """The `all` exclusions: what /profile and the account page list."""
    return [row for row in await list_for_user(db, user_id) if row.scope == Scope.ALL]


async def add(
    db: Database,
    user_id: int,
    kind: str,
    value: str,
    catalog: Catalog,
    *,
    scope: Scope = Scope.ALL,
    plan_id: int | None = None,
) -> bool:
    """Store one exclusion (validated) in `scope` (`plan` needs `plan_id`); whether it was
    new."""
    parsed, stored = normalize(kind, value, catalog)
    if (scope == Scope.PLAN) != (plan_id is not None):
        raise InvalidExclusionError("a plan exclusion needs its plan, and only it")
    async with db.transaction() as conn:
        return (
            await insert_exclusion(
                conn,
                user_id=user_id,
                kind=parsed.value,
                value=stored,
                scope=scope.value,
                plan_id=plan_id,
            )
            is not None
        )


async def drop_new_plan(db: Database, user_id: int) -> None:
    """The new-plan draft was closed without saving: its exclusions go too."""
    async with db.transaction() as conn:
        await delete_new_plan_exclusions(conn, user_id=user_id)


async def remove(db: Database, user_id: int, exclusion_id: int) -> bool:
    async with db.transaction() as conn:
        return await delete_exclusion(conn, user_id=user_id, exclusion_id=exclusion_id)
