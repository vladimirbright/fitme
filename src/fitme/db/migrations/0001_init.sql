-- 0001_init.sql
--
-- Every table in docs/ARCHITECTURE.md §4.1-4.3 (identity, health, derived output). STRICT
-- tables, CHECK constraints for enums, foreign keys with ON DELETE CASCADE from users, and
-- indexes for the selector paths described in the doc (A§4.7).
--
-- Statement order here is dependency order (a table is created after everything it
-- references), which differs slightly from the doc's identity/health/derived grouping.
--
-- Enum values match A§4.2 "Canonical enum values" exactly (screening_flags.flag and
-- chat_messages.direction are canonical there too, following what this file originally
-- derived). Timestamps follow the A§4.2 "Timestamps" rule: all are `fitme.clock`-formatted
-- UTC strings (`YYYY-MM-DDTHH:MM:SS.ffffffZ`); this file doesn't enforce that with a CHECK
-- (it would mean the same GLOB pattern repeated on ~15 columns), so a dedicated test
-- (`tests/integration/db/test_timestamp_format.py`) asserts every stored timestamp matches
-- it instead.
--
-- Never edit this file after it has been applied anywhere real: write a new migration
-- instead (A§4.7). db/migrate.py refuses to run if an applied file's checksum changes.
-- Pre-release exception (A§4.7): until the first real deployment (M10), no database exists
-- outside tests, so this file may still be edited in place to extend an enum CHECK.

-- ============================================================================
-- Identity & auth (A§4.1)
-- ============================================================================

CREATE TABLE users (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    language TEXT NOT NULL,
    -- Nullable: the activation flow creates this row before the setup questionnaire (which
    -- asks for the timezone) has run.
    timezone TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE telegram_accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users (id) ON DELETE CASCADE,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    linked_at TEXT NOT NULL
) STRICT;

-- Not linked to a user row: a code is minted before any user exists to activate. Codes and
-- session ids are hashed (SHA-256) before storage; never store them in plaintext (A§4.1).
CREATE TABLE activation_codes (
    id INTEGER PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT
) STRICT;

CREATE TABLE login_codes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used_at TEXT
) STRICT;

CREATE INDEX idx_login_codes_user_id ON login_codes (user_id);

CREATE TABLE web_sessions (
    id_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_web_sessions_user_id ON web_sessions (user_id);

-- ============================================================================
-- Health & self-reports (A§4.2)
-- ============================================================================

-- Columns below are nullable: setup answers are saved step by step (A§5), so a profile row
-- can exist before it is complete. `completed_at` marks when the whole questionnaire is
-- done. A CHECK on a nullable enum column allows NULL by normal SQL three-valued logic
-- (`NULL IN (...)` and `json_valid(NULL)` both evaluate to NULL, which CHECK treats as
-- satisfied) and still rejects any non-NULL value outside the set.
CREATE TABLE profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    age_bucket TEXT CHECK (age_bucket IN (
        '18_29', '30_39', '40_49', '50_59', '60_69', '70_79', '80_90'
    )),
    weight_bucket TEXT CHECK (weight_bucket IN (
        '30_39', '40_49', '50_59', '60_69', '70_79', '80_89', '90_99',
        '100_109', '110_119', '120_129', '130_139', '140_149', '150_159',
        '160_169', '170_179', '180_189', '190_200'
    )),
    experience TEXT CHECK (experience IN ('none', 'lt_6m', '6m_2y', '2y_5y', 'gt_5y')),
    barbell_experience TEXT CHECK (barbell_experience IN ('yes', 'some', 'no')),
    -- JSON array of preference enum values: weight_training, full_body, split, bodyweight,
    -- conditioning, mobility (A§4.2 canonical values). Per-element enum validation happens
    -- at the application layer (pydantic, M2); the DB only guards a well-formed JSON array.
    preferences TEXT CHECK (json_type(preferences) = 'array'),
    location TEXT CHECK (location IN (
        'public_gym', 'studio_gym', 'home_equipment', 'apartment_no_equipment', 'outdoor'
    )),
    -- JSON array of equipment enum values: dumbbells, barbell, rack, bench, pull_up_bar,
    -- kettlebell, resistance_bands (A§4.2 canonical values).
    equipment TEXT CHECK (json_type(equipment) = 'array'),
    sessions_per_week INTEGER CHECK (sessions_per_week BETWEEN 1 AND 6),
    session_minutes INTEGER CHECK (session_minutes IN (30, 45, 60, 90)),
    focus TEXT CHECK (focus IN ('strength', 'muscle', 'general_fitness', 'conditioning')),
    completed_at TEXT,
    updated_at TEXT NOT NULL
) STRICT;

