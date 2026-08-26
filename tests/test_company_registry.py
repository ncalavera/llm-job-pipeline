"""Tests for the company registry: loading, alias resolution, DB-outage vs
empty-table signalling, and website resolution.

Absorbed from tests/test_config.py, tests/test_company_registry_load.py,
tests/test_company_url_resolution.py. Covers: resolve_canonical_name's
alias/case-insensitive matching against an injected registry, the loader's
row -> config mapping and job-board opt-in, the REGISTRY_LOAD_FAILED signal
that distinguishes a DB outage from a genuinely empty table (with retry-then-
succeed and failure-then-recovery), and the domain-match verifier + resolver
that leaves a company's website unresolved rather than storing a wrong guess.
"""

import pytest
import company_registry
import find_company_urls as f
from config import resolve_canonical_name


# --- from test_config.py ---
#
# Tests for resolve_canonical_name and the company registry machinery.
#
# These exercise the alias-resolution LOGIC, not any specific company. The
# registry they resolve against is INJECTED (made-up placeholder names), so they
# encode no particular company list and, unlike the versions they replace, they
# still test something when there is no database: those skipped whenever the
# registry was empty, which is every run without a DB — including CI.


@pytest.fixture()
def registry(monkeypatch):
    """A two-company registry, injected the way the loader would have built it.

    resolve_canonical_name reads these module globals at call time, so patching
    them exercises the real resolution order against known data."""
    companies = {
        "Northwind Aid Trust": {"strategy": "greenhouse", "status": "active", "slug": "northwind"},
        "Fictive Robotics Guild": {"strategy": "lever", "status": "active", "slug": "frg"},
    }
    monkeypatch.setattr(company_registry, "COMPANIES", companies)
    monkeypatch.setattr(company_registry, "_COMPANIES_LOWER", {k.lower(): k for k in companies})
    monkeypatch.setattr(company_registry, "_ALIAS_INDEX", {"northwind": "Northwind Aid Trust"})
    return companies


# ---------------------------------------------------------------------------
# resolve_canonical_name — generic machinery
# ---------------------------------------------------------------------------


def test_CN01_exact_companies_key_returned_as_is(registry):
    """An exact registry key resolves to itself."""
    for key in registry:
        assert resolve_canonical_name(key) == key


def test_CN02_case_insensitive_companies_match(registry):
    """Resolution is case-insensitive for an existing registry key."""
    for key in registry:
        assert resolve_canonical_name(key.lower()) == key
        assert resolve_canonical_name(key.upper()) == key


def test_CN03_alias_resolves_to_its_canonical_name(registry):
    """An alias (any case) resolves; the whitespace normalizer runs first."""
    assert resolve_canonical_name("Northwind") == "Northwind Aid Trust"
    assert resolve_canonical_name("  northwind  ") == "Northwind Aid Trust"


def test_CN06_unknown_name_returned_unchanged():
    """An unknown name passes through unchanged (no false match)."""
    name = "SomeCompletelyUnknownOrg_XYZ_12345"
    assert resolve_canonical_name(name) == name


def test_CN08_loader_gives_every_entry_a_strategy(monkeypatch):
    """The loader's row -> config mapping, driven by a fake company table.

    Every monitored entry must carry 'strategy' (the fetch dispatcher keys on
    it), a scrape strategy must get its 'url', and a nonsense tier is dropped
    rather than passed through."""
    rows = [
        ("Northwind Aid Trust", "greenhouse", "active", "A", "", "northwind", None, None),
        (
            "Fictive Robotics Guild",
            "firecrawl_scrape",
            "active",
            "not-a-tier",
            "https://frg.test/jobs",
            None,
            {"depth": 2},
            None,
        ),
    ]

    class _Cursor:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(company_registry, "get_conn", lambda: _Conn())
    built = company_registry._build_companies_from_db()

    assert set(built) == {"Northwind Aid Trust", "Fictive Robotics Guild"}
    for name, cfg in built.items():
        assert "strategy" in cfg, f"{name} missing 'strategy' key"
    assert built["Northwind Aid Trust"]["tier"] == "A"
    assert built["Fictive Robotics Guild"]["tier"] is None
    assert built["Fictive Robotics Guild"]["url"] == "https://frg.test/jobs"
    assert built["Fictive Robotics Guild"]["depth"] == 2
    assert company_registry.registry_load_failed() is False


def test_CN09_all_known_names_covers_every_company_row(monkeypatch):
    """`config._ALL_CSV_NAMES` is the backward-compat name for the registry's
    _ALL_KNOWN_NAMES: EVERY company row, including the inactive and candidate
    ones the monitored registry leaves out.

    (The assertion this replaces compared two lengths that are both 0 without a
    database, so it passed on nothing.)"""
    from config import _ALL_CSV_NAMES

    assert isinstance(_ALL_CSV_NAMES, set)

    rows = [("Northwind Aid Trust",), ("Fictive Robotics Guild",), ("Dormant Trust",)]

    class _Cursor:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    monkeypatch.setattr(company_registry, "get_conn", lambda: _Conn())
    assert company_registry._load_all_known_names() == {name for (name,) in rows}


# ---------------------------------------------------------------------------
# Job boards are opt-in
# ---------------------------------------------------------------------------


def test_boards_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JOB_BOARDS", raising=False)
    import config as cfg

    assert cfg._select_enabled_boards() == {}


