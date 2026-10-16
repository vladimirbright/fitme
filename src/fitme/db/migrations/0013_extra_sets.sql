-- 0013_extra_sets.sql
--
-- ADR 0012: a set done beyond the prescription (three sets of a two-set exercise) is logged
-- as its own row. Prescribed rows keep being matched to the workout by position (A§4.2);
-- an extra row names the block and item it belongs to instead, so it never shifts that
-- matching. Both columns are NULL on every prescribed row, and on every row written before
-- this migration; the application fills both or neither.

ALTER TABLE set_logs ADD COLUMN extra_for_block INTEGER CHECK (extra_for_block >= 0);
ALTER TABLE set_logs ADD COLUMN extra_for_item INTEGER CHECK (extra_for_item >= 0);
