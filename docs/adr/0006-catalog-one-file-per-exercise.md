# ADR 0006 — One file per catalog exercise

Status: accepted (2026-10-15). Changes A§4.4 (where the catalog lives) and A§4.8 (what
`content_version` hashes).

## Context

The catalog grew from 114 to 190 exercises in one review of the local exercise reference
library, and further growth is planned. A single `exercises.toml` of 2,000+ lines made each
addition a hard-to-review diff and was awkward for an LLM to edit one entry at a time.

## Decision

- The catalog is `src/fitme/catalog/exercises/<range>/<id>.toml`: one exercise per file, its
  top-level keys being one `Exercise`. Range folders go by the id's first letter: `a-c`,
  `d-f`, `g-i`, `j-l`, `m-o`, `p-r`, `s-u`, `v-x`, `y-z`.
- `fitme.catalog.load_catalog()` reads every file in sorted path order and fails closed on a
  file whose name isn't its id, a file in the wrong range folder, or any other entry (only
  `exercises/README.md`, which holds the field notes, is allowed).
- `content_version` hashes every `catalog/exercises/**/*.toml` file (path, length, bytes),
  so adding, removing or moving an exercise changes the version. The README is not hashed.
- The validation model and `fitme catalog check` are unchanged.

## Consequences

- The catalog's order is now sorted by id, not by authoring section.
- Section comments that described groups of entries are gone; a per-entry comment moved into
  that entry's file.
- `content_version` changed once with this layout change, with no change in content.