def test_boards_enable_subset_via_env(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "reliefweb")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == {"reliefweb"}


def test_boards_enable_all_via_env(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "all")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == set(cfg._ALL_JOB_BOARDS)


def test_boards_unknown_id_ignored(monkeypatch):
    monkeypatch.setenv("JOB_BOARDS", "80k_hours,does_not_exist")
    import config as cfg

    enabled = cfg._select_enabled_boards()
    assert set(enabled) == {"80k_hours"}


# --- from test_company_registry_load.py ---
#
# Tests for the company registry load-failure signal.
#
# COMPANIES degrades to {} on a DB outage AND on a genuinely empty/fresh table —
# identical states. These tests pin that _build_companies_from_db() now exposes a
# distinct REGISTRY_LOAD_FAILED / registry_load_failed() signal so /jobs-new can
# hard-stop on an outage instead of offering destructive onboarding.
#
# The module eager-loads at import, so we call _build_companies_from_db() directly
# with company_registry.get_conn patched rather than relying on import-time state.
# Globals are reset after each test so we don't pollute other modules.


@pytest.fixture(autouse=True)
def _reset_registry_globals():
    """Restore the load-failure globals after each test."""
    failed = company_registry.REGISTRY_LOAD_FAILED
    error = company_registry.REGISTRY_LOAD_ERROR
    yield
    company_registry.REGISTRY_LOAD_FAILED = failed
    company_registry.REGISTRY_LOAD_ERROR = error


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Skip the between-retry backoff so the retry-path tests run instantly."""
    monkeypatch.setattr(company_registry.time, "sleep", lambda *_a, **_k: None)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        pass

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def commit(self):
        pass


def test_healthy_load_sets_no_failure(monkeypatch):
    """One active company row → COMPANIES non-empty and load did NOT fail."""
    row = (
        "Acme Corp",
        "greenhouse",
        "active",
        "A",
        "https://acme.example/careers",
        "acme",
        None,
        "tech",
    )
    monkeypatch.setattr(company_registry, "get_conn", lambda: _FakeConn([row]))

    companies = company_registry._build_companies_from_db()

    assert companies  # non-empty
    assert "Acme Corp" in companies
    assert company_registry.registry_load_failed() is False
    assert company_registry.REGISTRY_LOAD_ERROR is None


def test_db_failure_sets_failure_signal(monkeypatch):
    """get_conn raising (DB outage) → {} returned AND failure flag set True."""

    def _boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(company_registry, "get_conn", _boom)

    companies = company_registry._build_companies_from_db()

    assert companies == {}
    assert company_registry.REGISTRY_LOAD_FAILED is True
    assert company_registry.registry_load_failed() is True
    assert "connection refused" in (company_registry.REGISTRY_LOAD_ERROR or "")


def test_transient_contention_retries_then_succeeds(monkeypatch):
    """A transient lock/timeout on the first attempt must NOT be reported as an
    outage: a bounded retry recovers the healthy load, so REGISTRY_LOAD_FAILED
    stays False and /jobs-new is not hard-aborted over a few seconds of ordinary
    DB contention."""
    row = (
        "Acme Corp",
        "greenhouse",
        "active",
        "A",
        "https://acme.example/careers",
        "acme",
        None,
        "tech",
    )

    class _FlakyConn:
        def __init__(self):
            self.cursor_calls = 0

        def cursor(self):
            self.cursor_calls += 1
            if self.cursor_calls == 1:
                raise RuntimeError("canceling statement due to lock timeout")
            return _FakeCursor([row])

        def rollback(self):
            pass

        def commit(self):
            pass

    conn = _FlakyConn()
    monkeypatch.setattr(company_registry, "get_conn", lambda: conn)

    companies = company_registry._build_companies_from_db()

    assert "Acme Corp" in companies
    assert conn.cursor_calls == 2  # first attempt failed, retry succeeded
    assert company_registry.registry_load_failed() is False
    assert company_registry.REGISTRY_LOAD_ERROR is None


def test_failure_then_success_resets_flag(monkeypatch):
    """A failed load followed by a healthy one flips the flag back to False —
    the reset contract /jobs-new relies on (outage → recovery, no stale True)."""
    monkeypatch.setattr(
        company_registry, "get_conn", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert company_registry._build_companies_from_db() == {}
    assert company_registry.registry_load_failed() is True

    row = (
        "Acme Corp",
        "greenhouse",
        "active",
        "A",
        "https://acme.example/careers",
        "acme",
        None,
        "tech",
    )
    monkeypatch.setattr(company_registry, "get_conn", lambda: _FakeConn([row]))
    assert company_registry._build_companies_from_db()  # non-empty
    assert company_registry.registry_load_failed() is False
    assert company_registry.REGISTRY_LOAD_ERROR is None


# --- from test_company_url_resolution.py ---
#
# BUG-7 — company enrichment must not store a wrong/blank website.
#
# Enrichment used to take the top search hit as the homepage without verifying
# it belonged to the org, so ALONE → history.com and 01Health → vestbee.com. A
# wrong homepage feeds the evidence scrape another org's content and corrupts the
# WANT score. These tests cover the domain-match verifier and the resolver that
# now leaves the site UNRESOLVED (empty) with a visible flag when nothing matches.

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
