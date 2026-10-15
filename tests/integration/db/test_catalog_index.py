"""ADR 0007: the shipped index file stays in step with the catalog, incrementally, and the app
DB's derived tables are rebuilt from both. Embeddings are faked; no provider is called."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from fitme.catalog import load_catalog
from fitme.db.connection import Database
from fitme.db.search_index_file import read_index_file
from fitme.db.selectors.catalog_search import all_embeddings, fts_exercise_ids
from fitme.domain.catalog import Catalog
from fitme.llm.embeddings import EmbeddingError
from fitme.services.catalog_index import (
    build_index_file,
    default_index_path,
    index_status,
    sync_database,
)


class FakeEmbedder:
    """Deterministic bag-of-words vectors; records every batch it was asked for."""

    def __init__(self, model: str = "fake-1", dims: int = 32, fail: bool = False) -> None:
        self.model = model
        self.dims = dims
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("provider down")
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in text.lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dims] += 1.0
        vector[0] += 0.01  # never a zero vector
        return vector


def _small(catalog: Catalog, ids: Sequence[str]) -> Catalog:
    return Catalog(exercise=[e for e in catalog.exercise if e.id in ids])


_IDS = ("barbell_bench_press", "dumbbell_bench_press", "pull_up", "bodyweight_squat")


async def test_initial_build_then_no_op_rerun(tmp_path: Path) -> None:
    catalog = _small(load_catalog(), _IDS)
    path = tmp_path / "index.sqlite"
    embedder = FakeEmbedder()

    first = await build_index_file(catalog, embedder, path)
    assert sorted(first.new) == sorted(_IDS) and first.embedded == 4
    assert [row.record.exercise_id for row in read_index_file(path)] == sorted(_IDS)

    second = await build_index_file(catalog, embedder, path)
    assert second.embedded == 0 and sorted(second.unchanged) == sorted(_IDS)
    assert len(embedder.calls) == 1  # the rerun never called the provider


async def test_only_modified_and_new_are_embedded_and_removed_are_dropped(tmp_path: Path) -> None:
    base = load_catalog()
    path = tmp_path / "index.sqlite"
    embedder = FakeEmbedder()
    await build_index_file(_small(base, _IDS), embedder, path)

    changed = []
    for exercise in base.exercise:
        if exercise.id == "pull_up":
            exercise = exercise.model_copy(
                update={"names": {"en": "Pull-up v2", "ru": "Подтягивания"}}
            )
        if exercise.id in (*_IDS, "chin_up") and exercise.id != "bodyweight_squat":
            changed.append(exercise)
    report = await build_index_file(Catalog(exercise=changed), embedder, path)

    assert report.modified == ["pull_up"]
    assert report.new == ["chin_up"]
    assert report.removed == ["bodyweight_squat"]
    assert sorted(report.unchanged) == ["barbell_bench_press", "dumbbell_bench_press"]
    assert embedder.calls[-1] and len(embedder.calls[-1]) == 2
    assert "bodyweight_squat" not in {row.record.exercise_id for row in read_index_file(path)}


async def test_model_change_and_force_reembed_everything(tmp_path: Path) -> None:
    catalog = _small(load_catalog(), _IDS)
    path = tmp_path / "index.sqlite"
    await build_index_file(catalog, FakeEmbedder("fake-1"), path)

    other = await build_index_file(catalog, FakeEmbedder("fake-2"), path)
    assert sorted(other.modified) == sorted(_IDS)
    assert {row.record.model for row in read_index_file(path)} == {"fake-2"}

    forced = await build_index_file(catalog, FakeEmbedder("fake-2"), path, force=True)
    assert forced.embedded == 4


async def test_a_provider_failure_writes_nothing(tmp_path: Path) -> None:
    catalog = _small(load_catalog(), _IDS)
    path = tmp_path / "index.sqlite"
    await build_index_file(catalog, FakeEmbedder(), path)
    before = path.read_bytes()
    renamed = Catalog(
        exercise=[
            e.model_copy(update={"names": {"en": e.id, "ru": e.id}}) for e in catalog.exercise
        ]
    )
    with pytest.raises(EmbeddingError):
        await build_index_file(renamed, FakeEmbedder(fail=True), path)
    assert path.read_bytes() == before


async def test_indexing_never_touches_the_toml_catalog(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "fitme" / "catalog" / "exercises"
    snapshot = {p: p.read_bytes() for p in root.rglob("*.toml")}
    await build_index_file(load_catalog(), FakeEmbedder(), tmp_path / "index.sqlite")
    assert {p: p.read_bytes() for p in root.rglob("*.toml")} == snapshot


async def test_sync_rebuilds_fts_and_imports_only_current_embeddings(
    db: Database, tmp_path: Path
) -> None:
    base = load_catalog()
    path = tmp_path / "index.sqlite"
    await build_index_file(_small(base, _IDS), FakeEmbedder(), path)
    # The catalog moved on: pull_up's document changed, squat is gone, chin_up is new.
    current = Catalog(
        exercise=[
            e.model_copy(update={"names": {"en": "Pull-up v2", "ru": "Подтягивания"}})
            if e.id == "pull_up"
            else e
            for e in base.exercise
            if e.id in (*_IDS, "chin_up") and e.id != "bodyweight_squat"
        ]
    )
    report = await sync_database(db, current, path)
    assert report.fts_rows == 4
    assert report.embeddings == 2
    assert sorted(report.skipped) == ["bodyweight_squat", "pull_up"]
    async with db.read() as conn:
        assert await fts_exercise_ids(conn) == sorted(e.id for e in current.exercise)
        stored = await all_embeddings(conn)
    assert sorted(r.exercise_id for r in stored) == ["barbell_bench_press", "dumbbell_bench_press"]

    status = index_status(current, path)
    assert status.missing == ("chin_up",) and status.stale == ("pull_up",)
    assert status.removed == ("bodyweight_squat",) and not status.up_to_date


async def test_sync_without_an_index_file_still_builds_full_text(
    db: Database, tmp_path: Path
) -> None:
    report = await sync_database(db, load_catalog(), tmp_path / "missing.sqlite")
    assert report.fts_rows == len(load_catalog().exercise) and report.embeddings == 0


def test_the_shipped_index_matches_the_catalog() -> None:
    """The committed index file was rebuilt after the last catalog change (`make
    catalog-index`). If this fails after editing the catalog, re-run that target."""
    status = index_status(load_catalog(), default_index_path())
    assert status.up_to_date, status.lines()
