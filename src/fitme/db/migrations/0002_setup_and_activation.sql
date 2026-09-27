-- 0002_setup_and_activation.sql
--
-- M5 additions (A§5, A§6.1). Never edit 0001_init.sql (A§4.7): new tables only.
--
-- `setup_progress`: a durable marker for where the setup questionnaire (A§5.1) currently is,
-- so a bot restart mid-setup resumes at the same step instead of relying on in-memory aiogram
-- FSM state. `step` is one of `services.profile.STEP_ORDER`. `data` is scratch JSON for an
-- in-progress multi-select answer that hasn't been committed to `profiles`/`screening_flags`
-- yet (the committed answer is only ever written once the user finishes that step), plus an
-- optional `resume_step` used by a `/profile` edit (A§6.2) to remember where to return to
-- after re-running one or more screening steps.
--
-- `activation_state`: a single counter row for wrong `/activate` codes (A§6.1: "a
-- failed-attempt limit, e.g. 5 wrong codes invalidates the pending activation codes"). Not
-- part of `activation_codes` itself because the counter tracks *attempts*, not a specific
-- code, and must survive a code being replaced.

CREATE TABLE setup_progress (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_type(data) = 'object'),
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE activation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
) STRICT;
