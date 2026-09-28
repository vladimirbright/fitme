-- 0005_autoincrement_ids.sql
--
-- M9 review fix (B2, A§9.4): `workout_sessions.id` and `checkins.id` were plain
-- `INTEGER PRIMARY KEY`, which SQLite is free to reuse (the classic rowid rule: a new row
-- without an explicit id gets `MAX(rowid) + 1` *of the rows currently in the table*). Once
-- A§9.4's hard delete removes the newest session (or its "fine" check-ins), the next insert
-- can reuse that exact id. But decisions reference sessions and check-ins by id inside JSON
-- (`user_report.session_id`/`checkin_id` — the recap, the applying `start`/`session_adjust`
-- decision, `result_parse` lookups, `checkin_pain` halts) and Telegram callback data encodes
-- the same ids in buttons already sent to the user. A reused id would silently inherit a
-- deleted session's or check-in's history: a brand new session could produce another
-- session's stale recap text, or a stale button tap could answer the wrong check-in.
--
-- `AUTOINCREMENT` closes this: SQLite then guarantees a monotonically increasing id, backed
-- by `sqlite_sequence`, that is never reused even after every row is deleted (SQLite docs,
-- "rowid and AUTOINCREMENT"). Same rebuild procedure as 0003 (identical columns, CHECKs, FKs;
-- indexes recreated; `db/migrate.py` handles `PRAGMA foreign_keys = OFF`/the transaction/the
-- `foreign_key_check`), applied to `workout_sessions` then `checkins`.
--
-- No explicit `sqlite_sequence` seeding: SQLite records MAX(id) itself when the rows are
-- copied into the new AUTOINCREMENT table (an extra explicit INSERT would duplicate the row).
-- `sqlite_sequence` does not exist in this schema before this migration (no table here has
-- used `AUTOINCREMENT` until now, 0003's decisions/plan_versions rebuild didn't need it: they
-- are append-only and never delete rows). It comes into existence the moment the first
-- `CREATE TABLE ... AUTOINCREMENT` statement below runs, so the seeding INSERT for
-- `workout_sessions` only works because it comes after that table's own creation — the same
-- ordering then repeats for `checkins`. Each seed row is `MAX(id)` of the copied data (0 if
-- the table is empty), so the very next auto-assigned id continues after every id that ever
-- existed, not just what's left after copying.

-- ============================================================================
-- workout_sessions: id -> AUTOINCREMENT
-- ============================================================================

CREATE TABLE new_workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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

INSERT INTO new_workout_sessions (
    id, user_id, plan_version_id, workout_key, status, current_block, started_at,
    finished_at, halt_reason
)
SELECT
    id, user_id, plan_version_id, workout_key, status, current_block, started_at,
    finished_at, halt_reason
FROM workout_sessions;

DROP TABLE workout_sessions;

ALTER TABLE new_workout_sessions RENAME TO workout_sessions;

CREATE INDEX idx_workout_sessions_user_status ON workout_sessions (user_id, status);


-- ============================================================================
-- checkins: id -> AUTOINCREMENT
-- ============================================================================

CREATE TABLE new_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES workout_sessions (id) ON DELETE SET NULL,
    question_key TEXT NOT NULL,
    answer TEXT NOT NULL CHECK (answer IN ('fine', 'worse', 'pain', 'unknown')),
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    CHECK ((answer = 'unknown') = (answered_at IS NULL))
) STRICT;

INSERT INTO new_checkins (
    id, user_id, session_id, question_key, answer, asked_at, answered_at
)
SELECT
    id, user_id, session_id, question_key, answer, asked_at, answered_at
FROM checkins;

DROP TABLE checkins;

ALTER TABLE new_checkins RENAME TO checkins;

CREATE INDEX idx_checkins_user_question ON checkins (user_id, question_key);

