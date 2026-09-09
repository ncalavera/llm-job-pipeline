"""Cross-source dedup identity: what makes two rows the SAME vacancy or company.

Covers the three identity anchors used before title/description heuristics
ever run: the ATS requisition id embedded in the apply URL, the normalized
apply URL itself (tracking params / connective words / trailing slash), org
name whitespace, and company name variants (aliases, acronym forms). Absorbs:

  * tests/test_dedup_req_key.py
  * tests/test_dedup_url_normalization.py
  * tests/test_dedup_org_whitespace.py
  * tests/test_company_merge_dedup.py

Same SQLite harness throughout. Orgs invented except in the company-merge
matcher tests, which use real org names because the matcher's behavior is
defined against them in the bug report.
"""

import importlib
import sys

import pytest

from company_registry import company_name_variants_match


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


# ===========================================================================
# --- from test_dedup_req_key.py ---
# ===========================================================================
#
# Production regression (2026-08-19): the SAME Ashby requisition at one org
# reached the DB twice — Probably Good stored the apply link with its utm
# decoration GLUED onto the ashby_jid value with no separator
# (".../careers?ashby_jid=<uuid>utm_source=PG_board"), so the normalized-URL
# merge read the two boards' links as two different reqs. A user had already
# applied through one row while the other sat in the browse queue. A second
# same-day pair ("Director of Community Growth" vs "Director, Community Growth")
# slipped through the same crack. Earlier same-family escapes: J-PAL / WFP / FHI
# rows forked a body-salted sibling even though the existing row already carried
# the SAME apply URL (the fork branch never consulted URLs).
#
# Every observed dup shares one root: identity was derived from strings the
# boards mangle (org spelling, title punctuation, URL decoration, body chrome).
# The requisition id inside the apply URL survives every observed mangle.
#
# Contract under test:
#
#   * extract_req_key() reads the requisition id out of known ATS URL shapes,
#     including Probably Good's corrupted glued form;
#   * normalize_apply_url() salvages a glued utm tail;
#   * the save path folds a same-req candidate onto the existing row even when
#     title wording, URL decoration, and body chrome all differ;
#   * the exact-hash fork branch folds (never forks a sibling) when the
#     candidate's req key is already on the existing row;
#   * a source that stamps ONE url onto several different roles never collapses
#     them (title-overlap / body guard);
#   * dedup_sweep clusters a same-req pair and auto-collapses it even when both
#     rows are live.

_UUID = "97c6c334-bd3d-4fc3-a3f9-3d3928c343d1"
_CLEAN = f"https://www.acme-institute.org/careers?ashby_jid={_UUID}"
_DECORATED = _CLEAN + "&utm_source=80000hours&utm_medium=job-board"
_GLUED = f"https://www.acme-institute.org/careers?ashby_jid={_UUID}utm_source=PG_board"


def _job(title, *, url, desc):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc,
        "location": "Remote",
        "url": url,
    }


def _full_desc(seed):
    return (f"We are hiring: {seed}. You will own strategy and execution. " * 12) + (
        "Salary band published. Apply via the careers page."
    )


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, dedup_hash, title, status, locations FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure extraction contract
# ---------------------------------------------------------------------------


def test_extracts_ashby_req_from_clean_decorated_and_glued(dal):
    k = dal.extract_req_key
    assert k(_CLEAN) == f"ashby:{_UUID}"
    assert k(_DECORATED) == f"ashby:{_UUID}"
    assert k(_GLUED) == f"ashby:{_UUID}"
    assert k(f"https://jobs.ashbyhq.com/acme/{_UUID}") == f"ashby:{_UUID}"


def test_extracts_known_ats_families(dal):
    k = dal.extract_req_key
    assert k("https://job-boards.eu.greenhouse.io/acme/jobs/4757233101") == "greenhouse:4757233101"
    assert k("https://acme.com/careers?gh_jid=5302779008") == "greenhouse:5302779008"
    assert (
        k(
            "https://wfp.wd3.myworkdayjobs.com/job_openings/job/Cairo-Egypt/"
            "Supply-Chain-Expert--SSA--L10-_JR123456"
        )
        == "workday:jr123456"
    )
    assert k(f"https://jobs.lever.co/acme/{_UUID}") == f"lever:{_UUID}"
    assert (
        k("https://jobs.smartrecruiters.com/Acme/744000123471150-product-lead")
        == "smartrecruiters:744000123471150"
    )
    assert (
        k("https://uk.linkedin.com/jobs/view/head-of-change-at-acme-4436301661")
        == "linkedin:4436301661"
    )
    assert k(f"https://acme.pinpointhq.com/en/postings/{_UUID}") == f"pinpoint:{_UUID}"
    assert (
        k("https://www.idealist.org/en/nonprofit-job/a67db9e8e72e4547a97247e00fbe6feb-x")
        == "idealist:a67db9e8e72e4547a97247e00fbe6feb"
    )


