-- Persist the decision receipt atomically so a lost response can be retried.
CREATE TABLE IF NOT EXISTS screening_decision (
    operation_id UUID PRIMARY KEY,
    request JSONB NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
