-- Persist the decision receipt atomically so a lost response can be retried.
CREATE TABLE IF NOT EXISTS screening_decision (
    operation_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
