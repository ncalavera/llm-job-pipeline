"""Fetch-time dropping of roles the user's profile never wants stored.

Two profile rules decide this from company + title alone:

  * ``## COMPANY_TITLE_FILTERS`` — a per-company title INCLUDE-list.
  * ``## COMPANY_NEVER_FETCH`` — a whole-company ban.

Both used to bite only at the FILTER stage, after the roles were fetched,
stored and reported. WFP alone kept 195 stored rows that way, and one night's
run stored, excluded and digest-printed 51 more. These tests prove the drop now
happens BEFORE the save, and that a company named in neither list behaves
exactly as it did before.

The end-to-end tests run ``fetch_vacancies.main()`` on a throwaway SQLite DB
under a temporary profile — same harness as tests/test_fetch_vacancies_dispatch.py.
"""

import importlib
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = str((Path(__file__).resolve().parent.parent / "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import hard_filters as hf  # noqa: E402


# ---------------------------------------------------------------------------
# Parser unit tests — ## COMPANY_NEVER_FETCH
# ---------------------------------------------------------------------------


def test_parse_bullets_into_names():
    assert hf._parse_company_never_fetch("- WFP - World Food Programme\n* Some Agency\n") == [
        "WFP - World Food Programme",
        "Some Agency",
    ]


def test_parse_dedups_and_keeps_order():
    assert hf._parse_company_never_fetch("- A Co\n- B Co\n- A Co\n") == ["A Co", "B Co"]


def test_parse_ignores_html_comment_examples():
    body = "<!--\n- Example Org\n-->\n- Real Org\n"
    assert hf._parse_company_never_fetch(body) == ["Real Org"]


def test_malformed_entry_with_patterns_warns_and_is_skipped(capsys):
    """A line copied from the sibling section is skipped, loudly, not obeyed."""
    parsed = hf._parse_company_never_fetch("- Big NGO :: product, data\n- Real Org\n")
    assert parsed == ["Real Org"]
    err = capsys.readouterr().err
    assert "COMPANY_NEVER_FETCH" in err
    assert "Big NGO" in err
    assert "STILL fetched" in err


def test_text_without_bullets_warns_instead_of_silently_doing_nothing(capsys):
    """Names written without dashes would be a silently inactive ban list."""
    assert hf._parse_company_never_fetch("WFP - World Food Programme\nSome Agency\n") == []
    err = capsys.readouterr().err
    assert "COMPANY_NEVER_FETCH" in err
    assert "INACTIVE" in err


def test_empty_and_absent_section_are_off(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "USER_PROFILE_PATH",
        str(_write_profile(tmp_path / "a", never_fetch="")),
    )
    import prompts

    prompts.clear_profile_cache()
    assert hf.load_company_never_fetch() == []

    profile = tmp_path / "b" / "user_profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("## USER_PROFILE\n\nTest person.\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts.clear_profile_cache()
    assert hf.load_company_never_fetch() == []


def test_broken_profile_never_raises(monkeypatch):
    """A profile the loader cannot read must not take the pipeline down."""
    monkeypatch.setattr(
        hf, "_load_user_profile", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert hf.load_company_never_fetch() == []


# ---------------------------------------------------------------------------
# Temp-profile helpers (shared by the reason tests and the end-to-end runs)
# ---------------------------------------------------------------------------


def _write_profile(dir_path: Path, *, title_filters: str = "", never_fetch: str = "") -> Path:
    """Write a minimal profile with the two sections under test.

    Headers stay at column 0 — the profile parser requires that.
    """
    dir_path.mkdir(parents=True, exist_ok=True)
    profile = dir_path / "user_profile.md"
    profile.write_text(
        "## USER_PROFILE\n\nTest person.\n\n"
        f"## COMPANY_TITLE_FILTERS\n\n{title_filters}\n\n"
        f"## COMPANY_NEVER_FETCH\n\n{never_fetch}\n\n"
        "## OUTPUT_LANGUAGE\n\nEnglish\n",
        encoding="utf-8",
    )
    return profile


# Module chain that caches profile-derived constants at import time.
_CHAIN_PREFIXES = {
    "database_supabase",
    "config",
    "company_registry",
    "db_conn",
    "db_backend",
    "report",
    "fetchers",
    "fetch_vacancies",
    "run_status",
    "filters",
}


def _reset_chain(monkeypatch, db_file: Path, profile: Path):
    """Rebind the whole chain to a temp SQLite DB and a temp profile.

    Returns ``(db, saved_modules)``; the caller MUST restore in teardown.
    """
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))

    import prompts

    prompts.clear_profile_cache()
    importlib.reload(hf)

    saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] in _CHAIN_PREFIXES}
    for name in saved:
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "these tests must run on the SQLite backend"

    import database_supabase as db

    return db, saved


def _restore_chain(saved: dict) -> None:
    for name in list(sys.modules):
        if name.split(".")[0] in _CHAIN_PREFIXES:
            sys.modules.pop(name, None)
    sys.modules.update(saved)
    import prompts

    prompts.clear_profile_cache()
    importlib.reload(hf)


