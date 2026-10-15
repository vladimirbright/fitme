-- 0011_exclusions_and_sport.sql
--
-- ADR 0009: hard exercise exclusions and other sport training.
--
-- `exercise_exclusions`: what the user never wants prescribed — a whole movement pattern
-- (`pattern`, e.g. every squat-pattern exercise), one catalog exercise (`exercise`), or every
-- exercise whose id, names or search terms contain a word (`name`, e.g. "lunge"). Applied in
-- code to the allowed exercise set and by the `plan.excluded` guard. A training preference,
-- never a medical fact: it is not a screening flag and gates nothing else.
--
-- `profiles.sport` / `sport_sessions_per_week`: training the user does outside this app
-- (e.g. boxing three times a week), so plans and the workload check account for it. NULL
-- for a profile set up before this migration, read as "none".

CREATE TABLE exercise_exclusions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('pattern', 'exercise', 'name')),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 60),
    created_at TEXT NOT NULL,
    UNIQUE (user_id, kind, value)
);

ALTER TABLE profiles ADD COLUMN sport TEXT CHECK (sport IN (
    'none', 'boxing_martial_arts', 'running', 'cycling', 'swimming', 'team_sport',
    'racket_sport', 'climbing', 'other'
));
ALTER TABLE profiles ADD COLUMN sport_sessions_per_week INTEGER
    CHECK (sport_sessions_per_week BETWEEN 1 AND 14);
