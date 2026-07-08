-- 0017_screen_column_board_hidden — restore company.description parity and give
-- the board table a "hide from the dashboard" flag.
--
-- Two unrelated-but-tiny fixes ride together (both purely additive):
--
--   company.description — the cheap relevance screen (scripts/screen_candidates.py)
--       selects this column bare, but it drifted out of the live DB (it is declared
--       in the frozen baseline sql/schema.sql, never in a migration). Selecting a
--       missing column crashes the screen, which today lets ALL unscreened
--       candidates fall through into paid enrichment. Re-adding it makes the screen
--       run again; screen_candidates keeps a defensive name-only fallback so a
--       future drift degrades instead of crashing.
--   board.hidden — curation needs to HIDE a disabled board from the dashboard
--       Boards tab (hidden != deleted). Disabling stops a board fetching; hiding
--       stops it cluttering the view. Defaults FALSE so every existing board keeps
--       showing until explicitly hidden.
--
-- Additive (ADD COLUMN IF NOT EXISTS), so replaying against a DB that already has
-- either column is a clean no-op.

ALTER TABLE company ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE board ADD COLUMN IF NOT EXISTS hidden BOOLEAN DEFAULT FALSE;
