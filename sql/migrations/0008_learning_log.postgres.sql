-- Learning-cycle ledger. Append-only record that closes the feedback loop
-- (STRATEGY guardrail 8): verdicts teach filters/scoring/boards, every applied
-- change is logged, nothing edits itself silently.
--
-- One row per event, three kinds:
--   'garbage'  — a verdict flagged as a filter hole (reached scoring for
--                nothing). ref = vacancy id; detail = {title, source, score}.
--                Feeds filter-word proposals. Distinct from a plain pass
--                ('not mine'), which stays a vacancy status and calibrates
--                scoring instead.
--   'reviewed' — a COMPLETED learning-review cycle. detail = {agreement, ...}.
--                Its created_at is the rollover cursor: verdicts decided after
--                the latest 'reviewed' row are the undiscussed ones. A SKIPPED
--                review writes no row, so its verdicts roll over untouched.
--   'applied'  — an approved change was applied. detail = {kind, before, after}.
--                The audit trail for filter/profile/board edits.
CREATE TABLE IF NOT EXISTS learning_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT NOT NULL,
    ref         TEXT,
    detail      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_learning_log_kind_created
    ON learning_log (kind, created_at);
