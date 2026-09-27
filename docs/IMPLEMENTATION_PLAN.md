# Fitme — Implementation Plan

This plan is for an AI executor. Work through the milestones **in order**. Do not start a
milestone until the acceptance criteria of the previous one pass (`make check`: ruff, mypy, catalog check and pytest.
Before M3, when the catalog doesn't exist yet, run `make lint typecheck test`).

Every milestone ends with its own commit. The design lives in `docs/ARCHITECTURE.md`
(referenced below as `A§n`). `AGENTS.md` overrides both documents.

Standing rules for every milestone:

- Put no user-visible string in code. Use the i18n keys in `en.toml` and `ru.toml`.
- Put no prompt text in code. Use files in `src/fitme/prompts/`.
- No test may call a real LLM or the real Telegram API.
- If a task seems to need anything from AGENTS.md §1 or §8, stop and ask.

---

## M0 — Project scaffold

- Create `pyproject.toml` (uv, Python 3.13, src layout) with the dependencies from A§1, the
  console script `fitme`, and ruff, mypy and pytest config.
- Create `.env.example` with every variable from A§3.1 and placeholder values.
- Implement `config/settings.py` (pydantic-settings, `FITME_` prefix). It must fail fast
  with a clear error when a required variable is missing.
- Add a `fitme` CLI stub with `--help`. Set up JSON logging.
- Keep the root `Makefile` working. Every target wraps a `uv` or `fitme` command, and when you
  add a CLI command, add a target for it too.
- Add a `.gitignore` for `.env`, `*.db`, `.venv` and `__pycache__`.

**Accept:** `uv sync && uv run fitme --help` works. `uv run pytest` passes; it contains a
settings test.

## M1 — Database & account lifecycle

- `db/connection.py`: aiosqlite connection with WAL, `foreign_keys=ON` and `busy_timeout`,
  plus the `transaction()` and `read()` context managers (A§4.6).
- `db/migrate.py` and `db/migrations/0001_init.sql` covering every table in A§4.1–4.3
  (A§4.7).
- `db/controllers/` and `db/selectors/` modules for each aggregate. `plan_versions`,
  `decisions` and `decision_outcomes` get insert functions only.
- Enforce "at most one user" in `controllers/users.py` and with a DB constraint (for example
  a `CHECK (id = 1)` on `users`).
- `services/account.py`: `export_user` and `delete_user` (A§8.3). Wire up the CLI commands
  `db upgrade`, `export`, `delete` and `purge` (A§8.4).

**Accept:**

- Migrations run on an empty file and are no-ops on a second run. A changed checksum
  refuses to run.
- The SQL-containment test (A§4.6 rule 1) passes.
- Every controller and selector function has a test against a temporary DB.
- The export/delete integration test seeds every table and asserts that zero rows remain.
- A second user insert is rejected.
- The retention test purges old `chat_messages` and keeps `set_logs`.

## M2 — Domain & guards

- `domain/`: all models in A§4.5, the enums (buckets, flags, refusal codes, statuses) and
  the catalog model.
- `guards/`: every function in A§7. Add `stop_words/en.txt` and `stop_words/ru.txt`.
- `services/loads.py`: the load engine (A§7.3).

**Accept:**

- Every test listed in A§7.4 exists and passes.
- `mypy --strict` passes on `domain/` and `guards/`.
- `guards/` imports nothing outside `domain/` and the stdlib (checked by a test).

## M3 — Catalog & i18n

- `catalog/exercises.toml` with roughly 100 exercises covering every location and equipment
  option (A§4.4). Use conservative calibration starts.
- `i18n/`: a loader and `t(key, lang, **params)`, plus `locales/en.toml` and `ru.toml`.
- `fitme catalog check` (A§11).
- `config/content.py`: `content_version` hash (A§4.8).

**Accept:**

- `fitme catalog check` passes.
- A test asserts that every location has at least one workable full-body set of exercises.
- A test asserts that every locale has the same keys.
- A test asserts that `content_version` changes when a catalog or stop-word file changes
  and does not change when a locale changes.

## M4 — LLM layer

- `llm/prompts.py`: loads `prompts/<name>.v<N>.md` and returns template name, version and
  text.
- Write `prompts/*.v1.md` for the 5 agents in A§8.1, following the language rules in A§12.
- `llm/context.py`: the pseudonymized context builder and `scrub()` (A§8.2).
- `llm/models.py`: tier defaults, the agent-to-tier mapping, per-agent settings and
  `model_for(agent)` (A§8.5).
- `llm/agents.py`: agent factories that take a model, so tests can inject `TestModel` or
  `FunctionModel`.
- `fitme llm eval` with a few fixture profiles in `tests/fixtures/llm_eval/`. It is never
  run in CI.
- `llm/usage.py`: `llm_calls` accounting and a price-table loader.
- `services/decisions.py`: helpers that write `decisions` and `decision_outcomes`.

**Accept:**

- Model resolution tests:
  - the tier default applies when nothing is overridden;
  - a per-agent tier override works;
  - a per-agent full model string works;
  - an unknown tier fails at startup.
- The pseudonymization test passes (no Telegram ids, names or notes in rendered prompts).
- A fake-model run records a `llm_calls` row and a `decisions` row with the prompt version
  and model id.

## M5 — Bot skeleton, activation, setup

- aiogram dispatcher and `OwnerGateMiddleware` (A§6.1).
- `fitme activate [--rebind]`, then `/activate`, `/start`, `/help`, `/cancel` and
  `/profile`.
- The setup questionnaire (A§5) with inline keyboards and step-by-step saves.
- The language default comes from `language_code`.
- `fitme serve` runs the bot (the web comes in M9).

**Accept:**

- Integration tests use aiogram with a mocked Bot session:
  - an unknown user gets the private-instance reply and no row is written;
  - activation binds the account;
  - a second `/activate` is refused;
  - a full setup run stores the enum values;
  - a red flag without clearance is stored so that planning refuses.

## M6 — `/plan`

- `services/planning.py`: gates → context → agent → guards (with one retry) → proposal
  (A§6.4). The web uses the same service.
- Bot handlers: plan list, show, revise loop, confirm, set default, archive.

**Accept:**

- With a fake model:
  - a valid plan gets stored as version 1;
  - a plan with a non-catalog exercise is rejected and retried, then refused;
  - an open hold refuses without an LLM call;
  - every round writes a decision containing `guards_fired`;
  - a revise that fails guards twice escalates once to the `large` tier, then refuses, with
    each model recorded in `llm_calls`.

## M7 — `/train`

- `services/training.py`: the state machine from A§6.5, persisted in `workout_sessions`.
- Precheck, review/adjust loop, block-by-block delivery with explicit loads, the four
  buttons, and free-text results through the stop-word guard and `result_parse` with
  confirmation.
- The halt path (A§6.6), holds, and the clearing flow.
- Resume after a restart.

**Accept:**

- Integration tests cover:
  - the happy path with "According to plan" logs `set_logs`;
  - free text containing a stop word halts **without** an LLM call;
  - `safety_signal=true` halts;
  - the pain button halts;
  - after a halt, `/train` and `/plan` refuse;
  - a hold cannot be cleared on the same day;
  - a restart mid-workout resumes at the same block.

## M8 — Recap & progression

- End-of-workout recap, check-ins for flagged areas, load engine output, `recap` agent text,
  and **Apply** of suggestions through the guards to a new plan version.
- `decision_outcomes` rows record planned vs actual.

**Accept:**

- Tests cover:
  - all reps hit → +increment in the next session;
  - an unanswered check-in → no increase;
  - an increase already applied within the week → the second increase is blocked;
  - an LLM-suggested load above the ceiling is rejected and logged.

## M8b — Paste an existing plan

The operator asked for this during M8. It adds a second entry point to `/plan` next to
"New plan": **"Paste my plan"**.

- The user pastes the program text. It is stop-word scanned first, like all owner text.
- A new `plan_import` agent (`prompts/plan_import.v1.md`) **transcribes** the text into the
  `Plan` schema. It does not design anything:
  - days, exercises, sets, reps and loads are kept as written;
  - exercises map to allowed catalog ids; unmatched exercises are listed back to the user
    and never invented;
  - per-set loads use the lowest value, with the rest noted; load ranges use the lower
    bound; durations go into notes.
- The usual guards run: catalog, contraindications, equipment and location, schedule.
  Loads follow A§7:
  - an exercise with no history becomes `calibration`, and the user's declared kg is kept
    as a display hint ("calibration: your plan says 80 kg, start at or below it and log
    what you used");
  - an exercise with history is judged normally (engine substitution).
  - The declared loads are stored in the draft proposal, so `/train` shows the same hint.
- Confirm uses the existing revalidating confirm, with a new `plan_versions.origin =
  'import'` and a new decision kind `plan_import` (pre-release 0001 CHECK edit).
- EN and RU copy; stale-safe buttons; one decision per LLM attempt, with `llm_input`
  verbatim.

**Accept:**

- The operator's sample program (fixture) imports with all its days and the right
  catalog ids.
- An unmatched exercise is reported, not invented.
- A no-history kg becomes calibration with the declared hint shown in `/plan` and
  `/train`.
- An exercise with history uses the engine value when the declared load breaks the
  cap or ceiling.
- A stop word in the pasted text halts with no LLM call.
- The confirmed version has `origin = 'import'`.

## M9 — `/stats`, `/system`, `/export`, `/delete`, website

- Bot: `/stats` (A§6.7), `/system`, `/export` and `/delete` (A§6.2).
- Web: FastAPI app mounted in `fitme serve`. Auth (OTP, sessions, CSRF, headers; A§9.2) and
  every page in A§9.1. Charts use vanilla JS and SVG, with a table fallback.

**Accept:**

- Web tests (httpx `AsyncClient`) cover:
  - the OTP flow, including expiry, attempt limit and rate limit;
  - the cookie flags;
  - CSRF rejection;
  - `/app/*` redirects without a session;
  - plan edit over the cap needs confirmation and is logged as `user_edit`;
  - export download;
  - delete wipes everything;
  - plan detail lists only this plan's sessions from the last 14 days, across plan
    versions;
  - training delete (single and bulk) removes sessions and set_logs;
  - training delete rejects the whole batch if one id is foreign or unknown;
  - training delete keeps an open health hold, and keeps non-`fine` check-ins detached;
  - after deleting a session that contained this week's increase, the weekly cap still
    blocks a second increase;
  - training delete writes a `session_delete` decision.
- A test asserts that templates contain no external URLs.

## M10 — Deployment (Docker Compose) & docs

The reference deployment is **Docker Compose** on a small host, with the smallest
practical Linux image.

- A multi-stage `Dockerfile`:
  - **Builder:** `python:3.13-alpine` with `uv` (copied from the official
    `ghcr.io/astral-sh/uv` image). It runs `uv sync --frozen --no-dev`, installing into
    `/app/.venv`.
  - **Runtime:** `python:3.13-alpine` with only the venv and the app, and no build tools.
    It runs as a non-root user (uid 10001) with `PYTHONDONTWRITEBYTECODE=1`,
    `PYTHONUNBUFFERED=1` and `PYDANTIC_AI_NO_BANNER=1`.
  - The entrypoint runs `fitme db upgrade`, then `fitme serve`. Migrations are idempotent,
    and serve refuses to start while any are pending.
  - Keep it small: no compilers, no pip cache. Check that every dependency installs from
    musllinux wheels; if one doesn't, document it and switch that stage to
    `python:3.13-slim`.
  - A `.dockerignore` excludes `.venv`, `.git`, the DB, exports, backups, `.env*`, tests,
    docs and caches.
- `compose.yaml`:
  - **`fitme` service:** `env_file: .env`; a named volume `fitme-data` mounted at `/data`
    with `FITME_DB_PATH=/data/fitme.db`; `restart: unless-stopped`; `read_only: true`
    with a `tmpfs` for `/tmp`; `cap_drop: [ALL]`; `security_opt: [no-new-privileges:true]`.
  - **Healthcheck:** `fitme health`, a new CLI command that checks the DB opens,
    migrations are applied and settings are valid. It uses no network.
  - The web port binds to `127.0.0.1` only by default.
  - **Optional `caddy` service** under a compose profile `tls` (`caddy:2-alpine`), with a
    `Caddyfile` that reverse-proxies to `fitme:8080` using automatic HTTPS for
    `FITME_WEB_BASE_URL`'s host.
- **Backups:** `fitme backup --out PATH` uses Python's `sqlite3` online backup API, so the
  image needs no `sqlite3` binary. It writes mode 0600 and rotates to keep the last N
  (`--keep`). Provide `deploy/backup.sh`, which calls
  `docker compose exec fitme fitme backup` for host cron, plus a restore note.
- **Make targets:** `docker-build`, `up`, `down`, `logs`, `backup`, and
  `activate-docker` (`docker compose exec fitme fitme activate`).
- **README:** what it is; the AI disclosure; the not-medical-advice note; which data goes
  to the LLM provider; setup (`.env` from `.env.example`, `docker compose up -d`,
  `docker compose exec fitme fitme activate`, message the bot); backups and restore;
  local dev with `uv`.

**Accept:**

- `docker compose build` succeeds, and the runtime image is under ~120 MB (report the
  actual size).
- `docker compose up` starts with a valid `.env`, and the healthcheck passes.
- The container runs as non-root on a read-only root filesystem.
- The DB persists across `down`/`up`.
- `fitme backup` produces a restorable copy (tested in pytest without Docker).
- A fresh clone reaches a working bot by following only the README.

## M11 — Import plans and training history (last)

Lets the operator start from their real training history instead of calibration.

- `fitme history import PATH` (plus `--dry-run`). It reads a local file of past trainings
  and plans. The format is documented in `docs/import-format.md`: TOML or JSON, with
  weights as in the catalog (barbell = total including the bar, dumbbells = per dumbbell,
  machines = the stack number). The file is personal health data: it stays outside the
  repo, and `*.import.toml`/`*.import.json` are gitignored.
- Exercises are matched to catalog ids through an explicit alias table in the import file.
  Unknown exercises are reported and skipped. Nothing is guessed.
- Imported trainings become `workout_sessions` (status `completed`) and `set_logs` with
  `source = 'import'`. Imported plans become `plans` + `plan_versions` with
  `origin = 'import'`. Each import writes one `decision(kind = history_import)` that
  summarises what was imported. This needs a new migration; do not edit an applied one.
- Imported loads are real history. They set the historical max and the current working
  load used by the guards. The weekly cap and the check-in rules apply from the first
  generated session onward. The import itself is not a load increase and writes no
  `load_changes`.
- The import is idempotent: re-importing the same file doesn't duplicate rows (use a
  content hash per session). Export includes the imported rows, and delete removes them.

**Accept:**

- A sample fixture imports cleanly.
- A dry run writes nothing.
- A re-import is a no-op.
- An unknown exercise is reported.
- After import, `next_load` for a squat with an imported 75 kg history proposes from 75 kg
  (hold or +increment per A§7.3), not calibration.
- The ceiling uses the imported max.
- The import file pattern is gitignored, and no import fixture contains real personal data.
