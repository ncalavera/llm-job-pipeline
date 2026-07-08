"""BUG-7 — company enrichment must not store a wrong/blank website.

Enrichment used to take the top search hit as the homepage without verifying
it belonged to the org, so ALONE → history.com and 01Health → vestbee.com. A
wrong homepage feeds the evidence scrape another org's content and corrupts the
WANT score. These tests cover the domain-match verifier and the resolver that
now leaves the site UNRESOLVED (empty) with a visible flag when nothing matches.
"""

import find_company_urls as f


# ---------------------------------------------------------------------------
# _domain_matches_company — the verification heuristic
# ---------------------------------------------------------------------------


class TestDomainMatchesCompany:
    def test_rejects_alone_history(self):
        # The observed failure: "ALONE" grabbed history.com (the TV show).
        assert f._domain_matches_company("ALONE", "https://history.com") is False

    def test_rejects_01health_vestbee(self):
        # The observed failure: "01Health" grabbed vestbee.com (an aggregator).
        assert f._domain_matches_company("01Health", "https://vestbee.com") is False

    def test_accepts_token_in_domain(self):
        assert f._domain_matches_company("Open Philanthropy", "https://www.openphilanthropy.org")

    def test_accepts_single_token_org(self):
        assert f._domain_matches_company("GiveWell", "https://givewell.org")

    def test_accepts_compressed_name(self):
        # "80,000 Hours" → 80000hours.org
        assert f._domain_matches_company("80,000 Hours", "https://80000hours.org")

    def test_accepts_acronym(self):
        # Children's Investment Fund Foundation → ciff.org (possessive 's must
        # not corrupt the acronym).
        assert f._domain_matches_company(
            "Children's Investment Fund Foundation", "https://ciff.org"
        )

    def test_accepts_acronym_skipping_stopwords(self):
        # Real-world acronyms drop connector words: International Committee of
        # the Red Cross → ICRC (not ICOTRC).
        assert f._domain_matches_company(
            "International Committee of the Red Cross", "https://www.icrc.org"
        )

    def test_accepts_diacritics_folded(self):
        # NFKD fold: Médecins Sans Frontières must tokenize cleanly and match
        # its ASCII acronym domain.
        assert f._domain_matches_company("Médecins Sans Frontières", "https://www.msf.org")

    def test_accepts_co_uk_suffix(self):
        assert f._domain_matches_company("Acme Trust", "https://acme.org.uk")

    def test_rejects_generic_only_overlap(self):
        # Only the generic suffix "Foundation" overlaps — not an identifying
        # match, so a stranger's "foundation.com" is rejected.
        assert (
            f._domain_matches_company("Acme Health Foundation", "https://foundation.com") is False
        )


# ---------------------------------------------------------------------------
# _search_website — verified pick or unresolved-with-flag
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self, url):
        self.url = url


class _Results:
    def __init__(self, items):
        self.data = items


class _FakeClient:
    def __init__(self, items):
        self._items = items

    def search(self, query, limit):
        return _Results(self._items)


class TestSearchWebsite:
    def test_returns_verified_domain(self):
        client = _FakeClient([_Item("https://givewell.org/about")])
        assert f._search_website(client, "GiveWell") == "https://givewell.org"

    def test_skips_unrelated_top_hit_and_takes_matching(self):
        # Top hit is a stranger; a later result matches → return the match.
        client = _FakeClient(
            [
                _Item("https://history.com/alone"),
                _Item("https://openphilanthropy.org/grants"),
            ]
        )
        assert f._search_website(client, "Open Philanthropy") == "https://openphilanthropy.org"

    def test_unresolved_when_no_match(self, capsys):
        # ALONE → only history.com: no confident match → unresolved (None) with
        # a visible flag, so the evidence scrape skips instead of scraping a
        # stranger's site.
        client = _FakeClient([_Item("https://history.com/alone-show")])
        assert f._search_website(client, "ALONE") is None
        out = capsys.readouterr().out
        assert "website unresolved" in out

    def test_skips_job_boards(self):
        # A LinkedIn hit is skipped; nothing else matches → unresolved.
        client = _FakeClient([_Item("https://linkedin.com/company/alone")])
        assert f._search_website(client, "ALONE") is None


# ---------------------------------------------------------------------------
# Vacancy-first gate (R3): the ghost branch only searches for a URL once a
# vacancy has EARNED the stranger — otherwise the paid Firecrawl search never
# fires. DB-backed on an isolated temp SQLite DB.
# ---------------------------------------------------------------------------

import importlib  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))
    for mod in ("database_supabase", "config", "company_registry", "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE
    import database_supabase  # noqa: F401  (initializes the schema)

    yield db_backend
    db_backend.close_conn()


def _ghost(conn, name):
    cid = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO company (id, canonical_name, status, website) VALUES (%s, %s, 'candidate', '')",
        (cid, name),
    )
    conn.commit()
    cur.close()
    return cid


def _earn(conn, company_id, llm_score=90):
    vid = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, first_seen, last_seen, "
        "status, llm_score) VALUES (%s, %s, %s, 'Role', '2026-01-01', '2026-01-01', 'unseen', %s)",
        (vid, vid, company_id, llm_score),
    )
    conn.commit()
    cur.close()


def test_load_companies_to_find_gates_ghosts_on_earning_vacancy(sqlite_db, capsys):
    conn = sqlite_db.get_conn()
    earned = _ghost(conn, "Harborlight Trust")
    _ghost(conn, "Driftwood Society")  # no earning vacancy → stays unearned
    _earn(conn, earned, llm_score=90)

    names = f._load_companies_to_find()
    assert "Harborlight Trust" in names
    assert "Driftwood Society" not in names  # unearned ghost is not searched
    assert "1 ghost candidate(s) waiting unearned" in capsys.readouterr().out
