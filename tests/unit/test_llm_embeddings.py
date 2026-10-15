"""ADR 0007: the OpenAI-compatible embedder's request/response handling, and query
scrubbing. No network: `httpx.MockTransport` answers."""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest

from fitme.config.embeddings import EmbeddingSettings
from fitme.llm.embeddings import (
    BATCH_SIZE,
    EmbeddingError,
    OpenAICompatibleEmbedder,
    embed_query,
)


def _settings() -> EmbeddingSettings:
    return EmbeddingSettings(
        FITME_EMBEDDINGS_API_KEY="sk-test",
        FITME_EMBEDDINGS_BASE_URL="https://embeddings.example/v1/",
        FITME_EMBEDDINGS_MODEL="m-1",
    )


def _embedder(handler: object) -> OpenAICompatibleEmbedder:
    return OpenAICompatibleEmbedder(_settings(), transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_batches_are_sent_with_model_and_key_and_reordered_by_index() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://embeddings.example/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        seen.append(body)
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    texts = [f"t{i}" for i in range(BATCH_SIZE + 3)]
    vectors = await _embedder(handler).embed(texts)
    assert [len(batch["input"]) for batch in seen] == [BATCH_SIZE, 3]  # type: ignore[arg-type]
    assert all(batch["model"] == "m-1" for batch in seen)
    assert vectors[0] == [0.0, 1.0] and vectors[BATCH_SIZE] == [0.0, 1.0]
    assert vectors[2] == [2.0, 1.0]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": "down"}),
        httpx.Response(200, json={"nope": []}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"data": [{"index": 0, "embedding": ["x"]}]}),
        httpx.Response(200, json={"data": []}),  # wrong count
    ],
)
async def test_bad_answers_become_embedding_errors(response: httpx.Response) -> None:
    with pytest.raises(EmbeddingError):
        await _embedder(lambda request: response).embed(["a"])


def test_a_missing_key_is_a_clear_error() -> None:
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        OpenAICompatibleEmbedder(EmbeddingSettings())


def test_query_embedding_is_off_unless_asked_and_keyed() -> None:
    assert EmbeddingSettings().query_enabled is False
    assert EmbeddingSettings(FITME_EMBEDDINGS_QUERY=True).query_enabled is False
    keyed = EmbeddingSettings(FITME_EMBEDDINGS_QUERY=True, FITME_EMBEDDINGS_API_KEY="k")
    assert keyed.query_enabled is True


class _Recorder:
    model = "m"

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


async def test_a_query_is_scrubbed_before_it_is_embedded() -> None:
    recorder = _Recorder()
    await embed_query(recorder, "rows like @someone showed, mail me at me@example.org")
    sent = recorder.texts[0]
    assert "@someone" not in sent and "me@example.org" not in sent
    assert "rows like" in sent
