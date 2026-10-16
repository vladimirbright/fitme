-- 0012_exclusion_scope.sql
--
-- ADR 0010: an exclusion has a scope. "No squats" said while working on one plan is about
-- that plan, not every plan the user will ever have:
--
--   all       — every plan (what 0011 stored; /profile lists these), `plan_id` NULL;
--   plan      — one plan (`plan_id`), applied whenever that plan is drafted, revised or
--               edited; deleted with the plan;
--   new_plan  — the new plan being drafted right now, `plan_id` NULL. Confirming a new plan
--               turns these rows into `plan` rows of it; closing that planning session
--               without saving deletes them.
--
-- UNIQUE now includes the scope and plan. A plain UNIQUE treats NULL `plan_id`s as distinct,
-- so it is an expression index over `coalesce(plan_id, 0)` (ids start at 1).
--
-- Rebuilt with the 12-step procedure like 0007 (`db/migrate.py` turns foreign keys off around
-- the file and runs `foreign_key_check` before COMMIT). Existing rows keep their ids and
-- become `all`, which is what they meant when they were stored; nothing references this
-- table, and its sequence row is carried over like 0007's.

CREATE TABLE new_exercise_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('pattern', 'exercise', 'name')),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 60),
    scope TEXT NOT NULL DEFAULT 'all' CHECK (scope IN ('all', 'plan', 'new_plan')),
    plan_id INTEGER REFERENCES plans (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    CHECK ((scope = 'plan') = (plan_id IS NOT NULL))
);

INSERT INTO new_exercise_exclusions (id, user_id, kind, value, scope, plan_id, created_at)
SELECT id, user_id, kind, value, 'all', NULL, created_at FROM exercise_exclusions;

UPDATE sqlite_sequence
SET seq = (SELECT seq FROM sqlite_sequence WHERE name = 'exercise_exclusions')
WHERE name = 'new_exercise_exclusions'
    AND EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'exercise_exclusions');
INSERT INTO sqlite_sequence (name, seq)
SELECT 'new_exercise_exclusions', seq FROM sqlite_sequence
WHERE name = 'exercise_exclusions'
    AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'new_exercise_exclusions');

DROP TABLE exercise_exclusions;
ALTER TABLE new_exercise_exclusions RENAME TO exercise_exclusions;

CREATE UNIQUE INDEX idx_exercise_exclusions_unique
    ON exercise_exclusions (user_id, kind, value, scope, coalesce(plan_id, 0));
CREATE INDEX idx_exercise_exclusions_plan ON exercise_exclusions (plan_id);
