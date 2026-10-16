# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read `AGENTS.md` first.** It is the governing rule set (single-user posture, safety
invariants, language rules, data architecture, decision logging) and overrides anything here.
`docs/ARCHITECTURE.md` is the detailed design (cited in code and tests as `A§N`);
`docs/adr/` records decisions that changed it (ADR 0003/0004: the free-text assistant and
conversation sessions).

## Commands

Requires `uv` and Python 3.13. The app reads real env vars only; the Makefile passes `.env`
via `uv run --env-file` for app commands, but never for tests.

```sh
make check                    # lint + typecheck + catalog check + tests — run before committing
make test                     # uv run pytest
uv run pytest tests/guards/test_ceiling.py::test_name   # single test
make lint / make fmt          # ruff check / ruff format + --fix
make typecheck                # mypy src (strict on fitme.domain.* and fitme.guards.*)
make catalog                  # fitme catalog check: validate catalog + locales, print content_version
make catalog-index            # re-embed changed exercises into catalog/search_index.sqlite (needs OPENAI_API_KEY)
make migrate / make serve     # apply migrations / run bot + web + retention in one process
```

`make llm-eval` calls a real model and spends money — never run it unprompted. Tests always
fake the LLM (`pydantic_ai.models.test` / `FunctionModel`) and use a real migrated temp
SQLite file; SQL is never mocked.

The repo root holds the operator's personal data (`fitme.db`, `training.toml`, `*.import.toml`,
`.env`). Stage files by explicit path, never `git add -A` / `git add .`.

## Workflow

Every change goes through a pull request so an external reviewer can review it: a branch off
an up-to-date `main`, `make check` green, then `gh pr create`. Never push to `main` directly;
review and merging are the owner's. The PR description says what changed and why, what was
tested, and anything the operator must do after merging (e.g. `make migrate`,
`make catalog-index`).

## Architecture

One process (`fitme serve`): aiogram long-polling bot + FastAPI/uvicorn website + a daily
retention task, sharing one event loop and one SQLite connection (WAL).

Dependency direction (enforced by tests where noted):

```
bot, web, cli ──► services ──► guards, llm, db ──► domain
                               guards ──► domain + stdlib only   (tests/guards/test_guards_import_boundary.py)
```

- `domain/` — pure pydantic models/enums, no I/O. `PlanProposal = Plan | Refusal`: refusal is
  a first-class output.
- `guards/` — the AGENTS.md §2 invariants as pure functions returning `GuardVerdict`s (never
  raising for business outcomes). Fail closed on NaN/inf/non-positive loads. LLM safety
  signals may add halts/refusals but never remove a guard's. Every rule needs a test in
  `tests/guards/` proving it blocks the bad case.
- `services/` — use cases; the only layer bot/web/cli call. Both front-ends share the same
  services, so a plan from the web and one from the bot pass the same guards. `services/loads.py`
  is the deterministic load engine (double progression, calibration when no history); LLM-proposed
  loads are judged by the same guards and replaced with the engine value on violation.
- `db/` — the **only** place SQL exists (`tests/unit/test_sql_containment.py` enforces it).
  `controllers/` = INSERT/UPDATE/DELETE, `selectors/` = SELECT returning domain models or frozen
  dataclasses (never rows). First arg is always the connection; services own transactions via
  `async with db.transaction()` / `db.read()`. That lock is **not reentrant**: never nest units
  of work, and never await an LLM/Telegram/network call inside one (read → close → call LLM →
  open transaction → write). `plan_versions`, `decisions`, `decision_outcomes` are append-only.
- `db/migrations/NNNN_name.sql` — forward-only, checksummed, no `BEGIN`/`COMMIT` in the file.
  **Never edit an applied migration**; add a new one (use SQLite's 12-step table rebuild for
  column/CHECK changes). `fitme serve` refuses to start with pending migrations.
- `llm/` — one pydantic-ai agent per purpose with typed output; model tier resolution in
  `llm/models.py` (`FITME_LLM_AGENT_<NAME>` overrides, else the agent's default tier via
  `FITME_LLM_TIER_*`); one escalation to the large tier, then `Refusal`. `llm/context.py`
  pseudonymizes everything sent to the provider (no Telegram ids, names, or screening notes).
- `prompts/<name>.vN.md` — versioned prompt files. Change behaviour by adding a new version,
  and record the version in decisions.
- Reference data lives in version-controlled files, not SQLite: `catalog/exercises/<range>/<id>.toml` (one file per exercise; the
  only exercises that may be prescribed), `guards/stop_words/*.txt`, prompts, `i18n/locales/*.toml`.
  `config/content.py` hashes guard-relevant files (catalog, stop words, prompts, guard and
  catalog-logic sources) into `content_version`, written to every `decisions` row.
- `bot/` — no hard-coded user strings (everything via `i18n`, en + ru). Every free-text
  message is stored and passes the stop-word scan **before** anything else, including the LLM.
  Unclaimed free text goes to the tool-using assistant, which works inside a single active
  conversation session (planning or training) and stages actions that apply only after guards.
- Catalog search (ADR 0007): `services/exercise_search.py` ranks an allowed set (exact,
  FTS5, pattern words, shipped embeddings); it never adds an exercise. After a catalog edit
  run `make catalog-index` and commit `catalog/search_index.sqlite`, or
  `test_catalog_index.py` fails.
- `web/` — server-rendered Jinja2 + vanilla JS, no build step, no CDNs.

User-visible copy (locales, catalog names/instructions, model-authored plan text) is checked
against AGENTS.md §3 forbidden terms by `tests/unit/test_forbidden_terms.py` and
`i18n/wording.py`.
