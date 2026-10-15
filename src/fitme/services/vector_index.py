"""In-process cosine similarity over the catalog embeddings (ADR 0007).

Vectors are stored L2-normalized as little-endian float32 (`pack`/`unpack`), so cosine
similarity is a dot product. At a few thousand exercises one matrix-vector product is well
under a millisecond; no vector database is needed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import numpy as np
import numpy.typing as npt

from fitme.db.records import EmbeddingRecord

_DTYPE = np.dtype("<f4")


class VectorError(ValueError):
    """A vector is empty, non-finite, zero-length or of the wrong size."""


def normalize(vector: Sequence[float]) -> npt.NDArray[np.float32]:
    array = np.asarray(vector, dtype=np.float64)
    if array.ndim != 1 or array.size == 0:
        raise VectorError("a vector must be a non-empty list of numbers")
    if not np.all(np.isfinite(array)):
        raise VectorError("a vector must contain only finite numbers")
    norm = math.sqrt(float(np.dot(array, array)))
    if norm == 0.0:
        raise VectorError("a zero vector has no direction")
    return (array / norm).astype(_DTYPE)


def pack(vector: Sequence[float]) -> bytes:
    """Normalize and serialize: little-endian float32, `len(vector) * 4` bytes."""
    return normalize(vector).tobytes()


def unpack(blob: bytes, dimensions: int) -> npt.NDArray[np.float32]:
    if dimensions <= 0 or len(blob) != dimensions * 4:
        raise VectorError(f"expected {dimensions * 4} bytes for {dimensions} dims, got {len(blob)}")
    array = np.frombuffer(blob, dtype=_DTYPE)
    if not np.all(np.isfinite(array)):
        raise VectorError("stored vector contains non-finite values")
    return array


class VectorIndex:
    """Exercise id -> normalized vector, all of one model and one dimensionality."""

    def __init__(self, model: str, ids: list[str], matrix: npt.NDArray[np.float32]) -> None:
        self.model = model
        self.ids = ids
        self._matrix = matrix
        self._row = {exercise_id: row for row, exercise_id in enumerate(ids)}

    @classmethod
    def from_records(
        cls, model: str, records: Iterable[EmbeddingRecord], *, valid_hashes: dict[str, str]
    ) -> tuple[VectorIndex, list[str]]:
        """Build from stored rows, keeping only rows of `model` whose `content_hash` matches
        the current search document (`valid_hashes`: exercise id -> hash). Returns the index
        and the ids skipped as stale, of another model, or unreadable."""
        ids: list[str] = []
        rows: list[npt.NDArray[np.float32]] = []
        skipped: list[str] = []
        dims: int | None = None
        for record in records:
            if record.model != model or valid_hashes.get(record.exercise_id) != record.content_hash:
                skipped.append(record.exercise_id)
                continue
            try:
                vector = unpack(record.embedding, record.dimensions)
            except VectorError:
                skipped.append(record.exercise_id)
                continue
            if dims is not None and vector.size != dims:
                skipped.append(record.exercise_id)
                continue
            dims = vector.size
            ids.append(record.exercise_id)
            rows.append(vector)
        matrix = np.vstack(rows) if rows else np.zeros((0, 0), dtype=_DTYPE)
        return cls(model, ids, matrix), skipped

    def __len__(self) -> int:
        return len(self.ids)

    def __contains__(self, exercise_id: object) -> bool:
        return exercise_id in self._row

    @property
    def dimensions(self) -> int:
        return int(self._matrix.shape[1]) if len(self.ids) else 0

    def similarities(self, vector: npt.NDArray[np.float32]) -> dict[str, float]:
        """Cosine similarity of `vector` (already normalized) to every indexed exercise."""
        if not self.ids:
            return {}
        if vector.size != self.dimensions:
            raise VectorError(f"query has {vector.size} dimensions, index has {self.dimensions}")
        scores = self._matrix @ vector
        return {exercise_id: float(scores[row]) for row, exercise_id in enumerate(self.ids)}

    def vector(self, exercise_id: str) -> npt.NDArray[np.float32] | None:
        row = self._row.get(exercise_id)
        return None if row is None else self._matrix[row]

    def neighbors(self, exercise_id: str) -> dict[str, float]:
        """Similarity of every other indexed exercise to `exercise_id`; `{}` if unindexed."""
        vector = self.vector(exercise_id)
        if vector is None:
            return {}
        scores = self.similarities(vector)
        scores.pop(exercise_id, None)
        return scores
