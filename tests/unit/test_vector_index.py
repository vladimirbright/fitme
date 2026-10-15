"""ADR 0007: vector serialization and in-process cosine similarity."""

from __future__ import annotations

import math

import pytest

from fitme.db.records import EmbeddingRecord
from fitme.services.vector_index import VectorError, VectorIndex, normalize, pack, unpack


def _record(
    exercise_id: str, vector: list[float], *, model: str = "m", digest: str = "h"
) -> EmbeddingRecord:
    return EmbeddingRecord(exercise_id, model, digest, len(vector), pack(vector), "t")


def test_pack_normalizes_and_round_trips_as_little_endian_float32() -> None:
    blob = pack([3.0, 4.0])
    assert len(blob) == 8
    assert unpack(blob, 2).tolist() == pytest.approx([0.6, 0.8])


@pytest.mark.parametrize("vector", [[], [0.0, 0.0], [math.nan, 1.0], [math.inf, 1.0]])
def test_bad_vectors_are_rejected(vector: list[float]) -> None:
    with pytest.raises(VectorError):
        normalize(vector)


def test_unpack_rejects_a_wrong_length() -> None:
    with pytest.raises(VectorError):
        unpack(b"\x00" * 12, 2)


def test_cosine_similarity_ranks_the_closest_vector_first() -> None:
    index, skipped = VectorIndex.from_records(
        "m",
        [_record("a", [1.0, 0.0]), _record("b", [0.6, 0.8]), _record("c", [0.0, 1.0])],
        valid_hashes={"a": "h", "b": "h", "c": "h"},
    )
    assert not skipped
    scores = index.similarities(normalize([1.0, 0.1]))
    assert sorted(scores, key=lambda key: -scores[key]) == ["a", "b", "c"]
    assert scores["a"] == pytest.approx(1.0 / math.sqrt(1.01), rel=1e-5)
    neighbors = index.neighbors("a")
    assert "a" not in neighbors and neighbors["b"] == pytest.approx(0.6, rel=1e-5)


def test_dimension_mismatch_is_an_error() -> None:
    index, _ = VectorIndex.from_records("m", [_record("a", [1.0, 0.0])], valid_hashes={"a": "h"})
    with pytest.raises(VectorError):
        index.similarities(normalize([1.0, 0.0, 0.0]))


def test_stale_other_model_corrupt_and_mixed_dimension_rows_are_skipped() -> None:
    corrupt = EmbeddingRecord("d", "m", "h", 2, b"\x00\x00\xc0\x7f" * 2, "t")  # NaNs
    index, skipped = VectorIndex.from_records(
        "m",
        [
            _record("a", [1.0, 0.0]),
            _record("b", [1.0, 0.0], digest="old"),
            _record("c", [1.0, 0.0], model="other"),
            corrupt,
            _record("e", [1.0, 0.0, 0.0]),
        ],
        valid_hashes={"a": "h", "b": "h", "c": "h", "d": "h", "e": "h"},
    )
    assert index.ids == ["a"]
    assert sorted(skipped) == ["b", "c", "d", "e"]


def test_an_empty_index_answers_nothing() -> None:
    index, _ = VectorIndex.from_records("m", [], valid_hashes={})
    assert len(index) == 0
    assert index.similarities(normalize([1.0])) == {}
    assert index.neighbors("a") == {}
