"""Write-side access for the derived catalog search tables (ADR 0007, migration 0009)."""

from __future__ import annotations

from collections.abc import Sequence

import aiosqlite

from fitme.db.records import EmbeddingRecord


async def replace_fts(conn: aiosqlite.Connection, rows: Sequence[tuple[str, str, str]]) -> None:
    """Replace the whole full-text index with `(exercise_id, names, body)` rows."""
    await conn.execute("DELETE FROM catalog_search_fts")
    await conn.executemany(
        "INSERT INTO catalog_search_fts (exercise_id, names, body) VALUES (?, ?, ?)", rows
    )


async def replace_embeddings(
    conn: aiosqlite.Connection, records: Sequence[EmbeddingRecord]
) -> None:
    """Replace every stored embedding with `records`."""
    await conn.execute("DELETE FROM catalog_embeddings")
    await conn.executemany(
        "INSERT INTO catalog_embeddings "
        "(exercise_id, model, content_hash, dimensions, embedding, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (r.exercise_id, r.model, r.content_hash, r.dimensions, r.embedding, r.updated_at)
            for r in records
        ],
    )
