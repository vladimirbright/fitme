"""Text embeddings through an OpenAI-compatible `/embeddings` endpoint (ADR 0007).

Used in two places only: `fitme catalog index` on the operator's machine (catalog search
documents, never user data) and, when `FITME_EMBEDDINGS_QUERY` is on, one call per
free-text search query. A query is already stop-word-scanned by the caller and goes through
`llm.context.scrub()` here, like every other free text sent to a provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import httpx

from fitme.config.embeddings import EmbeddingSettings
from fitme.llm.context import scrub

BATCH_SIZE = 64


class EmbeddingError(RuntimeError):
    """The provider failed or answered with something that isn't a list of vectors."""


class Embedder(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    def __init__(
        self, settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.api_key is None:
            raise EmbeddingError(
                "no embeddings API key: set FITME_EMBEDDINGS_API_KEY or OPENAI_API_KEY"
            )
        self.model = settings.model
        self._url = settings.base_url.rstrip("/") + "/embeddings"
        self._key = settings.api_key.get_secret_value()
        self._timeout = settings.timeout_s
        self._transport = transport  # tests only

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = list(texts[start : start + BATCH_SIZE])
                vectors.extend(await self._batch(client, batch))
        return vectors

    async def _batch(self, client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        try:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                json={"model": self.model, "input": batch},
            )
            response.raise_for_status()
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in ordered]
        except (httpx.HTTPError, httpx.InvalidURL, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if len(vectors) != len(batch):
            raise EmbeddingError(f"asked for {len(batch)} embeddings, got {len(vectors)}")
        return vectors


async def embed_query(embedder: Embedder, text: str) -> list[float]:
    """One scrubbed free-text query -> one vector."""
    vectors = await embedder.embed([scrub(text)])
    if len(vectors) != 1:
        raise EmbeddingError("expected exactly one query embedding")
    return vectors[0]
