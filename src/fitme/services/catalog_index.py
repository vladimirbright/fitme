"""Keeping the derived search indexes in step with the TOML catalog (ADR 0007).

Two steps, deliberately separate:

1. **`build_index_file`** — `fitme catalog index`, on the operator's machine. Builds every
   exercise's search document, reuses each embedding in the existing index file whose
   document fingerprint and model are unchanged, embeds only new or changed documents, drops
   removed exercises, and rewrites `catalog/search_index.sqlite`. The only step that calls
   the embedding provider. Idempotent: a second run with no catalog change embeds nothing.
2. **`sync_database`** — `fitme serve` at startup (and the CLI search commands, against a
   scratch database). Rebuilds the full-text index from the catalog and imports the
   embeddings whose fingerprint still matches the current document. No provider call.

The catalog is never written. An exercise whose embedding is missing or stale is still
found by exact and full-text search and by catalog structure; `index_status` and
`fitme catalog check` report it so the operator knows to re-run the index.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass, field
from pathlib import Path

from fitme.catalog.search_docs import document_hash, lexical_fields, search_document
from fitme.clock import utc_now
from fitme.db.connection import Database
from fitme.db.controllers.catalog_search import replace_embeddings, replace_fts
from fitme.db.records import EmbeddingRecord
from fitme.db.search_index_file import IndexedDocument, read_index_file, write_index_file
from fitme.domain.catalog import Catalog
from fitme.llm.embeddings import Embedder, EmbeddingError
from fitme.services.vector_index import VectorError, pack

INDEX_FILENAME = "search_index.sqlite"


def default_index_path() -> Path:
    """The index file shipped next to the catalog (package data, so a checkout, a wheel and
    the Docker image all find it the same way)."""
    return Path(str(importlib.resources.files("fitme.catalog") / INDEX_FILENAME))


def documents(catalog: Catalog) -> dict[str, tuple[str, str]]:
    """exercise id -> (search document, fingerprint), for the whole catalog."""
    result: dict[str, tuple[str, str]] = {}
    for exercise in catalog.exercise:
        document = search_document(exercise)
        result[exercise.id] = (document, document_hash(document))
    return result


@dataclass(slots=True)
class IndexReport:
    model: str
    total: int = 0
    unchanged: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def embedded(self) -> int:
        return len(self.new) + len(self.modified)

    def lines(self) -> list[str]:
        return [
            "Exercise catalog indexing",
            "",
            f"Model:                 {self.model}",
            f"Total exercises:       {self.total}",
            f"Unchanged:             {len(self.unchanged)}",
            f"New:                   {len(self.new)}",
            f"Modified:              {len(self.modified)}",
            f"Removed:               {len(self.removed)}",
            "",
            f"Embeddings generated:  {self.embedded}",
        ]


async def build_index_file(
    catalog: Catalog, embedder: Embedder, path: Path, *, force: bool = False
) -> IndexReport:
    """Bring the index file at `path` in line with `catalog`. `force` re-embeds everything.
    On a provider error nothing is written and `EmbeddingError` propagates."""
    current = documents(catalog)
    # `force` re-embeds everything, so it never needs the old file (which may be corrupt).
    previous = {} if force else {row.record.exercise_id: row for row in read_index_file(path)}
    report = IndexReport(model=embedder.model, total=len(current))
    report.removed = sorted(set(previous) - set(current))
    keep: list[IndexedDocument] = []
    todo: list[str] = []
    for exercise_id in sorted(current):
        document, digest = current[exercise_id]
        old = previous.get(exercise_id)
        reusable = (
            not force
            and old is not None
            and old.record.model == embedder.model
            and old.record.content_hash == digest
            and old.record.dimensions > 0
            and len(old.record.embedding) == old.record.dimensions * 4
        )
        if reusable:
            assert old is not None
            keep.append(old)
            report.unchanged.append(exercise_id)
            continue
        todo.append(exercise_id)
        (report.new if old is None else report.modified).append(exercise_id)
    vectors = (
        await embedder.embed([current[exercise_id][0] for exercise_id in todo]) if todo else []
    )
    if len(vectors) != len(todo):
        raise EmbeddingError(f"asked for {len(todo)} embeddings, got {len(vectors)}")
    dims = {len(vector) for vector in vectors} | {row.record.dimensions for row in keep}
    if len(dims) > 1:
        raise EmbeddingError(f"embeddings of mixed dimensions {sorted(dims)}; use --force")
    stamp = utc_now()
    for exercise_id, vector in zip(todo, vectors, strict=True):
        document, digest = current[exercise_id]
        try:
            blob = pack(vector)
        except VectorError as exc:
            raise EmbeddingError(f"{exercise_id}: {exc}") from exc
        record = EmbeddingRecord(exercise_id, embedder.model, digest, len(vector), blob, stamp)
        keep.append(IndexedDocument(record=record, document=document))
    if todo or report.removed or not path.is_file():
        write_index_file(path, keep)
    return report


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """How the index file relates to the current catalog."""

    total: int
    indexed: int
    models: tuple[str, ...]
    missing: tuple[str, ...]  # in the catalog, no vector at all
    stale: tuple[str, ...]  # vector computed from a different document
    removed: tuple[str, ...]  # vector for an exercise no longer in the catalog

    @property
    def up_to_date(self) -> bool:
        return not (self.missing or self.stale or self.removed) and len(self.models) <= 1

    def lines(self) -> list[str]:
        lines = [
            f"search index: {self.indexed}/{self.total} exercises embedded"
            + (f" ({', '.join(self.models)})" if self.models else "")
        ]
        for label, ids in (
            ("missing", self.missing),
            ("stale", self.stale),
            ("removed", self.removed),
        ):
            if ids:
                shown = ", ".join(ids[:10]) + (f", ... (+{len(ids) - 10})" if len(ids) > 10 else "")
                lines.append(f"search index: {len(ids)} {label}: {shown}")
        if len(self.models) > 1:
            lines.append("search index: built with more than one model; run with --force")
        if not self.up_to_date:
            lines.append("search index: run `make catalog-index` to update it")
        return lines


def index_status(catalog: Catalog, path: Path) -> IndexStatus:
    current = documents(catalog)
    rows = {row.record.exercise_id: row.record for row in read_index_file(path)}
    stale = sorted(
        exercise_id
        for exercise_id, record in rows.items()
        if exercise_id in current and record.content_hash != current[exercise_id][1]
    )
    return IndexStatus(
        total=len(current),
        indexed=len([exercise_id for exercise_id in rows if exercise_id in current]) - len(stale),
        models=tuple(sorted({record.model for record in rows.values()})),
        missing=tuple(sorted(set(current) - set(rows))),
        stale=tuple(stale),
        removed=tuple(sorted(set(rows) - set(current))),
    )


@dataclass(frozen=True, slots=True)
class SyncReport:
    fts_rows: int
    embeddings: int
    skipped: tuple[str, ...]  # stale, removed or unreadable index rows not imported


async def sync_database(db: Database, catalog: Catalog, path: Path | None) -> SyncReport:
    """Rebuild the app DB's derived search tables from the catalog and the index file, in one
    transaction. Only embeddings matching the current documents are imported; `path=None`
    imports none (full-text only). Callers reload `exercise_search.load_runtime` afterwards:
    a cached runtime still holds the vectors it read before."""
    current = documents(catalog)
    fts_rows = [(exercise.id, *lexical_fields(exercise)) for exercise in catalog.exercise]
    imported: list[EmbeddingRecord] = []
    skipped: list[str] = []
    for row in read_index_file(path) if path is not None else []:
        record = row.record
        valid = (
            record.exercise_id in current
            and record.content_hash == current[record.exercise_id][1]
            and record.dimensions > 0
            and len(record.embedding) == record.dimensions * 4
        )
        (imported.append(record) if valid else skipped.append(record.exercise_id))
    async with db.transaction() as conn:
        await replace_fts(conn, fts_rows)
        await replace_embeddings(conn, imported)
    return SyncReport(fts_rows=len(fts_rows), embeddings=len(imported), skipped=tuple(skipped))
