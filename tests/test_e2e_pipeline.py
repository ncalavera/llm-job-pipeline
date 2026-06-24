"""End-to-end pipeline simulation on an isolated temp SQLite DB.

This is the FIRST run of the whole pipeline since the big refactor (config →
defaults.toml, geography → profile HARD_FILTERS, universal junk narrowed to
speculative-pipeline markers, scoring → script↔agent JSON seam). It walks the
documented flow — fetch → filter → score(contract) → report → like/dislike →
triage — across TWO simulated sessions, proving persistence across a fresh DB
connection on day two.

Everything is PLACE-AGNOSTIC and uses invented orgs ("Acme Foundation",
"Globex") and made-up vacancies, so it encodes no real company or geography.
No network, no real model: the scorer is replaced by a deterministic fake
payload built from the exact --local JSON contract.

The backend is forced to local SQLite (conftest already clears SUPABASE_DB_URL;
each test points JOBSEARCH_DB_PATH at its own temp file and reloads the
backend/registry/DAL chain).
"""

import importlib
import io
import json
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Fixtures — isolated temp SQLite DB + clean module chain
# ---------------------------------------------------------------------------

def _force_sqlite(monkeypatch, db_file):
    """Point the whole chain at a fresh temp SQLite file and reload it."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in ("database_supabase", "config", "company_registry",
                "db_conn", "db_backend"):
        sys.modules.pop(mod, None)
    import db_backend
    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "e2e must run on the SQLite backend"
    import database_supabase as db
    return db


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """Day-1 DAL bound to an isolated temp SQLite database."""
    db = _force_sqlite(monkeypatch, tmp_path / "jobsearch.db")
    yield db
    db.close_conn()


def _stub_report(monkeypatch):
    """Replace the dashboard module so --save doesn't write files."""
    monkeypatch.setitem(
        sys.modules, "report",
        types.SimpleNamespace(generate_dashboard=lambda *a, **k: None),
    )


# ---------------------------------------------------------------------------
# Fake board / ATS responses (made-up data, no real orgs)
# ---------------------------------------------------------------------------

def _job(title, *, city="Berlin, Germany", desc=None, url=None):
    """Build one fetcher-shaped job dict (what fetchers.py hands the DAL)."""
    return {
        "title": title,
        "snippet": f"{title} — short blurb.",
        "full_description": desc or (
            f"We are hiring a {title}. " * 12 + "Own the work end to end."
        ),
        "location": city,
        "url": url or ("https://example.test/job/"
                       + title.lower().replace(" ", "-")),
    }


# Day-1 listing for "Acme Foundation" (direct-ATS shaped). Two real roles, two
# universal-junk speculative entries that the filter must drop.
ACME_DAY1 = [
    _job("Volunteer Coordinator"),
    _job("Software Engineer", city="London, United Kingdom"),
    _job("Research Fellowship", city="Remote"),
    _job("Talent Pool — General Application", desc="Register your interest."),
    _job("Expression of Interest", desc="Tell us about yourself for future roles."),
]


# ---------------------------------------------------------------------------
# Scoring contract helpers — drive cmd_local / cmd_save in-process
# ---------------------------------------------------------------------------

def _run_local(monkeypatch, *, limit=None):
    """Invoke score_vacancies.cmd_local and capture the JSON it emits.

    Mirrors how the orchestrator calls `score_vacancies.py --local`: stdout
    carries pure JSON of the unscored vacancies for the (here faked) scorer.
    """
    import score_vacancies
    importlib.reload(score_vacancies)
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    args = types.SimpleNamespace(
        force=False, include_passed=False, no_candidates=False,
        limit=limit, offset=0,
    )
    try:
        score_vacancies.cmd_local(args)
    finally:
        monkeypatch.undo()  # restore stdout (and any other patches)
    return json.loads(buf.getvalue() or "[]")


