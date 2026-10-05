-- 0008_conversations.sql
--
-- ADR 0004: conversation sessions. A free-text exchange with the assistant belongs to one of
-- two kinds of session, and its history lives only as long as that session:
--
--   planning  — a new plan or a change of an existing one, iterated on a draft until the
--               owner saves it (a new plan version) or closes it without saving;
--   training  — a started workout, from Start until it is completed, aborted or halted.
--
-- `conversations` is the session itself (no text); `chat_messages.conversation_id` ties the
-- owner's messages *and* now the assistant's own replies (`direction = 'out'`, which
-- 0001_init.sql already allowed) to it, so the assistant can see the last turns. Raw text
-- stays in `chat_messages` only and keeps its retention (A§8.4).
--
-- `plan_id` is the plan being changed (NULL for a new plan); deleting that plan deletes its
-- session (CASCADE), it must never silently turn into a "new plan" session.
-- `draft_decision_id` is the session's current draft round (a `plan_generate`/
-- `plan_revise`/`plan_import` decision), what "Save" confirms.
-- A plain ADD COLUMN for `chat_messages`: nullable, no CHECK change, so no rebuild (as 0004).

CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('planning', 'training')),
    plan_id INTEGER REFERENCES plans (id) ON DELETE CASCADE,
    workout_session_id INTEGER REFERENCES workout_sessions (id) ON DELETE CASCADE,
    draft_decision_id INTEGER REFERENCES decisions (id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'saved', 'discarded', 'closed')),
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK (kind = 'planning' OR workout_session_id IS NOT NULL)
) STRICT;

CREATE INDEX idx_conversations_user_status ON conversations (user_id, status);

ALTER TABLE chat_messages
    ADD COLUMN conversation_id INTEGER REFERENCES conversations (id) ON DELETE SET NULL;

CREATE INDEX idx_chat_messages_conversation ON chat_messages (conversation_id);
