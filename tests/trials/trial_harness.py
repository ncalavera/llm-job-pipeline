"""Shared helpers for the persona trials (``tests/trials/``).

The trials reproduce the failures a first real user hit, end to end, from a real
persona PROFILE FIXTURE exercised through the production loaders — the same
``USER_PROFILE_PATH`` + SQLite backend the shipped scripts use — not hand-built
section dicts. Because the pipeline caches the parsed profile and the config at
import time, a trial that swaps the persona must drop those modules first so the
next import re-reads the fixture instead of the suite-pinned example profile.

Everything here is offline: temp SQLite files, recorded/synthetic data, and
request counters — never a network call or a live model.
"""

from __future__ import annotations

import importlib
import os
import sys

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
FIXTURES = os.path.join(TESTS_DIR, "fixtures")

# scripts/ carries the pipeline modules; tests/ carries sibling guards the trials
# compose against (e.g. WORLDVIEW_TOKEN in test_no_hardcoded_data). Both on path.
for _p in (SCRIPTS, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Every module that caches the profile, the resolved config, or the DB handle.
# Dropped before a persona swap so the next import re-reads the fixture; harmless
# for a module a given trial never imports (it simply is not re-created).
_POP = (
    "db_backend",
    "db_conn",
    "database_supabase",
    "company_registry",
    "config",
    "settings",
    "hard_filters",
    "prompts",
    "scoring_settings",
    "score_vacancies",
    "profile_targeting",
    "product_language",
    "i18n",
    "filters",
    "geo",
    "factors",
    "sources",
    "run_daily",
    "run_status",
    "migrate",
    "learning",
    "report",
    "report.data_prep",
)


def profile_path(name_or_path: str) -> str:
    """Resolve a fixtures filename to an absolute path; pass an absolute path through."""
    if os.path.isabs(name_or_path):
        return name_or_path
    return os.path.join(FIXTURES, name_or_path)


# Env vars a developer might have exported that would leak into a trial's
# assertions: JOB_BOARDS (the enabled-board check reads it), PRODUCT_LANGUAGE and
# the legacy DASHBOARD_LANGUAGE / OUTPUT_LANGUAGE knobs (they override the resolved
# product language). Cleared so a trial reflects the fixture, not the shell.
_LEAKY_ENV = ("JOB_BOARDS", "PRODUCT_LANGUAGE", "DASHBOARD_LANGUAGE", "OUTPUT_LANGUAGE")


def _clean_leaky_env(monkeypatch) -> None:
    for var in _LEAKY_ENV:
        monkeypatch.delenv(var, raising=False)


def _drop_cached_modules() -> None:
    for mod in _POP:
        sys.modules.pop(mod, None)


def swap_profile(monkeypatch, profile: str) -> None:
    """Point the profile loaders at ``profile`` (fixtures name or absolute path).

    No database is touched — for the pure language / banner surfaces. Import the
    pipeline modules you need AFTER calling this; their caches are clean, so they
    read the persona profile, not the suite-pinned example.
    """
    _clean_leaky_env(monkeypatch)
    monkeypatch.setenv("USER_PROFILE_PATH", profile_path(profile))
    _drop_cached_modules()


def use_persona(monkeypatch, *, profile: str, db_path, migrate: bool = True):
    """Bind the whole pipeline to a fresh temp SQLite DB and a persona fixture.

    Mirrors a first-time clone: no ``.env``, no Supabase, an empty local database
    file. Returns the freshly-imported ``database_supabase`` module. ``migrate``
    replays the SQL migrations so the schema matches what onboarding builds.
    """
    _clean_leaky_env(monkeypatch)
    monkeypatch.setenv("LLM_PIPELINE_DISABLE_DOTENV", "1")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_path))
    monkeypatch.setenv("USER_PROFILE_PATH", profile_path(profile))
    _drop_cached_modules()

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "trials must run on the SQLite backend"

    if migrate:
        import migrate as _m

        importlib.reload(_m)
        assert _m.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as dal

    return dal


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower())


def seed_roles(dal, org: str, roles, *, status: str = "active", tier: str = "A") -> int:
    """Insert one company (default active) and its roles.

    ``roles`` = ``[(title, description), ...]``. Returns the number of NEW
    vacancy rows saved. Commits — the DAL leaves that to the caller.
    """
    dal.ensure_company(org, status=status)
    jobs = [
        {
            "title": title,
            "snippet": f"{title} — summary.",
            "full_description": desc,
            "location": "Remote",
            "url": f"https://example.test/{_slug(org)}/{_slug(title)}",
        }
        for (title, desc) in roles
    ]
    n = dal.save_vacancies(org, tier, jobs)
    dal.get_conn().commit()
    return n
