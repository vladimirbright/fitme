"""The shipped embedding index file (ADR 0007): `src/fitme/catalog/search_index.sqlite`.

A small standalone SQLite file, written on the operator's machine by `fitme catalog index`
(the only step that calls the embedding provider) and committed with the catalog, so a
deploy carries it like any other package data. `fitme serve` imports it into the app DB's
`catalog_embeddings` at startup. It holds derived data only: one row per exercise with the
search document it was computed from, that document's fingerprint and the vector.

Synchronous `sqlite3` on purpose: it runs from the CLI and once at startup, on a file of a
few megabytes at most.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fitme.db.records import EmbeddingRecord

FILE_FORMAT = "1"


class SearchIndexFileError(RuntimeError):
    """The index file exists but can't be read as a Fitme search index."""


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    record: EmbeddingRecord
    document: str


def read_index_file(path: Path) -> list[IndexedDocument]:
    """Every row of the index file, sorted by exercise id; `[]` if the file is absent."""
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            if meta.get("format") != FILE_FORMAT:
                raise SearchIndexFileError(
                    f"{path}: unsupported index format {meta.get('format')!r}"
                )
            rows = conn.execute(
                "SELECT exercise_id, model, content_hash, dimensions, embedding, updated_at, "
                "document FROM embeddings ORDER BY exercise_id"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SearchIndexFileError(f"{path}: {exc}") from exc
    return [
        IndexedDocument(
            record=EmbeddingRecord(
                exercise_id=row[0],
                model=row[1],
                content_hash=row[2],
                dimensions=row[3],
                embedding=bytes(row[4]),
                updated_at=row[5],
            ),
            document=row[6],
        )
        for row in rows
    ]


def write_index_file(path: Path, rows: Sequence[IndexedDocument]) -> None:
    """Write a complete index file atomically: a temporary file next to `path`, then a
    rename, so a failed run never leaves a half-written index behind."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        _write(tmp, rows)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _write(tmp: Path, rows: Sequence[IndexedDocument]) -> None:
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE embeddings ("
            " exercise_id TEXT PRIMARY KEY, model TEXT NOT NULL, content_hash TEXT NOT NULL,"
            " dimensions INTEGER NOT NULL, embedding BLOB NOT NULL, updated_at TEXT NOT NULL,"
            " document TEXT NOT NULL);"
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FILE_FORMAT,))
        conn.executemany(
            "INSERT INTO embeddings (exercise_id, model, content_hash, dimensions, embedding, "
            "updated_at, document) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.record.exercise_id,
                    row.record.model,
                    row.record.content_hash,
                    row.record.dimensions,
                    row.record.embedding,
                    row.record.updated_at,
                    row.document,
                )
                for row in sorted(rows, key=lambda item: item.record.exercise_id)
            ],
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
