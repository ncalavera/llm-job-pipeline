"""Cross-board dedup on board-prefixed retitles and reordered titles.

Production regression (run 2026-08-04): the SAME program reached the DB twice
because two boards list it under different titles AND different URLs —

  * 80,000 Hours: "Charity Entrepreneurship Incubation Program", linking the
    org's own page, with a short summary stub (912 chars);
  * Idealist: "Non-profit Entrepreneur — Charity Entrepreneurship Incubation
    Program", linking Idealist's OWN posting page, with the full JD (13k chars).

No existing key matched: the title keys differ (board-added label segment),
the description fingerprints differ (stub vs full JD), and the URL key differs
(each board links its own page). A third, February copy of the same program
("Incubation Program, Charity Entrepreneurship") shared the normalized URL but
missed the same-URL merge because token ORDER broke both containment and the
stopword fold.

Contract under test:

  * _title_segment_keys() keys every dash/comma segment of a title that is
    substantial on its own (>= 3 significant words), org-scoped;
  * the save path folds a board copy whose full title equals a segment of a
    stored title (and vice versa) — the body guard, not the apply URL, decides
    distinctness; segment-vs-segment never matches;
  * _titles_equal_sans_stopwords() is word-order insensitive, so the same-URL
    merge folds reordered retitles of one req;
  * dedup_sweep clusters a board-prefix pair and auto-collapses it even when
    both rows are live, the survivor keeping the most-decided status.

Same SQLite harness as tests/test_dedup_url_normalization.py. Orgs invented.
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


_BARE = "Charity Kitchen Fellowship Program"
_PREFIXED = "Non-profit Entrepreneur — Charity Kitchen Fellowship Program"
_ORG_URL = "https://charitykitchen.test/fellowship-program"
_BOARD_URL = "https://board.test/nonprofit-job/abc123-charity-kitchen-fellowship"

_STUB = "This is a two-month training program to launch a high-impact project. " * 4
_FULL_JD = (
    "TLDR: Serious about impact? We help you start a high-impact project from "
    "scratch: strategy, fundraising, operations and evaluation, with seed "
    "funding and mentorship from experienced advisors across the whole cycle. "
) * 8


def _job(title, *, url, desc):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc,
        "location": "Remote",
        "url": url,
    }


def _board_job(title, *, url, desc, org):
    return {**_job(title, url=url, desc=desc), "org_override": org}


def _raw_rows(db):
    cur = db.get_conn().cursor(cursor_factory=db.RealDictCursor)
    cur.execute("SELECT id, dedup_hash, title, status FROM vacancy")
    rows = cur.fetchall()
    cur.close()
    return rows


# ---------------------------------------------------------------------------
# Pure contracts: segment keys and order-insensitive stopword fold
# ---------------------------------------------------------------------------


def test_segment_keys_for_substantial_segments_only(dal):
    keys = dal._title_segment_keys("Acme Org", _PREFIXED)
    assert dal.make_normalized_id("Acme Org", _BARE) in keys

    # Short segments ("Climate", "Ops Lead") never produce keys.
    assert dal._title_segment_keys("Acme Org", "Program Officer, Climate") == []
    # An unsegmented title produces no keys (that is the ordinary norm key).
    assert dal._title_segment_keys("Acme Org", "Director of Finance") == []


def test_segment_keys_are_org_scoped(dal):
    a = dal._title_segment_keys("Acme Org", _PREFIXED)
    b = dal._title_segment_keys("Other Org", _PREFIXED)
    assert a and b and set(a).isdisjoint(b)


def test_titles_equal_sans_stopwords_ignores_word_order(dal):
    strong = dal._normalize_title_strong
    eq = dal._titles_equal_sans_stopwords
    assert eq(
        strong("Fellowship Program, Charity Kitchen"),
        strong("Charity Kitchen Fellowship Program"),
    )
    assert not eq(strong("Director of Finance"), strong("Director of Programs"))


# ---------------------------------------------------------------------------
# Save path: a board-prefixed copy folds onto the stored row (either order)
# ---------------------------------------------------------------------------


def test_prefixed_board_copy_folds_onto_bare_row(dal):
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_vacancies("Charity Kitchen", "A", [_job(_BARE, url=_ORG_URL, desc=_STUB)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [_board_job(_PREFIXED, url=_BOARD_URL, desc=_FULL_JD, org="Charity Kitchen")],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 1, [r["title"] for r in rows]
    assert rows[0]["title"] == _BARE


def test_bare_copy_folds_onto_prefixed_row(dal):
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [_board_job(_PREFIXED, url=_BOARD_URL, desc=_FULL_JD, org="Charity Kitchen")],
    )
    _commit(dal)

    dal.save_vacancies("Charity Kitchen", "A", [_job(_BARE, url=_ORG_URL, desc=_STUB)])
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 1, [r["title"] for r in rows]
    assert rows[0]["title"] == _PREFIXED


def test_two_full_different_bodies_stay_two_roles(dal):
    """The body guard: a segment match backed by two full, comparably sized but
    DIFFERENT bodies is a second genuine role, not a board copy."""
    dal.ensure_company("Charity Kitchen", status="active")
    other_full = (
        "As the coordinator you will own logistics, partner comms and the "
        "weekly cohort rhythm; you will run selection interviews and demo days "
        "and keep the alumni community engaged after each cohort graduates. "
    ) * 8
    dal.save_vacancies("Charity Kitchen", "A", [_job(_BARE, url=_ORG_URL, desc=_FULL_JD)])
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Coordinator — Charity Kitchen Fellowship Program",
                url=_BOARD_URL,
                desc=other_full,
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


def test_segment_vs_segment_never_merges(dal):
    """Two roles decorated with the same program name are two roles."""
    dal.ensure_company("Charity Kitchen", status="active")
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job("Ops Lead — Charity Kitchen Fellowship Program", url=_ORG_URL, desc=_STUB)],
    )
    _commit(dal)

    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Research Lead — Charity Kitchen Fellowship Program",
                url=_BOARD_URL,
                desc=_STUB + " Research flavor.",
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)

    rows = _raw_rows(dal)
    assert len(rows) == 2, [r["title"] for r in rows]


# ---------------------------------------------------------------------------
# Sweep: the production trio collapses onto the decided row
# ---------------------------------------------------------------------------


def _sweep(monkeypatch, apply=False):
    import dedup_sweep

    importlib.reload(dedup_sweep)
    argv = ["dedup_sweep.py"] + (["--apply"] if apply else [])
    monkeypatch.setattr(sys, "argv", argv)
    dedup_sweep.main()


def test_sweep_collapses_live_board_prefix_pair(dal, monkeypatch):
    """Reproduces run 2026-08-04: a bare stub copy (passed), a board-prefixed
    full copy (to_apply) — both live — plus an old archived reordered copy
    sharing the bare row's normalized URL. All three must collapse; the
    to_apply row survives."""
    dal.ensure_company("Charity Kitchen", status="active")
    # Insert as distinct reqs (distinct URLs + titles the save path won't fold
    # by itself once statuses are rewritten below simulate the legacy state).
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job("Fellowship Program, Charity Kitchen", url="https://old.test/req", desc=_STUB)],
    )
    _commit(dal)
    dal.save_vacancies(
        "Charity Kitchen",
        "A",
        [_job(_BARE, url="https://mid.test/req", desc=_STUB + " Bare copy.")],
    )
    _commit(dal)
    # The save path now folds the prefixed board copy, so inject it BEHIND the
    # save path (simulates a row stored before this fix existed): save it under
    # an unrelated title, then repoint the title at the prefixed form.
    dal.save_board_vacancies(
        {"name": "Board", "url": "https://board.test"},
        [
            _board_job(
                "Placeholder Independent Role",
                url=_BOARD_URL,
                desc=_FULL_JD,
                org="Charity Kitchen",
            )
        ],
    )
    _commit(dal)
    assert len(_raw_rows(dal)) == 3

    cur = dal.get_conn().cursor()
    reordered_hash = dal.make_vacancy_id("Charity Kitchen", "Fellowship Program, Charity Kitchen")
    bare_hash = dal.make_vacancy_id("Charity Kitchen", _BARE)
    placeholder_hash = dal.make_vacancy_id("Charity Kitchen", "Placeholder Independent Role")
    prefixed_hash = dal.make_vacancy_id("Charity Kitchen", _PREFIXED)
    cur.execute(
        "UPDATE vacancy SET title = %s, dedup_hash = %s WHERE dedup_hash = %s",
        (_PREFIXED, prefixed_hash, placeholder_hash),
    )
    cur.execute(
        "UPDATE vacancy SET status = 'archived', last_seen = '2026-03-26', "
        "locations = %s WHERE dedup_hash = %s",
        (
            dal.Json([{"url": _ORG_URL + "?utm_source=80k&utm_medium=job-board"}]),
            reordered_hash,
        ),
    )
    cur.execute(
        "UPDATE vacancy SET status = 'passed', locations = %s WHERE dedup_hash = %s",
        (dal.Json([{"url": _ORG_URL + "?utm_source=80000hours"}]), bare_hash),
    )
    cur.execute("UPDATE vacancy SET status = 'to_apply' WHERE dedup_hash = %s", (prefixed_hash,))
    _commit(dal)

    _sweep(monkeypatch, apply=True)

    rows = _raw_rows(dal)
    live = [r for r in rows if r["status"] != "archived"]
    assert len(live) == 1, [(r["title"], r["status"]) for r in rows]
    assert live[0]["status"] == "to_apply"
    assert live[0]["title"] == _PREFIXED
