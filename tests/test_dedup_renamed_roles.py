"""Cross-variant vacancy dedup: renamed roles, re-punctuation, language copies.

The save path must treat these as ONE role at a company, not a new vacancy:
  * a seniority word added/removed in the title ("Product Manager, Geo
    Expansion" vs "Senior Product Manager, Geo Expansion");
  * punctuation / whitespace / case differences ("Innovation - Generative" vs
    "Innovation, Generative");
  * a same-company duplicate in another language whose description body matches.

It must also keep a user decision (applied / passed) on the surviving row, and a
repeat collection must never fork a copy. The exact dedup_hash formula stays
backward-compatible, so this is an ADDITIVE layer.

Backend is forced to local SQLite (conftest clears SUPABASE_DB_URL; each test
points JOBSEARCH_DB_PATH at its own temp file and reloads the DAL chain), same
harness as tests/test_sqlite_backend.py. All orgs/roles are invented.
"""

import hashlib
import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


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


def _job(title, *, city="Berlin, Germany", desc=None):
    return {
        "title": title,
        "snippet": f"{title} -- a genuine open role.",
        "full_description": desc
        or (f"We are hiring a {title}. " * 12 + "Own the work end to end."),
        "location": city,
        "url": "https://example.test/job/" + title.lower().replace(" ", "-"),
    }


def _rows(db):
    return db.load_vacancies(include_inactive_companies=True)


def _titles(db):
    return sorted(v["title"] for v in _rows(db).values())


def _set_status(db, dedup_hash, status):
    cur = db.get_conn().cursor()
    cur.execute("UPDATE vacancy SET status = %s WHERE dedup_hash = %s", (status, dedup_hash))
    db.get_conn().commit()
    cur.close()


# ---------------------------------------------------------------------------
# Pure normalization contract
# ---------------------------------------------------------------------------


def test_dedup_hash_formula_is_unchanged(dal):
    """make_vacancy_id() must still be md5(org|geo-normalized-title lower)[:16].

    Backward-compat guard: changing this silently would un-match every stored
    row and every tombstone (mass duplication / resurrection).
    """
    org, title = "Acme Foundation", "Ops Lead (Remote)"
    key = f"{org}|{dal._normalize_title_for_dedup(title)}".lower()
    assert dal.make_vacancy_id(org, title) == hashlib.md5(key.encode()).hexdigest()[:16]


def test_normalize_strips_level_words(dal):
    assert dal._normalize_title_strong("Senior Product Manager, Geo Expansion") == (
        dal._normalize_title_strong("Product Manager, Geo Expansion")
    )
    for lvl in ("Senior", "Sr.", "Lead", "Principal", "Junior", "Head of"):
        assert dal.make_normalized_id("Acme", f"{lvl} Data Engineer") == dal.make_normalized_id(
            "Acme", "Data Engineer"
        )


def test_normalize_ignores_punctuation_and_spacing(dal):
    assert dal.make_normalized_id("Acme", "Innovation - Generative") == dal.make_normalized_id(
        "Acme", "Innovation,  Generative"
    )


def test_normalized_key_is_company_scoped(dal):
    assert dal.make_normalized_id("Acme", "Ops Lead") != dal.make_normalized_id(
        "Globex", "Ops Lead"
    )


def test_description_fingerprint_short_body_is_none(dal):
    assert dal.description_fingerprint("too short to trust") is None
    long_body = "We build safe and useful systems for everyone, everywhere. " * 20
    assert len(long_body) > 1000
    fp = dal.description_fingerprint(long_body)
    assert fp is not None and fp == dal.description_fingerprint(long_body)


# ---------------------------------------------------------------------------
# 1. Renamed role (level word) -> one vacancy
# ---------------------------------------------------------------------------


