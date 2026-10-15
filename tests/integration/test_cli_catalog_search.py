"""ADR 0007: `fitme catalog index|search|substitutes` and the index line in `catalog check`.
No provider is called: `index` gets a fake embedder, `search` runs without `--semantic`."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from fitme.cli import catalog_search
from fitme.cli.main import main
from fitme.db.search_index_file import read_index_file


class FakeEmbedder:
    model = "fake-1"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [1.0 + int(hashlib.sha256(text.encode()).hexdigest()[:4], 16) % 7, 1.0]
            for text in texts
        ]


def test_search_finds_a_russian_name_without_any_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FITME_EMBEDDINGS_API_KEY", raising=False)
    assert main(["catalog", "search", "жим гантелей", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(" 1. dumbbell_bench_press")


def test_search_filters_by_pattern_location_and_area(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "catalog",
                "search",
                "--pattern",
                "vertical_pull",
                "--location",
                "outdoor",
                "--avoid-area",
                "neck",
                "--limit",
                "20",
            ]
        )  # fmt: skip
        == 0
    )
    out = capsys.readouterr().out
    assert "pull_up" not in out  # loads the neck
    assert "no matching exercises" in out or "pattern: vertical_pull" in out


def test_substitutes_for_a_bench_press_with_dumbbells(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "catalog", "substitutes", "barbell_bench_press", "--location", "home_equipment",
        "--equipment", "dumbbells,bench", "--limit", "3",
    ]  # fmt: skip
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert (
        out.splitlines()[0]
        == " 1. dumbbell_bench_press [close] — Dumbbell bench press / Жим гантелей лёжа"
    )


def test_substitutes_for_an_unknown_id_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "substitutes", "no_such_exercise"]) == 1
    assert "unknown exercise id" in capsys.readouterr().err


def test_index_writes_then_reports_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "index.sqlite"
    assert catalog_search.index(force=False, path=path, embedder=FakeEmbedder()) == 0
    assert "Index written" in capsys.readouterr().out
    assert len(read_index_file(path)) > 100
    assert catalog_search.index(force=False, path=path, embedder=FakeEmbedder()) == 0
    out = capsys.readouterr().out
    assert "Embeddings generated:  0" in out and "Index up to date." in out


def test_index_without_a_key_explains_what_to_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FITME_EMBEDDINGS_API_KEY", raising=False)
    assert catalog_search.index(force=False, path=tmp_path / "i.sqlite") == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_catalog_check_reports_the_search_index(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog", "check"]) == 0
    assert "search index:" in capsys.readouterr().out
