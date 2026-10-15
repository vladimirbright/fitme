"""Read-side access for the derived catalog search tables (ADR 0007, migration 0009)."""

from __future__ import annotations

import aiosqlite

from fitme.db.records import EmbeddingRecord


class FtsQueryError(ValueError):
    """SQLite rejected a full-text MATCH expression."""


# bm25 column weights: (exercise_id, names, body). A hit in a name or search term counts
# five times a hit in the rest of the document.
_BM25 = "bm25(catalog_search_fts, 0.0, 5.0, 1.0)"


async def fts_match(conn: aiosqlite.Connection, match: str, limit: int) -> list[tuple[str, float]]:
    """`(exercise_id, bm25)` for an FTS5 `match` expression, best first (lower bm25 is
    better). An expression SQLite rejects raises `FtsQueryError`."""
    try:
        async with conn.execute(
            f"SELECT exercise_id, {_BM25} AS score FROM catalog_search_fts "
            "WHERE catalog_search_fts MATCH ? ORDER BY score, exercise_id LIMIT ?",
            (match, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        raise FtsQueryError(str(exc)) from exc
    return [(str(row[0]), float(row[1])) for row in rows]


async def fts_exercise_ids(conn: aiosqlite.Connection) -> list[str]:
    async with conn.execute(
        "SELECT exercise_id FROM catalog_search_fts ORDER BY exercise_id"
    ) as cursor:
        rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def all_embeddings(conn: aiosqlite.Connection) -> list[EmbeddingRecord]:
    async with conn.execute(
        "SELECT exercise_id, model, content_hash, dimensions, embedding, updated_at "
        "FROM catalog_embeddings ORDER BY exercise_id"
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        EmbeddingRecord(
            exercise_id=row[0],
            model=row[1],
            content_hash=row[2],
            dimensions=row[3],
            embedding=bytes(row[4]),
            updated_at=row[5],
        )
        for row in rows
    ]
