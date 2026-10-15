-- 0009_catalog_search.sql
--
-- ADR 0007: derived search indexes over the exercise catalog. The TOML catalog
-- (src/fitme/catalog/exercises/) stays the only source of truth; these tables hold nothing
-- that can't be rebuilt from it plus the shipped embedding index
-- (src/fitme/catalog/search_index.sqlite), and `fitme serve` rebuilds them at startup.
-- No user data: the `catalog_` prefix marks them as derived, so account export/delete skip
-- them.
--
-- `catalog_search_fts`: full-text index. `unicode61 remove_diacritics 2` folds case and
-- diacritics for Latin and Cyrillic alike (ё/е included); there is no stemmer, so Russian
-- inflection is handled by prefix queries in the search service.
-- `catalog_embeddings`: one vector per exercise. `embedding` is little-endian float32,
-- L2-normalized, `dimensions` values long; `content_hash` is the search document's
-- fingerprint it was computed from (`fitme.catalog.search_docs.document_hash`).

CREATE VIRTUAL TABLE catalog_search_fts USING fts5 (
    exercise_id UNINDEXED,
    names,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE catalog_embeddings (
    exercise_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    embedding BLOB NOT NULL CHECK (length(embedding) = dimensions * 4),
    updated_at TEXT NOT NULL
);