def test_level_rename_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Product Manager, Geo Expansion")])
    _commit(dal)
    new = dal.save_vacancies(
        "Acme Foundation", "A", [_job("Senior Product Manager, Geo Expansion")]
    )
    _commit(dal)

    assert new == 0  # merged onto the existing role, not a new row
    assert len(_rows(dal)) == 1


# ---------------------------------------------------------------------------
# 2. Punctuation / whitespace / case -> one vacancy
# ---------------------------------------------------------------------------


def test_punctuation_variant_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Innovation - Generative")])
    _commit(dal)
    new = dal.save_vacancies("Acme Foundation", "A", [_job("Innovation,  Generative")])
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 1


# ---------------------------------------------------------------------------
# 3. Language duplicate by description body -> one vacancy
# ---------------------------------------------------------------------------


def test_language_duplicate_by_description_is_one_vacancy(dal):
    dal.ensure_company("Acme Foundation", status="active")
    # A realistic full job description: long AND role-specific, so the fingerprint
    # is a trustworthy same-role signal (short shared blurbs are ignored, see
    # _MIN_DESC_FP_CHARS).
    shared = (
        "We are building tools to advance safe and trustworthy AI for the public "
        "interest. You will lead applied research on evaluation and alignment, "
        "publish findings openly, and mentor a small team of engineers across "
        "several time zones. Responsibilities include designing experiments, "
        "shipping reproducible pipelines, and collaborating with policy staff. " * 4
    )
    assert len(shared) > 1000
    dal.save_vacancies("Acme Foundation", "A", [_job("Research Engineer", desc=shared)])
    _commit(dal)
    # Same body, a different-language title -> caught by description fingerprint.
    new = dal.save_vacancies("Acme Foundation", "A", [_job("Ingenieur de recherche", desc=shared)])
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 1