def test_generic_uuid_fallback_and_no_key(dal):
    k = dal.extract_req_key
    assert k(f"https://careers.acme.org/postings/{_UUID}") == f"uuid:{_UUID}"
    assert k("https://acme.org/careers") is None
    assert k("") is None
    assert k(None) is None


def test_normalize_apply_url_salvages_glued_utm(dal):
    assert dal.normalize_apply_url(_GLUED) == _CLEAN.replace("https://www.", "https://www.")
    assert dal.normalize_apply_url(_GLUED) == dal.normalize_apply_url(_DECORATED)


# ---------------------------------------------------------------------------
# Save path: same req = one row
# ---------------------------------------------------------------------------


def test_same_req_folds_across_retitle_decoration_and_chrome(dal):
    """The CEA Director pair: existing PG row with a corrupted URL and board
    chrome in the body; the 80k copy arrives retitled and decorated."""
    body = _full_desc("community growth leadership")
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Director of Community Growth", url=_GLUED, desc=body + "\n\nSkills: x | y")],
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Director, Community Growth", url=_DECORATED, desc=body + " Board summary.")],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1


def test_fork_branch_folds_on_shared_req_key(dal):
    """The J-PAL / WFP escape: same org+title, comparably sized but DIFFERENT
    bodies, same requisition URL — must fold, not fork a salted sibling."""
    dal.save_vacancies(
        "Acme Institute", "A", [_job("Product Lead", url=_GLUED, desc=_full_desc("v1 chrome"))]
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Product Lead", url=_DECORATED, desc=_full_desc("v2 entirely different wording"))],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1


def test_stamped_shared_url_keeps_distinct_roles_apart(dal):
    """The Taptap Send case: a board stamps ONE apply URL onto several of a
    company's genuinely different roles — they must stay separate rows."""
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("General Manager", url=_CLEAN, desc=_full_desc("run the company"))],
    )
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Head of East Africa", url=_CLEAN, desc=_full_desc("regional expansion work"))],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 2


def test_req_fold_inherits_user_decision(dal):
    """The applied-job regression: the surviving row keeps the decision."""
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [_job("Product Lead, Opportunities Board", url=_GLUED, desc=_full_desc("the board"))],
    )
    dal.get_conn().commit()
    row = _raw_rows(dal)[0]
    dal.update_vacancy_status(row["id"], "applied")
    dal.get_conn().commit()
    dal.save_vacancies(
        "Acme Institute",
        "A",
        [
            _job(
                "Product Lead, Opportunities Board",
                url=_DECORATED,
                desc=_full_desc("board copy, other chrome"),
            )
        ],
    )
    dal.get_conn().commit()
    rows = _raw_rows(dal)
    assert len(rows) == 1
    assert rows[0]["status"] == "applied"


# ---------------------------------------------------------------------------
# Sweep: same req clusters and auto-collapses even when both rows are live
# ---------------------------------------------------------------------------


def test_sweep_clusters_and_collapses_live_same_req_pair(dal):
    import dedup_sweep as ds

    body = _full_desc("one requisition, two boards")
    dal.save_vacancies(
        "Acme Institute", "A", [_job("Director of Community Growth", url=_GLUED, desc=body)]
    )
    dal.get_conn().commit()
    # Force the second variant in as its own row (bypassing the save-path fix)
    # to model the pre-fix prod state.
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (id, dedup_hash, company_id, title, full_description, "
        "locations, status, first_seen, last_seen) "
        "SELECT 'dup-row-1', 'ffffffffffffffff', company_id, "
        "'Director, Community Growth', ?, ?, 'unseen', first_seen, last_seen "
        "FROM vacancy LIMIT 1",
        (body + " Other chrome.", '[{"url": "' + _DECORATED.replace("&", "&") + '"}]'),
    )
    dal.get_conn().commit()
    clusters = ds._cluster(ds._load_rows())
    assert len(clusters) == 1
    assert not ds._needs_manual_review(clusters[0])