def _fake_scores(payload, scorer):
    """Build a --save payload from the --local payload + a scorer fn.

    scorer(org, title) -> (score, summary). Maps member_ids straight through
    (the DAL identifies rows by UUID member_ids, NOT the local `id`).
    """
    out = []
    for item in payload:
        score, summary = scorer(item["org"], item["title"])
        out.append({
            "payload_kind": "vacancy",
            "member_ids": item["member_ids"],
            "org": item["org"],
            "title": item["title"],
            "score": score,
            "reasoning": f"Deterministic test score for {item['title']}.",
            "short_summary": summary,
            "hard_requirements": ["test requirement"],
            "tags": ["test"],
        })
    return out


def _run_save(monkeypatch, save_payload, *, archive=False):
    import score_vacancies
    importlib.reload(score_vacancies)
    _stub_report(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(save_payload)))
    args = types.SimpleNamespace(archive=archive)
    score_vacancies.cmd_save(args)


# A summary long enough to clear cmd_save's 200-char short-summary warning.
def _summary(title):
    return (f"This role, {title}, is a deterministic test fixture. " * 6)


# ===========================================================================
# DAY 1
# ===========================================================================

def test_day1_backend_select_and_schema(dal):
    """1. SQLite backend selected, DB auto-created, schema present."""
    import db_backend
    assert db_backend.IS_SQLITE
    cur = dal.get_conn().cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {r[0] for r in cur.fetchall()}
    cur.close()
    assert {"company", "vacancy", "archived_hash"} <= tables


def test_day1_fetch_creates_company_and_rows(dal):
    """2. A fake ATS response → company candidate + vacancies + dedup_hash."""
    dal.ensure_company("Acme Foundation", status="active")
    new = dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()
    # Two junk speculative entries are dropped at MERGE time (universal junk),
    # so only the 3 real roles are inserted.
    assert new == 3
    vacs = dal.load_vacancies()
    assert len(vacs) == 3
    for v in vacs.values():
        assert v["org"] == "Acme Foundation"
        assert v["dedup_hash"]  # non-empty hash set


def test_day1_filter_drops_junk_keeps_real_with_empty_profile(dal):
    """3. Universal junk dropped; real roles SURVIVE with an EMPTY profile.

    The two speculative entries never reach the DB (merge-time junk gate), so
    after a fetch the unscored pool is exactly the 3 real roles and the filter
    classifies all 3 as 'ready' (nothing personal drops out of the box).
    """
    import filter_vacancies
    importlib.reload(filter_vacancies)

    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()

    # Junk is gone before classification.
    titles = {v["title"] for v in dal.load_vacancies().values()}
    assert titles == {"Volunteer Coordinator", "Software Engineer",
                      "Research Fellowship"}

    cats = filter_vacancies.classify_vacancies()
    ready_titles = {v["title"] for _, v in cats["ready"]}
    assert ready_titles == {"Volunteer Coordinator", "Software Engineer",
                            "Research Fellowship"}
    assert cats["delete_blacklist"] == []
    assert cats["delete_geo"] == []


def test_day1_score_contract_and_save(dal, monkeypatch):
    """4. --local emits a well-formed payload; --save ingests it via member_ids.

    Builds a FAKE scores payload (no model), runs --save, asserts llm_score,
    llm_scored_at, llm_summary and unseen status land on the right rows.
    """
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()

    payload = _run_local(monkeypatch)
    assert len(payload) == 3
    # Contract: each entry carries the fields the scorer needs.
    db_ids = set(dal.load_vacancies().keys())
    for item in payload:
        assert item["payload_kind"] == "vacancy"
        assert item["org"] == "Acme Foundation"
        assert item["title"]
        assert item["system_prompt"]
        assert item["user_msg"]
        assert item["member_ids"]
        # member_ids are real DB UUIDs (NOT the local id used only for grouping).
        assert set(item["member_ids"]) <= db_ids

    def scorer(org, title):
        return (70 if title == "Volunteer Coordinator" else 45,
                _summary(title))

    save_payload = _fake_scores(payload, scorer)
    _run_save(monkeypatch, save_payload)

    vacs = dal.load_vacancies()
    for v in vacs.values():
        assert v["llm_score"] in (70, 45)
        assert v["llm_scored_at"]
        assert v["llm_summary"]
        assert v["status"] == "unseen"  # scoring alone doesn't change status