@pytest.fixture
def restore_profile_env():
    saved = os.environ.get("USER_PROFILE_PATH")
    yield
    if saved is None:
        os.environ.pop("USER_PROFILE_PATH", None)
    else:
        os.environ["USER_PROFILE_PATH"] = saved
    import prompts

    prompts.clear_profile_cache()
    importlib.reload(hf)


# ---------------------------------------------------------------------------
# The reason function the fetch path calls
# ---------------------------------------------------------------------------


def test_never_fetch_reason_bans_every_title(tmp_path, monkeypatch, restore_profile_env):
    profile = _write_profile(tmp_path / "p", never_fetch="- Trash Agency")
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        import config
        import filters

        assert config.COMPANY_NEVER_FETCH == ["Trash Agency"]
        for title in ("Chief of Staff", "Driver", "Head of Product"):
            assert filters.fetch_time_drop_reason("Trash Agency", title) == (
                "company_never_fetch — Trash Agency is on the profile never-fetch list"
            )
    finally:
        db.close_conn()
        _restore_chain(saved)


def test_company_in_neither_list_is_untouched(tmp_path, monkeypatch, restore_profile_env):
    """The no-entry control: a company named nowhere behaves exactly as before."""
    profile = _write_profile(
        tmp_path / "p",
        title_filters="- Narrow Org :: product",
        never_fetch="- Trash Agency",
    )
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        import filters

        for title in ("Driver", "Product Manager", "Supply Chain Assistant"):
            assert filters.fetch_time_drop_reason("Some Other NGO", title) is None
            assert filters.company_never_fetch_reason("Some Other NGO") is None
            assert filters.company_title_filter_reason("Some Other NGO", title) is None
    finally:
        db.close_conn()
        _restore_chain(saved)


# ---------------------------------------------------------------------------
# End-to-end: the drop must PREVENT THE SAVE
# ---------------------------------------------------------------------------

JOBS = [
    {
        "title": "Product Manager - Specialist",
        "full_description": "Real long body. " * 12,
        "url": "https://x/1",
    },
    {
        "title": "Supply Chain Assistant",
        "full_description": "Real long body. " * 12,
        "url": "https://x/2",
    },
    {
        "title": "Driver GS2",
        "full_description": "Real long body. " * 12,
        "url": "https://x/3",
    },
]


def _run_fetch(fv, monkeypatch, companies: dict, jobs: list):
    """One real fetch run over `companies`, with the scraper handing back `jobs`."""
    monkeypatch.setattr(fv, "fetch_firecrawl_scrape", lambda *a, **k: [dict(j) for j in jobs])
    monkeypatch.setattr(fv, "COMPANIES", companies, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch_vacancies.py", "--force-all", "--no-boards", "--no-auto-enrich", "--no-dashboard"],
    )
    fv.main()


def _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path):
    monkeypatch.setattr(run_status, "STATUS_PATH", tmp_path / "run_status.json", raising=False)
    monkeypatch.setattr(fv, "FETCH_STATS_PATH", tmp_path / "fetch_stats.json", raising=False)
    monkeypatch.setattr(fv, "FETCH_LOG_DIR", tmp_path / "fetch_log", raising=False)


def _stored_titles(db):
    return {v["title"] for v in db.load_vacancies(include_inactive_companies=True).values()}


def _read_stats(tmp_path):
    import json

    return json.loads((tmp_path / "fetch_stats.json").read_text(encoding="utf-8"))


CONFIG = {"strategy": "firecrawl_scrape", "url": "https://x", "tier": "B"}


def test_title_filter_drops_before_the_save(tmp_path, monkeypatch, restore_profile_env):
    """A role outside the include-list never reaches the database at all."""
    profile = _write_profile(tmp_path / "p", title_filters="- Narrow Org :: product manager")
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        db.ensure_company("Narrow Org", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv
        import run_status

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)
        _run_fetch(fv, monkeypatch, {"Narrow Org": dict(CONFIG)}, JOBS)
        db.get_conn().commit()

        assert _stored_titles(db) == {"Product Manager - Specialist"}
        stats = _read_stats(tmp_path)
        assert stats[fv.PROFILE_DROP_KEY] == {"Narrow Org": 2}
    finally:
        db.close_conn()
        _restore_chain(saved)


def test_never_fetch_skips_the_company_entirely(tmp_path, monkeypatch, restore_profile_env):
    """A banned company costs nothing: the fetcher is never even called."""
    profile = _write_profile(tmp_path / "p", never_fetch="- Trash Agency")
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        db.ensure_company("Trash Agency", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv
        import run_status

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)

        calls = []

        def _never(*a, **k):
            calls.append(a)
            return [dict(j) for j in JOBS]

        monkeypatch.setattr(fv, "fetch_firecrawl_scrape", _never)
        monkeypatch.setattr(fv, "COMPANIES", {"Trash Agency": dict(CONFIG)}, raising=False)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "fetch_vacancies.py",
                "--force-all",
                "--no-boards",
                "--no-auto-enrich",
                "--no-dashboard",
            ],
        )
        fv.main()
        db.get_conn().commit()

        assert calls == [], "a never-fetch company must not be requested at all"
        assert _stored_titles(db) == set()
        stats = _read_stats(tmp_path)
        assert stats[fv.PROFILE_SKIPPED_KEY] == ["Trash Agency"]
        assert stats["career_sites"]["total"] == 0
    finally:
        db.close_conn()
        _restore_chain(saved)


