"""Cross-source dedup on the apply URL: tracking params and connective words.

Production regression (run 2026-08-04): the SAME Ashby requisition reached the
DB twice per role because two boards decorate the link differently —

  * 80,000 Hours board:  .../job-id?utm_source=...&utm_medium=job-board
  * Probably Good board: .../job-id            (bare)

— and the byte-wise URL guard read those as two distinct reqs (3 pairs). A
fourth pair ("Director of GLP-1 in India Fund" vs "Director, GLP-1 in India
Fund", identical URL) forked because the same-URL merge requires title
containment and "of" broke it. Two J-PAL pairs with IDENTICAL URLs sat in
MANUAL REVIEW because both rows were live.

Contract under test:

  * normalize_apply_url() strips utm_* / tracking params and the fragment,
    keeps job-identifying params, lowercases scheme+host;
  * the save path folds a board variant whose URL differs only by tracking
    params onto the existing row;
  * the same-URL merge tolerates connective-word retitles ("Director of X" ==
    "Director, X") but never merges different roles on a shared generic URL;
  * dedup_sweep clusters same-URL rows even when title keys and description
    fingerprints both miss, and auto-collapses a both-live same-URL pair.

Same SQLite harness as tests/test_dedup_renamed_roles.py. Orgs invented.
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


def _commit(db):
    db.get_conn().commit()


_ASHBY = "https://jobs.ashbyhq.com/acme/9576d650-a615-46c9-9187-9610e420a4a3"
_ASHBY_UTM = _ASHBY + "?utm_source=zbZ8a2qqDv&utm_source=80000hours&utm_medium=job-board"


def _job(title, *, url, desc=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc
        or (f"We are hiring a {title}. " * 12 + "Own the work end to end."),
        "location": "Remote",
        "url": url,
    }


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, dedup_hash, title, status FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure URL normalization contract
# ---------------------------------------------------------------------------


def test_tracking_params_and_fragment_stripped(dal):
    n = dal.normalize_apply_url
    assert n(_ASHBY_UTM) == _ASHBY
    assert n(_ASHBY + "#apply") == _ASHBY
    assert n("HTTPS://Jobs.AshbyHQ.com/acme/x") == "https://jobs.ashbyhq.com/acme/x"
    assert n("  " + _ASHBY + "  ") == _ASHBY
    assert n("") == "" and n(None) == ""


def test_job_identifying_params_survive(dal):
    n = dal.normalize_apply_url
    gh = "https://boards.greenhouse.io/acme/jobs/123?gh_jid=123"
    assert n(gh + "&utm_source=x") == gh
    assert n(gh) == gh


def test_titles_equal_sans_stopwords(dal):
    strong = dal._normalize_title_strong
    eq = dal._titles_equal_sans_stopwords
    assert eq(strong("Director of GLP-1 in India Fund"), strong("Director, GLP-1 in India Fund"))
    assert not eq(strong("Director of Finance"), strong("Director of Programs"))


# ---------------------------------------------------------------------------
# Save path: board variant with a utm-decorated URL folds onto the existing row
# ---------------------------------------------------------------------------


def test_utm_variant_folds_onto_existing_row(dal):
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies("Acme Fund", "A", [_job("Headhunting Product Specialist", url=_ASHBY_UTM)])
    _commit(dal)

    # Another board ships the SAME req: bare URL, suffixed title, its own stub body.
    dal.save_board_vacancies(
        {"name": "Probably Good", "url": "https://probablygood.test"},
        [
            {
                **_job(
                    "Headhunting Product Specialist, Career Services",
                    url=_ASHBY,
                    desc="Short board stub for the same role." * 3,
                ),
                "org_override": "Acme Fund",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_connective_word_retitle_same_url_folds(dal):
    dal.ensure_company("Acme Fund", status="active")
    url = "https://acme.test/careers/director-glp-1-in-india-fund"
    dal.save_vacancies("Acme Fund", "A", [_job("Director of GLP-1 in India Fund", url=url)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Probably Good", "url": "https://probablygood.test"},
        [
            {
                **_job(
                    "Director, GLP-1 in India Fund",
                    url=url,
                    desc="A board's own different summary of the same role." * 6,
                ),
                "org_override": "Acme Fund",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_generic_careers_url_never_merges_different_roles(dal):
    dal.ensure_company("Acme Fund", status="active")
    generic = "https://acme.test/careers"
    dal.save_vacancies("Acme Fund", "A", [_job("Director of Finance", url=generic)])
    _commit(dal)
    dal.save_vacancies("Acme Fund", "A", [_job("Director of Programs", url=generic)])
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


# ---------------------------------------------------------------------------
# Sweep: same-URL rows cluster and a both-live same-URL pair auto-collapses
# ---------------------------------------------------------------------------


def _sweep(monkeypatch, apply=False):
    import dedup_sweep

    importlib.reload(dedup_sweep)
    argv = ["dedup_sweep.py"] + (["--apply"] if apply else [])
    monkeypatch.setattr(sys, "argv", argv)
    dedup_sweep.main()


def test_sweep_collapses_both_live_same_url_pair(dal, monkeypatch, capsys):
    """Two live rows, same normalized URL, different titles AND different
    bodies (title key + description fingerprint both miss) — the URL key must
    cluster them and the both-live guard must NOT hold the pair for manual
    review, because one apply URL is one requisition."""
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies("Acme Fund", "A", [_job("Talent Database Lead", url=_ASHBY_UTM)])
    _commit(dal)
    # Inject the second row for the same req BEHIND the save path (simulates a
    # row stored before URL normalization existed): fork it as a genuinely
    # different role first, then repoint its URL at the shared req.
    dal.save_vacancies(
        "Acme Fund",
        "A",
        [
            _job(
                "Talent Database Lead, Career Services",
                url="https://elsewhere.test/other-req",
                desc="Completely different stub body from the other board." * 6,
            )
        ],
    )
    _commit(dal)
    rows = _raw_rows(dal)
    assert len(rows) == 2, [r["title"] for r in rows]
    cur = dal.get_conn().cursor()
    legacy_hash = dal.make_vacancy_id("Acme Fund", "Talent Database Lead, Career Services")
    cur.execute(
        "UPDATE vacancy SET locations = %s WHERE dedup_hash = %s",
        (
            dal.Json([{"url": _ASHBY + "?utm_medium=job-board", "work_mode": "remote"}]),
            legacy_hash,
        ),
    )
    _commit(dal)

    _sweep(monkeypatch, apply=True)
    rows = _raw_rows(dal)
    live = [r for r in rows if r["status"] != "archived"]
    assert len(live) == 1, [(r["title"], r["status"]) for r in rows]


# ---------------------------------------------------------------------------
# One careers-page req, three sources: trailing slash + an "X or Y" dual title
# ---------------------------------------------------------------------------
#
# Production regression (run 2026-08-24): one COO opening was stored THREE
# times — the direct fetch, one board that spelled the acronym out and dropped
# the URL's trailing slash, and one board that wrote the dual title with "or".
# The careers-page URL carries no ATS requisition id, so the req-key path never
# fires and the same-URL path is the only cross-source anchor; a trailing slash
# and a connective "or" were enough to break it.

_COO_SLASH = "https://www.northlight.test/careers/chief-operating-officer/"
_COO_BARE = "https://www.northlight.test/careers/chief-operating-officer"


def test_trailing_slash_is_not_a_second_req(dal):
    n = dal.normalize_apply_url
    assert n(_COO_SLASH) == n(_COO_BARE)
    assert n(_COO_SLASH + "?utm_source=x") == n(_COO_BARE)
    assert n("https://acme.test/") == n("https://acme.test")


def test_titles_equal_sans_stopwords_tolerates_or(dal):
    strong = dal._normalize_title_strong
    eq = dal._titles_equal_sans_stopwords
    assert eq(strong("COO / Director of Operations"), strong("COO or Director of Operations"))
    assert not eq(strong("Director of Finance"), strong("Director or Head of Programs"))


def test_one_req_listed_by_three_sources_stays_one_row(dal):
    dal.ensure_company("Northlight Foundation", status="active")
    # Direct fetch: full body, trailing-slash URL.
    dal.save_vacancies(
        "Northlight Foundation", "A", [_job("COO / Director of Operations", url=_COO_SLASH)]
    )
    _commit(dal)

    # Board 1 spells the acronym out and links the URL without the slash.
    dal.save_board_vacancies(
        {"name": "80,000 Hours", "url": "https://80k.test"},
        [
            {
                **_job(
                    "Chief Operating Officer / Director of Operations",
                    url=_COO_BARE,
                    desc="One board's own short stub for the same role. " * 6,
                ),
                "org_override": "Northlight Foundation",
            }
        ],
    )
    _commit(dal)

    # Board 2 writes the same dual title with "or" instead of the slash.
    dal.save_board_vacancies(
        {"name": "Probably Good", "url": "https://probablygood.test"},
        [
            {
                **_job(
                    "COO or Director of Operations",
                    url=_COO_SLASH,
                    desc="Another board's short stub for the same role. " * 6,
                ),
                "org_override": "Northlight Foundation",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_trailing_slash_never_merges_different_roles(dal):
    """The slash fold must not turn a shared generic careers URL into a merge
    licence: two genuinely different roles stay two rows."""
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies(
        "Acme Fund", "A", [_job("Director of Finance", url="https://acme.test/jobs")]
    )
    _commit(dal)
    dal.save_vacancies(
        "Acme Fund", "A", [_job("Director of Programs", url="https://acme.test/jobs/")]
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 2, [r["title"] for r in rows]