def test_day1_save_auto_archive_below_threshold(dal, monkeypatch):
    """4b. With --archive, unseen vacancies below threshold are archived."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", [
        _job("Keeper Role"), _job("Throwaway Role", city="London, United Kingdom"),
    ])
    dal.get_conn().commit()

    payload = _run_local(monkeypatch)

    threshold = dal.LLM_SCORE_THRESHOLD if hasattr(dal, "LLM_SCORE_THRESHOLD") else 20
    import config
    threshold = config.LLM_SCORE_THRESHOLD

    def scorer(org, title):
        return (80 if title == "Keeper Role" else max(threshold - 5, 0),
                _summary(title))

    _run_save(monkeypatch, _fake_scores(payload, scorer), archive=True)

    statuses = {v["title"]: v.get("status")
                for v in dal.load_vacancies(include_inactive_companies=True).values()}
    # Low-scoring unseen role archived (force=True path); keeper stays.
    assert statuses.get("Keeper Role") == "unseen"
    assert "Throwaway Role" not in {t for t, s in statuses.items() if s != "archived"}


def test_day1_report_builds_with_and_without_data(dal, monkeypatch):
    """5. data_prep builds the dashboard data from the DB, and on an EMPTY DB."""
    import report.data_prep as data_prep
    importlib.reload(data_prep)

    # Empty DB: must not crash and must yield zero groups.
    empty = data_prep.prepare_report_data()
    assert empty["groups"] == []
    assert empty["stats"]["total_roles"] == 0

    # With scored data the dashboard surfaces the scored roles.
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()
    payload = _run_local(monkeypatch)
    _run_save(monkeypatch, _fake_scores(payload, lambda o, t: (66, _summary(t))))

    data = data_prep.prepare_report_data()
    assert data["stats"]["total_roles"] == 3
    report_titles = {g["title"] for g in data["groups"]}
    assert report_titles == {"Volunteer Coordinator", "Software Engineer",
                             "Research Fellowship"}


def test_day1_like_dislike_persist(dal):
    """6. status=liked / passed via the DAL, asserted persisted."""
    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()
    ids = list(dal.load_vacancies().keys())
    dal.update_vacancy_status(ids[0], "liked")
    dal.update_vacancy_status(ids[1], "passed")
    dal.get_conn().commit()
    statuses = dal.get_vacancy_statuses()
    assert statuses[ids[0]] == "liked"
    assert statuses[ids[1]] == "passed"


def test_day1_triage_load_liked_and_persist_decision(dal):
    """7. triage.get_liked_vacancies + group_by_company, then a to_apply sticks."""
    import triage
    importlib.reload(triage)

    dal.ensure_company("Acme Foundation", status="active")
    dal.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    dal.get_conn().commit()
    ids = list(dal.load_vacancies().keys())
    liked_id = ids[0]
    dal.update_vacancy_status(liked_id, "liked")
    dal.get_conn().commit()

    liked = triage.get_liked_vacancies()
    assert len(liked) == 1
    assert liked[0]["id"] == liked_id

    groups = triage.group_by_company(liked)
    assert len(groups) == 1
    assert groups[0]["org"] == "Acme Foundation"
    assert len(groups[0]["vacancies"]) == 1

    # Persist a triage decision through the DAL helper.
    assert triage.update_status(liked_id, "to_apply") is True
    assert dal.get_vacancy_statuses()[liked_id] == "to_apply"


# ===========================================================================
# DAY 2 — repeat behaviour on a NEW connection (proves persistence)
# ===========================================================================

@pytest.fixture()
def day2(tmp_path, monkeypatch):
    """Seed a DB on day 1, close it, then hand back a builder that reopens the
    SAME file on a fresh connection (simulating a second session)."""
    db_file = tmp_path / "jobsearch.db"

    # --- Day 1 seed ---
    db = _force_sqlite(monkeypatch, db_file)
    db.ensure_company("Acme Foundation", status="active")
    db.save_vacancies("Acme Foundation", "A", ACME_DAY1)
    db.get_conn().commit()
    # Decide one role on day 1 so we can prove it survives the reconnect.
    ids = list(db.load_vacancies().keys())
    title_to_id = {db.load_vacancies()[i]["title"]: i for i in ids}
    liked_id = title_to_id["Volunteer Coordinator"]
    db.update_vacancy_status(liked_id, "liked")
    db.get_conn().commit()
    # Score every day-1 row directly through the DAL so the day-2 --local
    # payload only contains genuinely NEW (unscored) rows. (We score via the
    # DAL here rather than the cmd_local/cmd_save seam — that seam is exercised
    # in the day-1 tests; here we only need the rows marked scored.)
    for vid in ids:
        db.update_llm_score(vid, {
            "llm_score": 55,
            "llm_reasoning": "seed",
            "llm_summary": "seed summary " * 20,
            "llm_hard_requirements": [],
        })
    db.get_conn().commit()
    db.close_conn()

    state = {"db_file": db_file, "liked_id": liked_id,
             "title_to_id": dict(title_to_id)}

    def reopen():
        return _force_sqlite(monkeypatch, db_file)

    return state, reopen


def test_day2_refetch_dedup_new_and_gone(day2, monkeypatch):
    """8. Re-fetch: a repeat dedups, a new role inserts, a vanished role is
    archived by archive_gone_vacancies."""
    state, reopen = day2
    db = reopen()

    before = db.load_vacancies()
    assert len(before) == 3  # the 3 real day-1 roles persisted across reconnect

    # Day-2 listing OMITS "Research Fellowship", REPEATS the others, adds a NEW
    # "Programme Lead".
    day2_listing = [
        _job("Volunteer Coordinator"),                      # repeat → dedup
        _job("Software Engineer", city="London, United Kingdom"),  # repeat
        _job("Programme Lead"),                             # new → insert
    ]
    new = db.save_vacancies("Acme Foundation", "A", day2_listing)
    db.get_conn().commit()
    assert new == 1  # only the genuinely new role inserts; repeats dedup

    # Direct-ATS ground truth: the fellowship is gone from source → archive it.
    archived = db.archive_gone_vacancies("Acme Foundation", day2_listing)
    db.get_conn().commit()
    assert archived == 1

    live = {v["title"]: v["status"]
            for v in db.load_vacancies(include_inactive_companies=True).values()}
    assert live.get("Research Fellowship") == "archived"
    live_titles = {t for t, s in live.items() if s != "archived"}
    assert live_titles == {"Volunteer Coordinator", "Software Engineer",
                           "Programme Lead"}

    # Re-score only the unscored/new role.
    payload = _run_local(monkeypatch)
    new_titles = {p["title"] for p in payload}
    assert new_titles == {"Programme Lead"}
    db.close_conn()


def test_day2_status_decision_persist_across_reconnect(day2):
    """10. The day-1 liked/to_apply decision survives a fresh connection."""
    state, reopen = day2
    db = reopen()
    # Promote the day-1 liked row to to_apply, reconnect again, assert it sticks.
    db.update_vacancy_status(state["liked_id"], "to_apply")
    db.get_conn().commit()
    db.close_conn()

    db2 = reopen()
    statuses = db2.get_vacancy_statuses()
    assert statuses[state["liked_id"]] == "to_apply"
    db2.close_conn()


# ===========================================================================
# DAY 2 — profile change re-runs the filter under a temp HARD_FILTERS profile
# ===========================================================================

_RELOAD_CHAIN = ("settings", "geo", "prompts", "hard_filters", "config",
                 "database_supabase", "filter_vacancies")


def _reload_filter_chain():
    import settings
    settings.clear_cache()
    for mod in _RELOAD_CHAIN:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
        else:
            importlib.import_module(mod)
    import filter_vacancies
    return filter_vacancies


def _write_profile(path, *, countries="(none)", keywords="(none)"):
    path.write_text(
        "## HARD_FILTERS\n\n"
        f"exclude_countries: {countries}\n"
        f"exclude_title_keywords: {keywords}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def restore_chain():
    """Restore the import chain to the ambient config after the test.

    Teardown clears BOTH env overrides itself (it must not depend on the
    monkeypatch fixture's teardown order — that fixture may revert AFTER this
    one, leaving the temp TOML/profile live during our reload and corrupting the
    real geo map for the rest of the suite). After clearing, it reloads the chain
    so geo.py / config.py rebind to the real defaults.toml.
    """
    import os
    saved_profile = os.environ.get("USER_PROFILE_PATH")
    saved_toml = os.environ.get("DEFAULTS_TOML_PATH")
    yield
    for var, saved in (("USER_PROFILE_PATH", saved_profile),
                       ("DEFAULTS_TOML_PATH", saved_toml)):
        if saved is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = saved
    _reload_filter_chain()
    # geo.py caches the country/city maps at import; reload it explicitly so the
    # real maps are restored for the rest of the session.
    importlib.reload(sys.modules["geo"])
    importlib.reload(sys.modules["filter_vacancies"])


def test_day2_profile_change_drops_then_restores(dal, tmp_path, monkeypatch,
                                                 restore_chain):
    """9. exclude_countries + exclude_title_keywords drop matching rows; an
    empty profile drops nothing.

    Uses a fictional geo map so the test is place-agnostic: 'Country A' is the
    excluded country, the keyword 'engineer' is the excluded title word.
    """
    import os

    # Fictional geo map: pin our fake country/city to a neutral bucket. Carry
    # the universal-junk words too — pointing DEFAULTS_TOML_PATH at a temp file
    # replaces the whole config, so without a [junk] section UNIVERSAL_JUNK would
    # be empty and the "junk still drops" assertions below would be meaningless.
    geo_toml = tmp_path / "defaults.toml"
    geo_toml.write_text(
        "[junk]\n"
        "words = [\"talent pool\", \"expression of interest\", "
        "\"general application\"]\n"
        "substr = []\n"
        "desc_substr = []\n\n"
        "[geo.countries]\nother = [\"country a\"]\n\n"
        "[geo.cities]\nother = [\"city a\"]\n\n"
        "[geo.work_mode]\nremote = [\"remote\"]\nhybrid = [\"hybrid\"]\n",
        encoding="utf-8",
    )
    profile = tmp_path / "user_profile.md"
    monkeypatch.setenv("DEFAULTS_TOML_PATH", str(geo_toml))
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))

    country_a_vac = {"locations": [{"country": "Country A", "city": "City A"}]}
    germany_vac = {"locations": [{"country": "Country A", "city": "City A"},
                                 {"work_mode": "remote"}]}  # has a kept location
    eng_title = "Senior Software Engineer"
    other_title = "Head of Operations"

    # --- Profile WITH filters ---
    _write_profile(profile, countries="country a", keywords="engineer")
    fv = _reload_filter_chain()
    import config
    assert config.EXCLUDE_COUNTRIES == ["country a"]
    assert "engineer" in config.EXCLUDE_TITLE_KEYWORDS

    # filters was reloaded in place by the chain reload above; this reference
    # reflects the current (WITH-filters) profile.
    import filters
    # A vacancy located ONLY in Country A drops on geography.
    assert fv._all_locations_excluded(country_a_vac) is True
    assert fv._geo_delete_category(country_a_vac) == "delete_geo"
    # A multi-location posting that also has a kept location survives.
    assert fv._all_locations_excluded(germany_vac) is False
    # An 'engineer' TITLE drops; a non-matching title survives.
    assert filters.title_words_blacklisted(eng_title) is True
    assert filters.title_words_blacklisted(other_title) is False
    # Universal junk still drops regardless of profile.
    assert filters.title_words_blacklisted("Talent Pool — General Application") is True

    # --- Empty profile: nothing personal drops ---
    _write_profile(profile, countries="(none)", keywords="(none)")
    fv = _reload_filter_chain()
    import config as config2
    importlib.reload(config2)
    assert config2.EXCLUDE_COUNTRIES == []
    assert config2.EXCLUDE_TITLE_KEYWORDS == []

    import database_supabase as db_mod2
    importlib.reload(db_mod2)  # reloads filters in place under the empty profile
    import filters as filters2
    assert fv._all_locations_excluded(country_a_vac) is False
    assert fv._geo_delete_category(country_a_vac) is None
    assert filters2.title_words_blacklisted(eng_title) is False
    # Universal junk STILL drops with an empty profile.
    assert filters2.title_words_blacklisted("Expression of Interest") is True
