"""backfill_deadline_from_text — one small check.

Real bug it fixes: enrich_blind_vacancies.py used to write full_description
alone, so a role whose short board summary had no date stayed deadline=NULL
even once its full posting text (which usually names one) was fetched. Found
live on "Project Officer (Humanitarian Hub)" — see the commit for the DB
evidence. Mocked cursor: the function's own SQL uses a Postgres ``::uuid``
cast (this call site is Postgres-only already, like the rest of
enrich_blind_vacancies.py), so a real DB round-trip isn't the cheapest check.
"""

from unittest.mock import MagicMock

import database_supabase as db


def test_backfill_writes_when_extractable_and_deadline_missing():
    cur = MagicMock()
    wrote = db.backfill_deadline_from_text(cur, "vid-1", "<p>Apply by: September 29, 2026</p>")
    assert wrote is True
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "COALESCE(deadline" in sql
    assert params == ("2026-09-29", "vid-1")


def test_backfill_noop_when_text_has_no_date():
    cur = MagicMock()
    wrote = db.backfill_deadline_from_text(cur, "vid-2", "<p>No dates here.</p>")
    assert wrote is False
    cur.execute.assert_not_called()
