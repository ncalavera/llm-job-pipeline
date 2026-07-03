"""Cross-source company dedup: a whitespace-mangled org string must still
resolve to the SAME company as the clean one.

Vacancy dedup (``make_vacancy_id``) is scoped by ``org|title``: the exact-hash
lookup in ``_find_existing_vacancy`` is a global query, so two saves for the
same org+title only collapse to one row when both paths resolve the org to the
BYTE-IDENTICAL canonical string. ``company_registry.resolve_canonical_name``
never stripped its input, so a source whose org field carries incidental
whitespace (e.g. ``scripts/fetchers/boards/algolia.py``'s ``company_name``,
unlike its sibling fetchers which all ``.strip()``) fails every lookup stage,
falls through to passthrough, and ``ensure_company`` forks a SECOND company row
for the same real-world org. Every vacancy for that org then duplicates: one
row per company row, both with the same title/score/age, exactly the
dashboard symptom (the same posting rendered as two identical cards).

This is a same-company, whitespace-only match — orthogonal to the separate
vacancy-TITLE-side norm/desc/URL guards that decide whether two same-company
postings are a rename or distinct sibling roles. Fixing org resolution does
not touch title matching at all.

Harness mirrors tests/test_dedup_renamed_roles.py: SQLite backend, isolated
temp DB per test, reload the DAL chain. All orgs/roles are invented.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "dedup_sweep",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "test must run on the SQLite backend"
    import database_supabase as db

    yield db
    db.close_conn()


def _job(title, *, org=None, city="San Francisco, USA", url=None):
    job = {
        "title": title,
        "snippet": f"{title} -- a genuine open role with real responsibilities.",
        "full_description": f"We are hiring a {title}. " * 12 + "Own the work end to end.",
        "location": city,
    }
    if org is not None:
        job["org_override"] = org
    if url is not None:
        job["url"] = url
    return job


def test_leading_whitespace_org_does_not_fork_company(dal):
    """A board org string with a stray leading space (algolia.py's un-.strip()'d
    ``company_name``) must resolve onto the SAME company as the clean ATS org,
    so the same role saved via both paths collapses to one vacancy row."""
    title = "Senior Operations Associate, Office of the Chief Executive Officer"

    # Path 1: direct ATS fetch — clean org string.
    dal.save_vacancies("Coefficient Giving", "B", [_job(title)])
    dal.get_conn().commit()

    # Path 2: job board fetch — org string carries a leading space, exactly the
    # shape algolia.py (80,000 Hours board) produces since it never strips
    # hit.get("company_name").
    board_cfg = {"name": "80000 Hours", "url": "https://board.test/feed", "tier": "B"}
    dal.save_board_vacancies(board_cfg, [_job(title, org=" Coefficient Giving")])
    dal.get_conn().commit()

    rows = dal.load_vacancies(include_inactive_companies=True)
    matching = [v for v in rows.values() if v["title"] == title]
    assert len(matching) == 1, (
        f"expected one merged vacancy, got {len(matching)} — "
        "org-name whitespace forked a duplicate company/vacancy row"
    )

    cur = dal.get_conn().cursor()
    cur.execute("SELECT COUNT(*) FROM company WHERE canonical_name LIKE '%Coefficient Giving%'")
    (company_count,) = cur.fetchone()
    cur.close()
    assert company_count == 1, (
        f"expected one company row, got {company_count} — "
        "whitespace-mangled org string created a duplicate company"
    )