# ===========================================================================
# --- from test_dedup_url_normalization.py ---
# ===========================================================================
#
# Production regression (run 2026-08-04): the SAME Ashby requisition reached
# the DB twice per role because two boards decorate the link differently —
#
#   * 80,000 Hours board:  .../job-id?utm_source=...&utm_medium=job-board
#   * Probably Good board: .../job-id            (bare)
#
# — and the byte-wise URL guard read those as two distinct reqs (3 pairs). A
# fourth pair ("Director of GLP-1 in India Fund" vs "Director, GLP-1 in India
# Fund", identical URL) forked because the same-URL merge requires title
# containment and "of" broke it. Two J-PAL pairs with IDENTICAL URLs sat in
# MANUAL REVIEW because both rows were live.
#
# Contract under test:
#
#   * normalize_apply_url() strips utm_* / tracking params and the fragment,
#     keeps job-identifying params, lowercases scheme+host;
#   * the save path folds a board variant whose URL differs only by tracking
#     params onto the existing row;
#   * the same-URL merge tolerates connective-word retitles ("Director of X" ==
#     "Director, X") but never merges different roles on a shared generic URL;
#   * dedup_sweep clusters same-URL rows even when title keys and description
#     fingerprints both miss, and auto-collapses a both-live same-URL pair.

_ASHBY = "https://jobs.ashbyhq.com/acme/9576d650-a615-46c9-9187-9610e420a4a3"
_ASHBY_UTM = _ASHBY + "?utm_source=zbZ8a2qqDv&utm_source=80000hours&utm_medium=job-board"


def _job_u(title, *, url, desc=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc
        or (f"We are hiring a {title}. " * 12 + "Own the work end to end."),
        "location": "Remote",
        "url": url,
    }