-- One row per flag (A§4.2): the app upserts by (user_id, flag).
CREATE TABLE screening_flags (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    flag TEXT NOT NULL CHECK (flag IN (
        'heart_condition', 'chest_discomfort', 'dizziness_fainting', 'high_blood_pressure',
        'recent_surgery', 'pregnant', 'other_condition_limits',
        'neck_injury_current', 'shoulder_injury_current', 'elbow_wrist_injury_current',
        'lower_back_injury_current', 'hip_injury_current', 'knee_injury_current',
        'ankle_injury_current', 'hernia', 'other_unlisted'
    )),
    value TEXT NOT NULL CHECK (value IN ('yes', 'no', 'unknown')),
    clearance TEXT CHECK (clearance IN ('yes', 'no')),
    answered_at TEXT NOT NULL,
    UNIQUE (user_id, flag)
) STRICT;

-- Free text for "other". Never sent to the LLM; shown back to the user only (A§4.2).
CREATE TABLE screening_notes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

-- ============================================================================
-- Derived output (A§4.3), created before the health-group tables that reference it
-- ============================================================================

CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_plans_user ON plans (user_id);

-- At most one default plan per user (A§4.3): a partial unique index over is_default = 1.
CREATE UNIQUE INDEX idx_plans_one_default_per_user ON plans (user_id) WHERE is_default = 1;

-- Append-only (A§4.3, A§6): controllers/decisions.py exposes insert functions only.
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'plan_generate', 'plan_revise', 'session_adjust', 'result_parse', 'progression',
        'session_halt', 'hold_clear', 'refusal', 'user_edit', 'session_delete',
        'history_import', 'plan_confirm'
    )),
    -- Nullable: not every decision involves an LLM call (e.g. a deterministic halt/refusal).
    prompt_template TEXT,
    prompt_version TEXT,
    model TEXT,
    content_version TEXT NOT NULL,
    llm_input TEXT CHECK (llm_input IS NULL OR json_valid(llm_input)),
    user_report TEXT CHECK (user_report IS NULL OR json_valid(user_report)),
    proposal TEXT CHECK (proposal IS NULL OR json_valid(proposal)),
    -- JSON array of `domain.LoadChange {exercise_id, from_kg, to_kg}` (A§4.3). Every decision
    -- that applies a load records it here, of any kind (progression, plan_revise,
    -- session_adjust, user_edit, ...): `progression.check_weekly_increment`'s `increases_7d`
    -- sums the positive deltas across every kind, read from this column (A§7, A§9.4).
    load_changes TEXT NOT NULL DEFAULT '[]' CHECK (json_type(load_changes) = 'array'),
    guards_fired TEXT NOT NULL DEFAULT '[]' CHECK (json_type(guards_fired) = 'array'),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_decisions_user_created ON decisions (user_id, created_at);

-- Append-only (A§4.3, A§4.6 rule 5): controllers/decisions.py has insert functions only.
-- This trigger makes it a DB invariant too, not just a code-review convention. It does not
-- block DELETE: the full-account delete (A§8.3) is the one sanctioned exception, and that
-- uses DELETE, never UPDATE.
CREATE TRIGGER trg_decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only');
END;

-- Append-only (A§4.3): controllers/plans.py exposes insert functions only.
CREATE TABLE plan_versions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    origin TEXT NOT NULL CHECK (origin IN ('llm', 'progression', 'user_edit')),
    -- Every version is traceable to the decision that produced it (AGENTS.md §6).
    -- ON DELETE RESTRICT, not CASCADE: a plan edit (which creates a decision, then a new
    -- plan_version) must never let some other path silently wipe an existing plan_version
    -- by deleting its decision. The full-account delete explicitly deletes plan_versions
    -- before decisions (db/controllers/account.py) to satisfy this.
    decision_id INTEGER NOT NULL REFERENCES decisions (id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, version)
) STRICT;

CREATE TRIGGER trg_plan_versions_no_update
BEFORE UPDATE ON plan_versions
BEGIN
    SELECT RAISE(ABORT, 'plan_versions is append-only');
END;

-- ============================================================================
-- Health & self-reports, continued: tables that reference plan_versions/workout_sessions
-- ============================================================================

-- plan_version_id is ON DELETE RESTRICT, not CASCADE: a plan edit must never silently wipe
-- training history. The full-account delete explicitly deletes workout_sessions before
-- plan_versions to satisfy this (db/controllers/account.py).
CREATE TABLE workout_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    plan_version_id INTEGER NOT NULL REFERENCES plan_versions (id) ON DELETE RESTRICT,
    workout_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'confirmed', 'in_progress', 'completed', 'aborted', 'halted'
    )),
    current_block INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    halt_reason TEXT
) STRICT;

