-- 0006_history_import.sql
--
-- M11 "Import plans and training history" (A§4.2, A§4.3, IMPLEMENTATION_PLAN M11). Two
-- schema changes:
--
--   set_logs.source            += 'import'   (a set row written by `fitme history import`)
--   workout_sessions.import_hash  new column  (idempotent re-imports, see below)
--
-- `set_logs` is rebuilt with the documented 12-step procedure, the same way 0003 and 0005
-- did it (https://www.sqlite.org/lang_altertable.html#otheralter): create the new table with
-- the identical columns, CHECKs (the skipped/actual_* invariant included) and foreign keys,
-- copy every row across with an explicit column list and the ids verbatim, drop the old
-- table, rename the new one, recreate both indexes exactly as 0001_init.sql defines them.
-- `db/migrate.py` supplies the rest: `PRAGMA foreign_keys = OFF` before, one transaction
-- around the whole file, `PRAGMA foreign_key_check` before COMMIT, foreign keys back on
-- afterwards. Nothing references `set_logs` by foreign key, and its own FK points at the
-- table *name* `workout_sessions`, so the rebuild orphans nothing.
--
-- While the table is rebuilt anyway, its id becomes `AUTOINCREMENT`, for the same reason
-- 0005 gave `workout_sessions` and `checkins` one: A§9.4's hard delete cascades `set_logs`
-- rows away, and a plain rowid could then be handed out again to a brand-new set. Decision
-- outcomes reference set rows by id inside JSON (the confirmed-results outcome's
-- `set_log_ids`, services/training.py), so a reused id would point a stored outcome at a
-- different set than the one it described. As in 0005,
-- SQLite records MAX(id) of the copied rows in `sqlite_sequence` by itself, so no explicit
-- seeding INSERT is needed (an extra one would duplicate the row).
--
-- `workout_sessions.import_hash` is added in place with `ALTER TABLE ... ADD COLUMN`, which
-- STRICT tables allow for a nullable column with no default. It holds the SHA-256 (hex) of
-- one imported session's normalized content (its resolved timestamp plus every set row), so
-- importing the same file twice is a no-op: the partial UNIQUE index below rejects a second
-- row with the same hash at the schema level, and the service checks first to report the
-- duplicate instead of failing. Sessions the bot or the website create keep `NULL` here and
-- are outside the index entirely.

-- ============================================================================
-- set_logs: source CHECK += 'import'; id -> AUTOINCREMENT
-- ============================================================================

CREATE TABLE new_set_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES workout_sessions (id) ON DELETE CASCADE,
    exercise_id TEXT NOT NULL,
    set_index INTEGER NOT NULL,
    planned_load_kg REAL,
    planned_reps_min INTEGER,
    planned_reps_max INTEGER,
    actual_load_kg REAL,
    actual_reps INTEGER,
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (
        skipped IN (0, 1)
        AND (skipped = 0 OR (actual_reps IS NULL AND actual_load_kg IS NULL))
    ),
    rpe REAL,
    source TEXT NOT NULL CHECK (source IN ('button', 'free_text', 'web', 'import')),
    created_at TEXT NOT NULL
) STRICT;

INSERT INTO new_set_logs (
    id, session_id, exercise_id, set_index, planned_load_kg, planned_reps_min,
    planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, source, created_at
)
SELECT
    id, session_id, exercise_id, set_index, planned_load_kg, planned_reps_min,
    planned_reps_max, actual_load_kg, actual_reps, skipped, rpe, source, created_at
FROM set_logs;

DROP TABLE set_logs;

ALTER TABLE new_set_logs RENAME TO set_logs;

CREATE INDEX idx_set_logs_session ON set_logs (session_id);

CREATE INDEX idx_set_logs_exercise ON set_logs (exercise_id);

-- ============================================================================
-- workout_sessions: import_hash (idempotent imports)
-- ============================================================================

ALTER TABLE workout_sessions ADD COLUMN import_hash TEXT;

CREATE UNIQUE INDEX idx_workout_sessions_import_hash
ON workout_sessions (user_id, import_hash) WHERE import_hash IS NOT NULL;