def test_unlisted_company_still_saves_everything(tmp_path, monkeypatch, restore_profile_env):
    """The regression guard: with both sections in use, a company in neither is
    fetched and saved exactly as before, and contributes no drop count."""
    profile = _write_profile(
        tmp_path / "p",
        title_filters="- Narrow Org :: product manager",
        never_fetch="- Trash Agency",
    )
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        db.ensure_company("Plain Org", status="active")
        db.get_conn().commit()

        import fetch_vacancies as fv
        import run_status

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)
        _run_fetch(fv, monkeypatch, {"Plain Org": dict(CONFIG)}, JOBS)
        db.get_conn().commit()

        assert _stored_titles(db) == {j["title"] for j in JOBS}
        stats = _read_stats(tmp_path)
        assert stats[fv.PROFILE_DROP_KEY] == {}
        assert stats[fv.PROFILE_SKIPPED_KEY] == []
    finally:
        db.close_conn()
        _restore_chain(saved)


def test_empty_profile_leaves_everything_on(tmp_path, monkeypatch, restore_profile_env):
    """Both sections present but empty = feature off; nothing is dropped."""
    profile = _write_profile(tmp_path / "p")
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        db.ensure_company("Narrow Org", status="active")
        db.get_conn().commit()

        import config
        import fetch_vacancies as fv
        import run_status

        assert config.COMPANY_TITLE_FILTERS == {}
        assert config.COMPANY_NEVER_FETCH == []

        _quiet_disk_writes(monkeypatch, fv, run_status, tmp_path)
        _run_fetch(fv, monkeypatch, {"Narrow Org": dict(CONFIG)}, JOBS)
        db.get_conn().commit()

        assert _stored_titles(db) == {j["title"] for j in JOBS}
        assert _read_stats(tmp_path)[fv.PROFILE_DROP_KEY] == {}
    finally:
        db.close_conn()
        _restore_chain(saved)


# ---------------------------------------------------------------------------
# The WFP include-list, checked against real titles from the live database
# ---------------------------------------------------------------------------

#: The WFP patterns now in the profile — Innovation Accelerator work only.
WFP_PATTERNS = [
    "innovation accelerator",
    "business innovation",
    "innovation officer",
    "innovation manager",
    "innovation lead",
    "product manager",
    "product owner",
    "head of product",
]

#: Every WFP title these patterns keep, from the 195 rows stored on 2026-08-28.
WFP_SURVIVORS = [
    "Business Innovation Consultant - VA 174031",
    "Business innovation manager - VA 831464",
    "Business Innovation Senior Consultant",
    "Business Innovation senior manager",
    "Product Manager - Specialist",
]

#: A sample of what they drop — the supply-chain, driver and country-office bulk
#: that filled the morning digest, plus the near-misses the OLD patterns
#: ("digital", "data", "strategy") let through.
WFP_DROPPED = [
    "Conductor GS2",
    "Conductor GS3",
    "Supply Chain Officer NOA",
    "Supply Chain Expert (SSA, L10)",
    "BSA Supply Chain",
    "Logistics Associate FSQ",
    "Programme Policy Officer",
    "Programme Associate (School Feeding) GS6",
    "Asistente de Logistica",
    "Head of Field Office",
    # Near-misses the old, looser list kept:
    "Digital Transformation Lead",
    "Stagiaire chargé(e) de programme Digital & IT Operations",
    "Senior Data Analyst - Consultant",
    "Data Scientist",
    "VAM Officer (Data Analysis) - NOB",
]


@pytest.mark.parametrize("title", WFP_SURVIVORS)
def test_wfp_patterns_keep_the_innovation_roles(title, tmp_path, monkeypatch, restore_profile_env):
    profile = _write_profile(
        tmp_path / "p",
        title_filters=f"- WFP - World Food Programme :: {', '.join(WFP_PATTERNS)}",
    )
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        import filters

        assert filters.fetch_time_drop_reason("WFP - World Food Programme", title) is None
    finally:
        db.close_conn()
        _restore_chain(saved)


@pytest.mark.parametrize("title", WFP_DROPPED)
def test_wfp_patterns_drop_the_rest(title, tmp_path, monkeypatch, restore_profile_env):
    profile = _write_profile(
        tmp_path / "p",
        title_filters=f"- WFP - World Food Programme :: {', '.join(WFP_PATTERNS)}",
    )
    db, saved = _reset_chain(monkeypatch, tmp_path / "jobsearch.db", profile)
    try:
        import filters

        assert filters.fetch_time_drop_reason("WFP - World Food Programme", title) is not None
    finally:
        db.close_conn()
        _restore_chain(saved)
