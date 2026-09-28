-- 0004_login_codes_created_at.sql
--
-- M9 website OTP (A§9.2): `POST /auth/request-code` is rate-limited per A§9.1 (1 per 60s,
-- 5 per hour) and `services.webauth` needs each code's issue time to enforce that, and to
-- look codes up "by user_id + latest unused, not by hash alone" (the M1 note). `login_codes`
-- had no timestamp column at all (0001_init.sql), so this adds one.
--
-- A plain ADD COLUMN, not a table rebuild: no existing CHECK/constraint changes, so the
-- simple form is enough (A§4.7's 12-step rebuild procedure is only needed to change an
-- existing column). Nullable rather than `NOT NULL DEFAULT ...`: SQLite's ADD COLUMN would
-- otherwise need a constant default, and every row that predates this migration was already
-- consumed or expired long before the website existed, so a NULL for those is harmless.
-- `db/controllers/auth.py::insert_login_code` always writes it going forward.

ALTER TABLE login_codes ADD COLUMN created_at TEXT;

-- `services.webauth` looks up "the latest unused code for this user" (the M1 note), and the
-- rate limiter counts recent codes for this user: both scan by user_id, newest first.
CREATE INDEX idx_login_codes_user_created ON login_codes (user_id, created_at);
