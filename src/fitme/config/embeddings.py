"""Embedding provider settings (ADR 0007), separate from `Settings` on purpose.

`fitme catalog index` runs on the operator's machine with only these variables set (no bot
token, no database), and the server needs none of them unless runtime query embedding is
switched on. Any OpenAI-compatible `/embeddings` endpoint works.

- `FITME_EMBEDDINGS_MODEL` (default `text-embedding-3-small`): the model the shipped index is
  built with. The server only uses vectors built with the same model it would embed queries
  with, so changing it means re-running `fitme catalog index`.
- `FITME_EMBEDDINGS_BASE_URL` (default `https://api.openai.com/v1`).
- `FITME_EMBEDDINGS_API_KEY`, else `OPENAI_API_KEY`.
- `FITME_EMBEDDINGS_QUERY` (default false): embed free-text search queries at request time.
  Off, search uses exact, full-text and catalog-structure matching plus the shipped
  exercise-to-exercise similarity, with no provider call at all.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    model: str = Field(default="text-embedding-3-small", alias="FITME_EMBEDDINGS_MODEL")
    base_url: str = Field(default="https://api.openai.com/v1", alias="FITME_EMBEDDINGS_BASE_URL")
    api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("FITME_EMBEDDINGS_API_KEY", "OPENAI_API_KEY")
    )
    query: bool = Field(default=False, alias="FITME_EMBEDDINGS_QUERY")
    timeout_s: float = Field(default=30.0, gt=0, alias="FITME_EMBEDDINGS_TIMEOUT_S")

    @property
    def query_enabled(self) -> bool:
        return self.query and self.api_key is not None