CREATE INDEX idx_workout_sessions_user_status ON workout_sessions (user_id, status);

-- No user_id column: a set belongs to a session, which belongs to a user (A§4.2). Source of
-- truth for history and the historical max (A§7); cascades from workout_sessions (A§4.6).
-- One row per *prescribed* set, created when the block is sent (A§4.2): a set that isn't
-- performed is `skipped = 1` with `actual_*` NULL, so a partial or halted session is visible
-- in the data, not silently absent. `planned_reps_min`/`planned_reps_max` (not a single
-- `planned_reps`) so the load engine's full double-progression rule (A§7.3: hit the top of
-- the range, fall below the bottom, or hold) can be read back from this table alone.
CREATE TABLE set_logs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES workout_sessions (id) ON DELETE CASCADE,
    exercise_id TEXT NOT NULL,
    set_index INTEGER NOT NULL,
    planned_load_kg REAL,
    planned_reps_min INTEGER,
    planned_reps_max INTEGER,
    actual_load_kg REAL,
    actual_reps INTEGER,
    -- A skipped set has no actual result at all: the schema, not just application code,
    -- enforces that `skipped = 1` always pairs with both actual_* columns NULL, so a set
    -- can never be simultaneously "skipped" and "logged" (which the load engine's
    -- hit_reps_max/below_reps_min computation depends on, A§7.3).
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (
        skipped IN (0, 1)
        AND (skipped = 0 OR (actual_reps IS NULL AND actual_load_kg IS NULL))
    ),
    rpe REAL,
    source TEXT NOT NULL CHECK (source IN ('button', 'free_text', 'web')),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_set_logs_session ON set_logs (session_id);
CREATE INDEX idx_set_logs_exercise ON set_logs (exercise_id);

-- source_session_id is detached (ON DELETE SET NULL), not cascaded: deleting a halted
-- session must not clear the hold it created (A§9.4).
CREATE TABLE health_holds (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- `pain_button` (M7): the persistent "Pain / feeling unwell" button on every in-workout
    -- message (A§6.3, A§6.6). Added under the A§4.7 pre-release exception.
    reason TEXT NOT NULL CHECK (reason IN (
        'stop_word', 'checkin_pain', 'llm_safety_signal', 'precheck_yes', 'pain_button'
    )),
    source_session_id INTEGER REFERENCES workout_sessions (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    cleared_at TEXT
) STRICT;

-- An open hold has cleared_at IS NULL; the partial index serves that lookup directly.
CREATE INDEX idx_health_holds_user_open ON health_holds (user_id) WHERE cleared_at IS NULL;

-- session_id is detached (ON DELETE SET NULL), not cascaded: a non-'fine' check-in must
-- keep blocking increases even after its session is deleted (A§9.4). A row is created as
-- 'unknown' with no answered_at, and is only ever updated by an explicit answer (AGENTS.md
-- §2: silence is not consent) — enforced here, not just by convention: 'unknown' if and only
-- if answered_at is still NULL.
CREATE TABLE checkins (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES workout_sessions (id) ON DELETE SET NULL,
    question_key TEXT NOT NULL,
    answer TEXT NOT NULL CHECK (answer IN ('fine', 'worse', 'pain', 'unknown')),
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    CHECK ((answer = 'unknown') = (answered_at IS NULL))
) STRICT;

CREATE INDEX idx_checkins_user_question ON checkins (user_id, question_key);

-- session_id cascades (unlike health_holds/checkins): deleting a session deletes the chat
-- messages tied to it (A§9.4). Raw text; purged after FITME_CHAT_RETENTION_DAYS (A§8.4).
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES workout_sessions (id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_chat_messages_user ON chat_messages (user_id);
CREATE INDEX idx_chat_messages_created_at ON chat_messages (created_at);

-- ============================================================================
-- Derived output, continued (A§4.3)
-- ============================================================================

-- Append-only (A§4.3): controllers/decisions.py exposes insert functions only.
CREATE TABLE decision_outcomes (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions (id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (json_valid(outcome)),
    created_at TEXT NOT NULL
) STRICT;

CREATE TRIGGER trg_decision_outcomes_no_update
BEFORE UPDATE ON decision_outcomes
BEGIN
    SELECT RAISE(ABORT, 'decision_outcomes is append-only');
END;

-- No prompt content here (A§4.3); feeds /system. No user_id column: this instance is
-- single-user (ADR 0002), so every row already belongs to the one user.
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions (id) ON DELETE CASCADE,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_estimate_usd REAL,
    latency_ms INTEGER NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX idx_llm_calls_created_at ON llm_calls (created_at);
CREATE INDEX idx_llm_calls_decision ON llm_calls (decision_id);
