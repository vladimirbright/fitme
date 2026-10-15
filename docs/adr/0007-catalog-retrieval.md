# ADR 0007 — Catalog retrieval: search, substitutes and plan candidate pools

Status: accepted (2026-10-15). Adds to A§4.4 (catalog), A§6.4 (plan generation), A§8.1
(`plan_generate.v4`, `assistant.v6`) and A§11 (CLI).

## Context

The catalog grew to 190 exercises and keeps growing. `plan_generate` received only the bare
list of allowed ids, with no names or patterns, and the assistant's `find_exercises` was a
substring match. Replacing an exercise had no ranked help at all.

## Decision

**The TOML catalog stays the only source of truth.** Everything below is derived from it and
can be rebuilt; nothing writes the catalog.

- **Search documents** (`catalog/search_docs.py`): one deterministic text per exercise from
  its own fields: en/ru names, search terms, pattern, kind, equipment, loaded areas and the
  first sentence of each instruction. The catalog has no muscle fields, so none are invented.
  Each document has a SHA-256 fingerprint that includes a format version.
- **Embeddings are built locally and shipped.** `fitme catalog index` (`make catalog-index`)
  runs on the operator's machine with an OpenAI-compatible key. It embeds only new or changed
  documents (fingerprint or model changed), drops removed exercises and rewrites
  `src/fitme/catalog/search_index.sqlite`, which is committed and deployed as package data.
  `--force` (or `reindex --force`) re-embeds everything. The default model is
  `text-embedding-3-small`. Vectors are stored as little-endian float32, L2-normalized.
- **At startup** `fitme serve` imports the file into the app DB (`catalog_embeddings`, only
  rows whose fingerprint matches the current document) and rebuilds the FTS5 index
  (`catalog_search_fts`) from the catalog. No provider is called. Migration 0009 adds both
  tables; the `catalog_` prefix marks them as derived, so account export/delete skip them.
- **Retrieval** (`services/exercise_search.py`) only ranks a set the caller has already
  filtered with `available_exercises` (location, equipment, contraindications). It can never
  add an exercise, and safety is never inferred from similarity. Signals: exact name, FTS5
  (prefix terms, a crude Russian ending trim), body-region words → patterns, and semantic
  similarity, fused with Reciprocal Rank Fusion (k = 60). "Avoid this area" is a hard filter
  on `loads_areas`, never a text match.
- **Runtime query embedding is off by default** (`FITME_EMBEDDINGS_QUERY`). Without it there
  is no provider call at request time. Substitutes still use the shipped
  exercise-to-exercise similarity, which needs no key. Any embedding failure falls back to
  the other signals.
- **Substitutes** rank by catalog structure first: same pattern, then comparable kind, then
  similarity of name, loaded areas and embedding. Results are marked `close` or general. No
  load is carried over; a swapped-in exercise gets the load engine's value, as before.
- **Plan generation** (`plan_generate.v4`) gets a balanced candidate pool instead of every
  allowed id once the allowed set exceeds 40. The pool has per-pattern quotas, puts compound
  moves first, varies equipment, ranks request matches first, and always includes every
  allowed exercise with history. Each pooled id comes with `exercise_facts` (name, kind,
  pattern, equipment, load unit). The guards still judge against the full allowed set, and a
  retrieval error falls back to sending every allowed id.
- **Assistant** (`assistant.v6`): `find_exercises` uses the same hybrid search, and a new
  read-only `find_substitutes(exercise_id, avoid_areas)` tool ranks replacements; the swap
  itself still goes through `edit_plan`/`edit_today` and the guards.

## Consequences

- Editing the catalog requires re-running `make catalog-index` (a few cents at most). Until
  then the changed exercises lose only their semantic signal. `fitme catalog check` reports
  the index status, and `tests/integration/db/test_catalog_index.py` fails until the shipped
  file matches the catalog.
- The search index is not part of `content_version`: it decides what the model is offered,
  never what the guards allow. The prompt version and `llm_input` (which includes the pool)
  are recorded on every decision as before.
- New dependencies: `numpy` (vector math) and `httpx` (made explicit; already present).
