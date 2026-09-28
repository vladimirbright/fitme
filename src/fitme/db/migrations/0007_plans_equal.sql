-- 0007_plans_equal.sql
--
-- "All plans are equal" (operator decision, 2026-09-28; A§4.3): there is no archived state
-- any more, and imported training history belongs to no plan. Three changes:
--
--   workout_sessions.plan_version_id  becomes nullable  (an imported session has none)
--   the M11 "Imported history" holder plan(s)  are deleted  (they only existed because the
--                                              column was NOT NULL)
--   plans.status = 'archived'          becomes 'active'   (every plan can be viewed, edited,
--                                              trained from and made the default)
--
-- `workout_sessions` is rebuilt with the documented 12-step procedure, the same way 0003,
-- 0005 and 0006 did it (https://www.sqlite.org/lang_altertable.html#otheralter): create the
-- new table with the identical columns (0005's AUTOINCREMENT id, 0006's `import_hash`),
-- CHECKs and foreign keys — `plan_version_id` now without NOT NULL, its FK still
-- `ON DELETE RESTRICT` — copy every row across with an explicit column list and the ids
-- verbatim, drop the old table, rename the new one, recreate both indexes exactly as 0005
-- and 0006 define them (the partial UNIQUE `import_hash` index included). `db/migrate.py`
-- supplies the rest: `PRAGMA foreign_keys = OFF` before, one transaction around the whole
-- file, `PRAGMA foreign_key_check` before COMMIT, foreign keys back on afterwards.
-- `set_logs`, `checkins`, `health_holds` and `chat_messages` reference `workout_sessions` by
-- table *name*, which the rename restores; the ids are copied verbatim, so nothing is
-- orphaned.
--
-- `sqlite_sequence` continuity: copying rows with explicit ids makes SQLite record MAX(id)
-- for the new table, but the old table's sequence may be *higher* than its current MAX(id)
-- (A§9.4's hard delete can remove the newest sessions, and AUTOINCREMENT's whole point is
-- that a deleted id is never handed out again). So the old table's sequence row is copied
-- onto the new table before the old one is dropped: `DROP TABLE` removes the old row, and
-- `ALTER TABLE ... RENAME` renames the new one. An old table that never had a row has no
-- sequence row, and then neither does the new one, which is the same state as before.
--
-- Holder plans are identified structurally, never by their display name: a plan every
-- version of which has `origin = 'import'` AND one of whose versions is named as
-- `holder_plan_version_id` in a `history_import` decision outcome (that is exactly how
-- `services.history_import` found it again). Their imported sessions are detached first
-- (`plan_version_id = NULL`, keyed on `import_hash`), then the versions, then the plans.
-- `plan_versions` has a BEFORE UPDATE trigger, not a BEFORE DELETE one. Should any *other*
-- session (no `import_hash`) still reference a holder version — impossible through the app,
-- the holder had one empty workout and was archived — the RESTRICT FK would be violated,
-- `foreign_key_check` would fail, and the whole migration rolls back rather than losing
-- a training. The decision outcomes that named the holder stay as they are: append-only,
-- and now merely historical.
--
-- `plans.status` keeps its CHECK ('draft', 'active', 'archived') for schema compatibility;
-- the application writes only 'active' from now on.

-- ============================================================================
-- workout_sessions: plan_version_id NOT NULL -> nullable
-- ============================================================================

CREATE TABLE new_workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    plan_version_id INTEGER REFERENCES plan_versions (id) ON DELETE RESTRICT,
    workout_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'confirmed', 'in_progress', 'completed', 'aborted', 'halted'
    )),
    current_block INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    halt_reason TEXT,
    import_hash TEXT
) STRICT;

INSERT INTO new_workout_sessions (
    id, user_id, plan_version_id, workout_key, status, current_block, started_at,
    finished_at, halt_reason, import_hash
)
SELECT
    id, user_id, plan_version_id, workout_key, status, current_block, started_at,
    finished_at, halt_reason, import_hash
FROM workout_sessions;

DELETE FROM sqlite_sequence WHERE name = 'new_workout_sessions';

INSERT INTO sqlite_sequence (name, seq)
SELECT 'new_workout_sessions', seq FROM sqlite_sequence WHERE name = 'workout_sessions';

DROP TABLE workout_sessions;

ALTER TABLE new_workout_sessions RENAME TO workout_sessions;

CREATE INDEX idx_workout_sessions_user_status ON workout_sessions (user_id, status);

CREATE UNIQUE INDEX idx_workout_sessions_import_hash
ON workout_sessions (user_id, import_hash) WHERE import_hash IS NOT NULL;

-- ============================================================================
-- Imported sessions belong to no plan; the holder plans go away
-- ============================================================================

UPDATE workout_sessions SET plan_version_id = NULL WHERE import_hash IS NOT NULL;

CREATE TEMP TABLE holder_plans AS
SELECT p.id AS plan_id
FROM plans p
WHERE EXISTS (
    SELECT 1
    FROM plan_versions v
    JOIN decisions d ON d.id = v.decision_id
    JOIN decision_outcomes o ON o.decision_id = d.id
    WHERE v.plan_id = p.id
      AND d.kind = 'history_import'
      AND json_extract(o.outcome, '$.holder_plan_version_id') = v.id
)
AND NOT EXISTS (
    SELECT 1 FROM plan_versions v WHERE v.plan_id = p.id AND v.origin != 'import'
);

DELETE FROM plan_versions WHERE plan_id IN (SELECT plan_id FROM holder_plans);

DELETE FROM plans WHERE id IN (SELECT plan_id FROM holder_plans);

DROP TABLE holder_plans;

-- ============================================================================
-- No archived state
-- ============================================================================

UPDATE plans SET status = 'active' WHERE status = 'archived';