def test_distinct_roles_are_not_merged(dal):
    """Two genuinely different roles (different title, different body) stay two."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies(
        "Acme Foundation",
        "A",
        [_job("Data Engineer"), _job("Communications Officer")],
    )
    _commit(dal)
    assert _titles(dal) == ["Communications Officer", "Data Engineer"]


# ---------------------------------------------------------------------------
# 4. Status inheritance (applied / passed survive a rename)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decided", ["applied", "passed"])
def test_rename_inherits_decided_status(dal, decided):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Growth Marketing Manager")])
    _commit(dal)
    h = dal.make_vacancy_id("Acme Foundation", "Growth Marketing Manager")
    _set_status(dal, h, decided)

    new = dal.save_vacancies("Acme Foundation", "A", [_job("Senior Growth Marketing Manager")])
    _commit(dal)

    rows = _rows(dal)
    assert new == 0
    assert len(rows) == 1
    assert next(iter(rows.values()))["status"] == decided  # NOT reset to unseen


# ---------------------------------------------------------------------------
# 5. Idempotent repeat collection
# ---------------------------------------------------------------------------


def test_repeat_collection_is_idempotent(dal):
    dal.ensure_company("Acme Foundation", status="active")
    batch = [
        _job("Program Officer"),
        _job("Senior Program Officer"),  # same role, level-renamed within the batch
        _job("Software Engineer"),
    ]
    first = dal.save_vacancies("Acme Foundation", "A", batch)
    _commit(dal)
    second = dal.save_vacancies("Acme Foundation", "A", batch)
    _commit(dal)

    assert first == 2  # Program Officer + Software Engineer (Senior* folds in)
    assert second == 0
    assert len(_rows(dal)) == 2


# ---------------------------------------------------------------------------
# 6. archive_gone keeps a renamed-but-still-live role
# ---------------------------------------------------------------------------


def test_archive_gone_keeps_renamed_live_role(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [_job("Data Analyst")])
    _commit(dal)

    # The ATS now lists the SAME role under a renamed title.
    listing = [_job("Senior Data Analyst")]
    dal.save_vacancies("Acme Foundation", "A", listing)  # merges onto the row
    _commit(dal)
    archived = dal.archive_gone_vacancies("Acme Foundation", listing)
    _commit(dal)

    rows = _rows(dal)
    assert int(archived) == 0
    assert len(rows) == 1
    assert next(iter(rows.values()))["status"] == "unseen"  # kept alive, not archived


# ---------------------------------------------------------------------------
# 7. A tombstone suppresses a renamed variant (no resurrection loop)
# ---------------------------------------------------------------------------


def test_tombstone_suppresses_renamed_variant(dal):
    dal.ensure_company("Acme Foundation", status="active")
    dedup = dal.make_vacancy_id("Acme Foundation", "Growth Product Manager")
    norm = dal.make_normalized_id("Acme Foundation", "Growth Product Manager")
    dal.record_archived_hashes([(dedup, "score_below_threshold", norm)])
    _commit(dal)

    # A renamed variant of the buried role must not be re-imported.
    new = dal.save_vacancies("Acme Foundation", "A", [_job("Senior Growth Product Manager")])
    _commit(dal)

    assert new == 0
    assert len(_rows(dal)) == 0


# ---------------------------------------------------------------------------
# 8. dedup_sweep utility: dry-run reports, --apply collapses
# ---------------------------------------------------------------------------


def _seed_row(
    dal, company_id, title, *, status="unseen", score=None, desc=None, first_seen="2026-01-01"
):
    cur = dal.get_conn().cursor()
    cur.execute(
        "INSERT INTO vacancy (dedup_hash, company_id, title, full_description, status, "
        "llm_score, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            dal.make_vacancy_id("Acme Foundation", title),
            company_id,
            title,
            desc or "",
            status,
            score,
            first_seen,
            first_seen,
        ),
    )
    dal.get_conn().commit()
    cur.close()


def _seed_preexisting_dupes(dal):
    cid = dal.ensure_company("Acme Foundation", status="active")
    dal.get_conn().commit()
    # Level-rename pair; the decided (applied) row must survive.
    _seed_row(dal, cid, "Growth Product Manager", status="unseen", score=40)
    _seed_row(dal, cid, "Senior Growth Product Manager", status="applied", score=55)
    # Language pair sharing a (long, role-specific) description body.
    body = "We advance safe AI in the public interest and publish our research openly. " * 20
    _seed_row(dal, cid, "Research Scientist", status="unseen", desc=body)
    _seed_row(dal, cid, "Chercheur scientifique", status="liked", desc=body)
    # A standalone role that must be left untouched.
    _seed_row(dal, cid, "Office Coordinator", status="unseen")
    return cid


def test_dedup_sweep_dry_run_reports_but_does_not_write(dal, capsys, monkeypatch):
    _seed_preexisting_dupes(dal)
    before = len(_rows(dal))
    assert before == 5

    import dedup_sweep

    importlib.reload(dedup_sweep)
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py"])
    rc = dedup_sweep.main()
    out = capsys.readouterr().out

    assert rc == 0
    assert "DRY-RUN" in out
    assert "2 duplicate cluster(s)" in out
    assert "inherits 'applied'" in out
    # Nothing was written.
    assert len(_rows(dal)) == before


def test_dedup_sweep_apply_collapses_and_inherits_status(dal, monkeypatch):
    _seed_preexisting_dupes(dal)

    import dedup_sweep

    importlib.reload(dedup_sweep)
    monkeypatch.setattr(sys, "argv", ["dedup_sweep.py", "--apply"])
    rc = dedup_sweep.main()

    assert rc == 0
    rows = _rows(dal)
    # 5 rows -> 3 (two clusters collapse to one survivor each; standalone kept).
    assert len(rows) == 3
    by_status = {v["title"]: v["status"] for v in rows.values()}
    # Survivors carry the decided status of the cluster.
    assert "applied" in by_status.values()  # Growth Product Manager cluster
    assert "liked" in by_status.values()  # Research Scientist cluster
    assert "Office Coordinator" in by_status
