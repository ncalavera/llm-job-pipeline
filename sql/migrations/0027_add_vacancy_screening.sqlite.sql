-- 0027_add_vacancy_screening — SQLite variant (see .postgres.sql for the
-- rationale). JSON is TEXT here, timestamps are TEXT, like digest_sent_at.
-- The frozen baseline never carries these columns, so a bare ADD is correct.
ALTER TABLE vacancy ADD COLUMN screening TEXT;
ALTER TABLE vacancy ADD COLUMN screening_state TEXT;
ALTER TABLE vacancy ADD COLUMN screening_prepared_at TEXT;
ALTER TABLE vacancy ADD COLUMN screening_fingerprint TEXT;
