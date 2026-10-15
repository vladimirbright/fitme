"""Suite-wide isolation: tests never reach a real embeddings provider (ADR 0007), whatever
the developer's shell exports."""

from __future__ import annotations

import pytest

_EMBEDDING_ENV = (
    "OPENAI_API_KEY",
    "FITME_EMBEDDINGS_API_KEY",
    "FITME_EMBEDDINGS_QUERY",
    "FITME_EMBEDDINGS_MODEL",
    "FITME_EMBEDDINGS_BASE_URL",
    "FITME_EMBEDDINGS_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _no_live_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _EMBEDDING_ENV:
        monkeypatch.delenv(name, raising=False)