def _raw_rows_u(db):
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
    dal.save_vacancies("Acme Fund", "A", [_job_u("Headhunting Product Specialist", url=_ASHBY_UTM)])
    _commit(dal)

    # Another board ships the SAME req: bare URL, suffixed title, its own stub body.
    dal.save_board_vacancies(
        {"name": "Probably Good", "url": "https://probablygood.test"},
        [
            {
                **_job_u(
                    "Headhunting Product Specialist, Career Services",
                    url=_ASHBY,
                    desc="Short board stub for the same role." * 3,
                ),
                "org_override": "Acme Fund",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows_u(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_connective_word_retitle_same_url_folds(dal):
    dal.ensure_company("Acme Fund", status="active")
    url = "https://acme.test/careers/director-glp-1-in-india-fund"
    dal.save_vacancies("Acme Fund", "A", [_job_u("Director of GLP-1 in India Fund", url=url)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Probably Good", "url": "https://probablygood.test"},
        [
            {
                **_job_u(
                    "Director, GLP-1 in India Fund",
                    url=url,
                    desc="A board's own different summary of the same role." * 6,
                ),
                "org_override": "Acme Fund",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows_u(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_generic_careers_url_never_merges_different_roles(dal):
    dal.ensure_company("Acme Fund", status="active")
    generic = "https://acme.test/careers"
    dal.save_vacancies("Acme Fund", "A", [_job_u("Director of Finance", url=generic)])
    _commit(dal)
    dal.save_vacancies("Acme Fund", "A", [_job_u("Director of Programs", url=generic)])
    _commit(dal)

    rows = _raw_rows_u(dal)
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
    dal.save_vacancies("Acme Fund", "A", [_job_u("Talent Database Lead", url=_ASHBY_UTM)])
    _commit(dal)
    # Inject the second row for the same req BEHIND the save path (simulates a
    # row stored before URL normalization existed): fork it as a genuinely
    # different role first, then repoint its URL at the shared req.
    dal.save_vacancies(
        "Acme Fund",
        "A",
        [
            _job_u(
                "Talent Database Lead, Career Services",
                url="https://elsewhere.test/other-req",
                desc="Completely different stub body from the other board." * 6,
            )
        ],
    )
    _commit(dal)
    rows = _raw_rows_u(dal)
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
    rows = _raw_rows_u(dal)
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
        "Northlight Foundation", "A", [_job_u("COO / Director of Operations", url=_COO_SLASH)]
    )
    _commit(dal)

    # Board 1 spells the acronym out and links the URL without the slash.
    dal.save_board_vacancies(
        {"name": "80,000 Hours", "url": "https://80k.test"},
        [
            {
                **_job_u(
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
                **_job_u(
                    "COO or Director of Operations",
                    url=_COO_SLASH,
                    desc="Another board's short stub for the same role. " * 6,
                ),
                "org_override": "Northlight Foundation",
            }
        ],
    )
    _commit(dal)

    rows = _raw_rows_u(dal)
    assert len(rows) == 1, [r["title"] for r in rows]


def test_trailing_slash_never_merges_different_roles(dal):
    """The slash fold must not turn a shared generic careers URL into a merge
    licence: two genuinely different roles stay two rows."""
    dal.ensure_company("Acme Fund", status="active")
    dal.save_vacancies(
        "Acme Fund", "A", [_job_u("Director of Finance", url="https://acme.test/jobs")]
    )
    _commit(dal)
    dal.save_vacancies(
        "Acme Fund", "A", [_job_u("Director of Programs", url="https://acme.test/jobs/")]
    )
    _commit(dal)

    rows = _raw_rows_u(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


# ===========================================================================
# --- from test_dedup_org_whitespace.py ---
# ===========================================================================
#
# Cross-source company dedup: a whitespace-mangled org string must still
# resolve to the SAME company as the clean one.
#
# Vacancy dedup (``make_vacancy_id``) is scoped by ``org|title``: the exact-hash
# lookup in ``_find_existing_vacancy`` is a global query, so two saves for the
# same org+title only collapse to one row when both paths resolve the org to the
# BYTE-IDENTICAL canonical string. ``company_registry.resolve_canonical_name``
# never stripped its input, so a source whose org field carries incidental
# whitespace (e.g. ``scripts/fetchers/boards/algolia.py``'s ``company_name``,
# unlike its sibling fetchers which all ``.strip()``) fails every lookup stage,
# falls through to passthrough, and ``ensure_company`` forks a SECOND company row
# for the same real-world org. Every vacancy for that org then duplicates: one
# row per company row, both with the same title/score/age, exactly the
# dashboard symptom (the same posting rendered as two identical cards).
#
# This is a same-company, whitespace-only match — orthogonal to the separate
# vacancy-TITLE-side norm/desc/URL guards that decide whether two same-company
# postings are a rename or distinct sibling roles. Fixing org resolution does
# not touch title matching at all.


def _job_w(title, *, org=None, city="San Francisco, USA", url=None):
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
    dal.save_vacancies("Coefficient Giving", "B", [_job_w(title)])
    dal.get_conn().commit()

    # Path 2: job board fetch — org string carries a leading space, exactly the
    # shape algolia.py (80,000 Hours board) produces since it never strips
    # hit.get("company_name").
    board_cfg = {"name": "80000 Hours", "url": "https://board.test/feed", "tier": "B"}
    dal.save_board_vacancies(board_cfg, [_job_w(title, org=" Coefficient Giving")])
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


# ===========================================================================
# --- from test_company_merge_dedup.py ---
# ===========================================================================
#
# Company-level dedup: a board-sourced NAME VARIANT of a company we already
# track must MERGE into that row, not fork a new candidate.
#
# Two layers:
#
# 1. ``company_name_variants_match`` — the pure tolerance matcher. Parametrized
#    against the run-log true positives (must merge) and the hand-caught false
#    positives (must NOT merge). Precision-biased: normalized-token equality or
#    an "ACRONYM - Full Name" containment, nothing looser.
#
# 2. ``ensure_company`` merge behavior — SQLite backend, isolated temp DB. An
#    existing ACTIVE, already-WANT-scored company plus a board save carrying its
#    long-form variant must leave ONE company row (no new candidate), fold the
#    variant into ``aliases``, attach the vacancy to the canonical id, and NOT
#    touch the existing status / score (no re-enrichment).
#
# Real org names are used because the matcher's behavior is defined against
# them in the bug report.

# ---------------------------------------------------------------------------
# 1. Pure matcher — must-match / must-not-match quality bar
# ---------------------------------------------------------------------------

MUST_MATCH = [
    (
        "EBRD - European Bank for Reconstruction and Development",
        "european bank for reconstruction and development (ebrd)",
    ),
    ("Save the Children International", "Save the Children"),
    (
        "IFAD - International Fund for Agricultural Development",
        "International Fund for Agricultural Development",
    ),
    ("Code.X 0", "Code.X"),
    ("Resolution", "Resolution Foundation"),
    # Accent folding: NFKD-normalized names must dedup.
    ("Médecins Sans Frontières", "Medecins Sans Frontieres"),
]

MUST_NOT_MATCH = [
    ("Henley & Partners", "Global Partners"),
    ("Via", "[via Fast Forward]"),
    ("Apple", "Apple CSR"),
    ("Imperial College London", "Imperial College London, National Heart and Lung Institute"),
    # Weak generic-token overlap must not merge (prod audit, 2026-07-06).
    ("Frontier Institute of Technology", "Massachusetts Institute of Technology"),
    ("Social Change Lab", "Change.org"),
    # Short/generic existing names as a substring/token of a longer one.
    ("Front", "Frontier Institute of Technology"),
    ("Merge", "Merge Labs"),
    # A distinct joint centre overlaps two DIFFERENT parents — auto-merge into
    # neither (multi-parent ambiguity).
    (
        "Cambridge University, Leverhulme Centre for the Future of Intelligence",
        "University of Cambridge",
    ),
    (
        "Cambridge University, Leverhulme Centre for the Future of Intelligence",
        "Leverhulme Trust",
    ),
    # Anagram is NOT an acronym: in-order initials are FIA, not FAI.
    ("FAI - Fund International Agricultural", "Fund International Agricultural"),
    # A lone generic org-suffix token carries no identity — never merge on it.
    ("The Foundation", "Foundation Inc"),
]


@pytest.mark.parametrize("a,b", MUST_MATCH)
def test_variants_must_match(a, b):
    assert company_name_variants_match(a, b), f"{a!r} should merge into {b!r}"
    assert company_name_variants_match(b, a), "match must be symmetric"


@pytest.mark.parametrize("a,b", MUST_NOT_MATCH)
def test_variants_must_not_match(a, b):
    assert not company_name_variants_match(a, b), f"{a!r} must NOT merge into {b!r}"
    assert not company_name_variants_match(b, a), "non-match must be symmetric"


def test_unrelated_names_do_not_match():
    assert not company_name_variants_match("Open Philanthropy", "GiveWell")
    assert not company_name_variants_match("", "Anything")


# ---------------------------------------------------------------------------
# 2. Merge behavior in the save layer
# ---------------------------------------------------------------------------


def _seed_company(dal, canonical, *, status, aliases, score):
    """Insert an existing, already-WANT-scored company directly."""
    import json

    conn = dal.get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO company (canonical_name, status, aliases, alignment_score, enriched_at)
           VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING id""",
        (canonical, status, json.dumps(aliases), score),
    )
    cid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    return cid


def _job_c(title, *, org, city="San Francisco, USA"):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role with real responsibilities.",
        "full_description": f"We are hiring a {title}. " * 12 + "Own the work end to end.",
        "location": city,
        "org_override": org,
    }


def test_board_variant_merges_into_existing_active_company(dal):
    """A board that surfaces "EBRD - European Bank for Reconstruction and
    Development" must NOT create a second row for the existing active "EBRD":
    the variant folds into aliases, the vacancy attaches to the canonical id,
    and the existing status/score are untouched (no re-enrichment)."""
    existing_id = _seed_company(
        dal,
        "EBRD",
        status="active",
        aliases=["european bank for reconstruction and development (ebrd)"],
        score=71,
    )

    board_cfg = {"name": "Impactpool", "url": "https://board.test/feed", "tier": "A"}
    variant = "EBRD - European Bank for Reconstruction and Development"
    dal.save_board_vacancies(board_cfg, [_job_c("Principal Economist", org=variant)])
    dal.get_conn().commit()

    cur = dal.get_conn().cursor()

    # Exactly one company row — no duplicate candidate.
    cur.execute("SELECT COUNT(*) FROM company")
    (n_companies,) = cur.fetchone()
    assert n_companies == 1, f"expected 1 company, got {n_companies} — variant forked a duplicate"

    # Existing row unchanged (status + score preserved, i.e. not re-scored).
    cur.execute("SELECT id, status, alignment_score, aliases FROM company")
    cid, status, score, aliases = cur.fetchone()
    assert cid == existing_id
    assert status == "active"
    assert score == 71
    # Variant folded into aliases.
    assert variant in aliases, f"variant not folded into aliases: {aliases}"

    # Vacancy attached to the canonical company id.
    cur.execute("SELECT company_id FROM vacancy")
    vac_rows = cur.fetchall()
    assert len(vac_rows) == 1
    assert vac_rows[0][0] == existing_id
    cur.close()


def test_new_org_still_creates_candidate(dal):
    """A genuinely new org (no existing match) still lands as one candidate —
    the merge path must not swallow legitimately new companies."""
    board_cfg = {"name": "Impactpool", "url": "https://board.test/feed", "tier": "A"}
    dal.save_board_vacancies(board_cfg, [_job_c("Data Scientist", org="Wholly Novel Org")])
    dal.get_conn().commit()

    cur = dal.get_conn().cursor()
    cur.execute("SELECT canonical_name, status FROM company")
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Wholly Novel Org"
    assert rows[0][1] == "candidate"
    cur.close()
