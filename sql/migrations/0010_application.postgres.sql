-- application — one row per job application (Postgres).
--
-- An application is a first-class entity, distinct from the vacancy it targets:
-- it carries its OWN lifecycle (applied -> interview -> offer/rejected), the
-- channel it went out on, the date, and references to the artifacts that were
-- sent (which CV version, the cover letter, interview-question answers, links
-- to research). Modeling it as a child table — not extra columns on vacancy —
-- keeps the fetcher-owned vacancy row (rewritten every refetch) cleanly
-- separated from user-owned application data, and lets one company profile show
-- its applications even after a vacancy row is archived or pruned.
--
-- The artifact FILES live only in the gitignored private zone
-- (config.APPLICATION_ARTIFACTS_DIR); this table stores their references +
-- small inline text (answers), never the personal files themselves.
--
-- One vacancy has at most one application: enforced by a partial unique index
-- on vacancy_id (NULLs — hand-added applications with no vacancy — are exempt).
CREATE TABLE IF NOT EXISTS application (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- SET NULL, not CASCADE: a pruned vacancy must not delete the record that
    -- you applied. The company link (below) keeps it reachable in the profile.
    vacancy_id    UUID REFERENCES vacancy(id) ON DELETE SET NULL,
    company_id    UUID NOT NULL REFERENCES company(id) ON DELETE CASCADE,

    channel       TEXT,  -- site | email | form | referral | other (advisory)
    status        TEXT NOT NULL DEFAULT 'applied'
                  CHECK (status IN ('draft', 'applied', 'interview',
                                    'offer', 'rejected', 'withdrawn')),
    applied_at    DATE,

    -- Artifact references + inline answers, e.g.
    -- {cv_version, cover_letter_path, answers, research_urls: [...]}.
    -- File references into the private zone only — never file contents.
    artifacts     JSONB DEFAULT '{}',
    notes         TEXT,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_application_company ON application (company_id);
CREATE INDEX IF NOT EXISTS idx_application_vacancy ON application (vacancy_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_vacancy_unique
    ON application (vacancy_id) WHERE vacancy_id IS NOT NULL;
