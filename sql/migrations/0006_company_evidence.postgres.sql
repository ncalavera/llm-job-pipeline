-- Raw evidence store for company profiling. One row per (company, source) fetch.
-- Sources: 'website' | 'perplexity' | 'exa' | 'deep_research'
CREATE TABLE IF NOT EXISTS company_evidence (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id  UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    url         TEXT,
    content     TEXT,
    meta        JSONB,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_company_evidence_company_source
    ON company_evidence (company_id, source);
