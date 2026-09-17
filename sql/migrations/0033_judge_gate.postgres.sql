-- 0033_judge_gate — the LLM judge + human review layer.
--
-- `judge_state` is the judge's own verdict on a role, independent of `status`
-- (the pipeline's inbox/decided lifecycle). 'pending' = not judged yet.
-- 'keep'/'unsure'/'killed' mirror the judge's KEEP/UNSURE/KILL verdict once it
-- runs. A KILL also moves `status` to 'passed' (done by the judge job itself,
-- not by this migration) so the row leaves the inbox; judge_state records WHY
-- independently of status, so an audit can tell a judge-kill from any other
-- pass reason.
--
-- `description_source` is provenance for `full_description`, added because the
-- judge must never score an AI-written board summary as if it were the
-- posting: 'source_page' (fetched from the posting/apply URL), 'feed' (full
-- body delivered by the board feed itself, e.g. ReliefWeb RSS), 'board_summary'
-- (the board's own summary — not judgeable), NULL = legacy/unknown (direct-ATS
-- rows, judgeable when text length clears the existing threshold).
--
-- `judge_review` holds the user's own answers from the Review tab and Today
-- cards — one current answer per role (UNIQUE vacancy_id, upserted), plus a
-- `draw`/`split` label frozen at first insert so a role's lesson/exam bucket
-- never moves once assigned.
--
-- Fully additive: two nullable/defaulted columns and one new table. Guarded
-- with IF NOT EXISTS like 0025/0026/0027.

ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS judge_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (judge_state IN ('pending', 'keep', 'unsure', 'killed'));
ALTER TABLE vacancy ADD COLUMN IF NOT EXISTS description_source TEXT;

CREATE TABLE IF NOT EXISTS judge_review (
    id            BIGSERIAL PRIMARY KEY,
    vacancy_id    UUID NOT NULL UNIQUE REFERENCES vacancy(id),

    -- Where the answer was given: the dedicated Review tab, or a Today card.
    source        TEXT NOT NULL CHECK (source IN ('review', 'today')),

    -- the user's verdict. NULL means comment-only (he left a note but did not
    -- vote yet).
    verdict       TEXT CHECK (verdict IN ('keep', 'unsure', 'remove')),
    comment       TEXT,

    -- Snapshot of the judge's own verdict at the time the user answered, so a
    -- later re-judge does not rewrite what he was actually agreeing/disagreeing
    -- with.
    judge_verdict TEXT,
    brief_version TEXT,

    -- Coin flip, decided once at first insert and never changed on update:
    -- draw = how the role was surfaced ('random' | 'hard' | 'today'),
    -- split = which bucket it counts toward ('lesson' | 'exam').
    draw          TEXT CHECK (draw IN ('random', 'hard', 'today')),
    split         TEXT CHECK (split IN ('lesson', 'exam')),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Judge queue / audit sampling reads by state; cheap on a table this size.
CREATE INDEX IF NOT EXISTS idx_vacancy_judge_state ON vacancy (judge_state);
