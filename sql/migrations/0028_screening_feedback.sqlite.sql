-- Explicit user reasons survive browser sessions; agents propose, never auto-apply.
CREATE TABLE IF NOT EXISTS screening_feedback (
    id TEXT PRIMARY KEY,
    vacancy_ids TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('liked', 'passed')),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0 AND length(reason) <= 4000),
    group_label TEXT NOT NULL CHECK (length(group_label) <= 200),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'reviewed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    review_outcome TEXT,
    review_session TEXT,
    CHECK (status = 'pending' OR
        (reviewed_at IS NOT NULL AND review_outcome IS NOT NULL
         AND review_session IS NOT NULL AND length(trim(review_outcome)) > 0
         AND length(trim(review_session)) > 0))
);
CREATE INDEX IF NOT EXISTS idx_screening_feedback_pending
    ON screening_feedback (status, created_at);
