"""`fitme catalog index|search|substitutes` (ADR 0007).

`index` is the operator's maintenance step: it embeds new or changed search documents and
rewrites the shipped index file (`catalog/search_index.sqlite`); it needs only the embedding
variables (`config/embeddings.py`), not the bot's settings. `search` and `substitutes` run
against a scratch in-memory database built from the catalog and that file, so they work
without a deployment database and, unless `--semantic` is given, without any provider call.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from fitme.catalog import load_catalog
from fitme.config.embeddings import EmbeddingSettings
from fitme.db.connection import Database, open_database
from fitme.db.migrate import migrate
from fitme.db.search_index_file import SearchIndexFileError
from fitme.domain.catalog import (
    HOME_SELECTABLE_EQUIPMENT,
    LOCATION_DEFAULT_EQUIPMENT,
    Catalog,
    Exercise,
)
from fitme.domain.enums import (
    Equipment,
    ExerciseKind,
    ExercisePattern,
    Experience,
    Location,
    ScreeningFlag,
)
from fitme.domain.screening import ScreeningFlagState
from fitme.llm.embeddings import Embedder, EmbeddingError, OpenAICompatibleEmbedder
from fitme.services import catalog_index, exercise_search
from fitme.services.catalog import available_exercises


def index(*, force: bool, path: Path | None = None, embedder: Embedder | None = None) -> int:
    target = path or catalog_index.default_index_path()
    try:
        if embedder is None:
            embedder = OpenAICompatibleEmbedder(EmbeddingSettings())
        report = asyncio.run(
            catalog_index.build_index_file(load_catalog(), embedder, target, force=force)
        )
    except (EmbeddingError, SearchIndexFileError, ValidationError) as exc:
        print(f"fitme catalog index: {exc}", file=sys.stderr)
        return 1
    for line in report.lines():
        print(line)
    print("")
    print(f"Index written: {target}" if report.embedded or report.removed else "Index up to date.")
    return 0


def _parse_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def candidates(
    catalog: Catalog,
    *,
    location: str,
    equipment: str | None,
    flags: Sequence[str],
    experience: str | None = None,
) -> list[Exercise]:
    """The hard filter, exactly as plans use it: location, equipment, contraindications."""
    where = Location(location)
    if equipment is not None:
        kit = frozenset(Equipment(item) for item in _parse_list(equipment))
    elif where == Location.HOME_EQUIPMENT:
        kit = HOME_SELECTABLE_EQUIPMENT
    else:
        kit = LOCATION_DEFAULT_EQUIPMENT[where]
    states = [ScreeningFlagState(flag=ScreeningFlag(flag), value="yes") for flag in flags]
    level = Experience(experience) if experience else None
    return available_exercises(catalog, where, kit, states, level)


async def _scratch(
    catalog: Catalog, semantic: bool
) -> tuple[Database, exercise_search.SearchRuntime]:
    db = await open_database(":memory:")
    await migrate(db)
    report = await catalog_index.sync_database(db, catalog, catalog_index.default_index_path())
    if report.skipped:
        print(f"note: {len(report.skipped)} stale embeddings ignored", file=sys.stderr)
    settings = EmbeddingSettings()
    embedder = OpenAICompatibleEmbedder(settings) if semantic else None
    runtime = await exercise_search.load_runtime(db, catalog, settings, embedder)
    return db, runtime


def _print(catalog: Catalog, results: Sequence[exercise_search.ExerciseSearchResult]) -> None:
    if not results:
        print("no matching exercises")
    for rank, result in enumerate(results, start=1):
        exercise = catalog.by_id(result.exercise_id)
        names = (
            ""
            if exercise is None
            else f"{exercise.names.get('en', '')} / {exercise.names.get('ru', '')}"
        )
        marker = " [close]" if result.close else ""
        print(f"{rank:>2}. {result.exercise_id}{marker} — {names}")
        print(f"    score {result.score:.4f}: {'; '.join(result.match_reasons)}")


def search(
    text: str | None,
    *,
    location: str,
    equipment: str | None,
    flags: Sequence[str],
    experience: str | None = None,
    patterns: Sequence[str],
    kinds: Sequence[str],
    exclude: str | None,
    avoid_areas: Sequence[str] = (),
    limit: int,
    semantic: bool,
) -> int:
    catalog = load_catalog()

    async def run() -> int:
        db, runtime = await _scratch(catalog, semantic)
        try:
            if semantic and not len(runtime.vectors):
                print("note: no embeddings for the configured model; lexical only", file=sys.stderr)
            query = exercise_search.ExerciseQuery(
                text=text,
                patterns=frozenset(ExercisePattern(item) for item in patterns),
                kinds=frozenset(ExerciseKind(item) for item in kinds),
                excluded=frozenset(_parse_list(exclude)),
                avoid_areas=frozenset(avoid_areas),
                limit=limit,
            )
            pool = candidates(
                catalog, location=location, equipment=equipment, flags=flags, experience=experience
            )
            _print(catalog, await exercise_search.search(db, runtime, pool, query))
        finally:
            await db.close()
        return 0

    try:
        return asyncio.run(run())
    except (ValueError, EmbeddingError, SearchIndexFileError) as exc:
        print(f"fitme catalog search: {exc}", file=sys.stderr)
        return 1


def substitutes(
    exercise_id: str,
    *,
    location: str,
    equipment: str | None,
    flags: Sequence[str],
    exclude: str | None,
    experience: str | None = None,
    avoid_areas: Sequence[str] = (),
    limit: int,
) -> int:
    catalog = load_catalog()

    async def run() -> int:
        db, runtime = await _scratch(catalog, semantic=False)
        try:
            pool = candidates(
                catalog, location=location, equipment=equipment, flags=flags, experience=experience
            )
            results = exercise_search.find_substitutes(
                runtime,
                exercise_id,
                pool,
                excluded=_parse_list(exclude),
                avoid_areas=avoid_areas,
                limit=limit,
            )
            _print(catalog, results)
        finally:
            await db.close()
        return 0

    try:
        return asyncio.run(run())
    except exercise_search.UnknownExerciseError:
        print(f"fitme catalog substitutes: unknown exercise id {exercise_id!r}", file=sys.stderr)
        return 1
    except (ValueError, SearchIndexFileError) as exc:
        print(f"fitme catalog substitutes: {exc}", file=sys.stderr)
        return 1
