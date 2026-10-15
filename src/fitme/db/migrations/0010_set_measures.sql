-- 0010_set_measures.sql
--
-- ADR 0009: a prescribed set is measured in reps, time or distance. A timed set (a plank, a
-- battle-rope interval) records its target and result in seconds, a distance set (a carry,
-- a sled push) in meters; a reps set keeps using planned_reps_min/max and actual_reps.
-- Exactly one family is filled per row, by the application; NULL in all four for every row
-- written before this migration (they are all reps sets).

ALTER TABLE set_logs ADD COLUMN planned_seconds INTEGER CHECK (planned_seconds > 0);
ALTER TABLE set_logs ADD COLUMN planned_meters INTEGER CHECK (planned_meters > 0);
ALTER TABLE set_logs ADD COLUMN actual_seconds INTEGER CHECK (actual_seconds >= 0);
ALTER TABLE set_logs ADD COLUMN actual_meters INTEGER CHECK (actual_meters >= 0);
