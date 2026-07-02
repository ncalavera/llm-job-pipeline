-- Score provenance (see postgres variant). sql/schema.sqlite.sql (the frozen
-- baseline) does not declare this column, so a fresh install applies this
-- migration cleanly -- unlike 0003/0005 there is no duplicate-column overlap
-- with the baseline to special-case here.
ALTER TABLE vacancy ADD COLUMN scored_by TEXT;
