-- 0003_plan_import.sql
--
-- M8b "Paste my plan" (A§4.3): two enum CHECKs grow by one value each —
--   decisions.kind        += 'plan_import'  (one plan_import agent attempt over a pasted program)
--   plan_versions.origin  += 'import'       (a confirmed pasted plan)
--
-- SQLite cannot alter a CHECK constraint in place, so both tables are rebuilt with the
-- documented 12-step procedure (https://www.sqlite.org/lang_altertable.html#otheralter),
-- one table at a time: create the new table with the identical schema except the CHECK,
-- copy every row across (explicit column lists, ids included), drop the old table, rename
-- the new one, then recreate every index and trigger exactly as 0001_init.sql defines them
-- (the append-only BEFORE UPDATE triggers included). The runner (db/migrate.py) supplies the
-- rest of the procedure: `PRAGMA foreign_keys = OFF` before, one transaction around the
-- whole file, `PRAGMA foreign_key_check` before COMMIT, foreign keys back on afterwards.
-- Foreign keys from other tables keep pointing at the table *names* (plan_versions ->
-- decisions, decision_outcomes -> decisions, llm_calls -> decisions, workout_sessions ->
-- plan_versions), which the rename restores; with foreign keys off, dropping the old tables
-- cascades nothing. Row ids are copied verbatim, so no child row is orphaned.

-- ============================================================================
-- decisions: kind CHECK += 'plan_import'
-- ============================================================================

CREATE TABLE new_decisions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'plan_generate', 'plan_revise', 'session_adjust', 'result_parse', 'progression',
        'session_halt', 'hold_clear', 'refusal', 'user_edit', 'session_delete',
        'history_import', 'plan_confirm', 'plan_import'
    )),
    prompt_template TEXT,
    prompt_version TEXT,
    model TEXT,
    content_version TEXT NOT NULL,
    llm_input TEXT CHECK (llm_input IS NULL OR json_valid(llm_input)),
    user_report TEXT CHECK (user_report IS NULL OR json_valid(user_report)),
    proposal TEXT CHECK (proposal IS NULL OR json_valid(proposal)),
    load_changes TEXT NOT NULL DEFAULT '[]' CHECK (json_type(load_changes) = 'array'),
    guards_fired TEXT NOT NULL DEFAULT '[]' CHECK (json_type(guards_fired) = 'array'),
    created_at TEXT NOT NULL
) STRICT;

INSERT INTO new_decisions (
    id, user_id, kind, prompt_template, prompt_version, model, content_version, llm_input,
    user_report, proposal, load_changes, guards_fired, created_at
)
SELECT
    id, user_id, kind, prompt_template, prompt_version, model, content_version, llm_input,
    user_report, proposal, load_changes, guards_fired, created_at
FROM decisions;

DROP TABLE decisions;

ALTER TABLE new_decisions RENAME TO decisions;

CREATE INDEX idx_decisions_user_created ON decisions (user_id, created_at);

CREATE TRIGGER trg_decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions is append-only');
END;

-- ============================================================================
-- plan_versions: origin CHECK += 'import'
-- ============================================================================

CREATE TABLE new_plan_versions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    origin TEXT NOT NULL CHECK (origin IN ('llm', 'progression', 'user_edit', 'import')),
    decision_id INTEGER NOT NULL REFERENCES decisions (id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, version)
) STRICT;

INSERT INTO new_plan_versions (id, plan_id, version, body, origin, decision_id, created_at)
SELECT id, plan_id, version, body, origin, decision_id, created_at
FROM plan_versions;

DROP TABLE plan_versions;

ALTER TABLE new_plan_versions RENAME TO plan_versions;

CREATE TRIGGER trg_plan_versions_no_update
BEFORE UPDATE ON plan_versions
BEGIN
    SELECT RAISE(ABORT, 'plan_versions is append-only');
END;
