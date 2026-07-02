"""Shared fixtures for the llm-job-pipeline test suite."""

import os
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Disable the automatic repo-root .env load for the WHOLE test session.
#
# Must be set BEFORE any pipeline module is imported: db_backend.load_dotenv()
# runs at db_backend import time and would re-inject the maintainer's real
# SUPABASE_DB_URL / SUPABASE_DIRECT_URL via setdefault AFTER the pops below
# (they run at conftest import; db_backend imports later, during collection) —
# silently pointing the offline suite at a live Supabase. With this flag
# load_dotenv() is a no-op. Tests that exercise the loader itself clear the
# flag locally and point the loader at tmp files, never at the real repo root.
# ---------------------------------------------------------------------------
os.environ["LLM_PIPELINE_DISABLE_DOTENV"] = "1"

# ---------------------------------------------------------------------------
# Isolate the default SQLite path for the whole offline run.
#
# With no SUPABASE_DB_URL set, the registry/DAL now connect to the local SQLite
# backend on import (instead of short-circuiting on an env check). Test modules
# import config / company_registry / database_supabase at COLLECTION time —
# before any fixture runs — so the path must be redirected here, at conftest
# import, not in a fixture. Otherwise importing config would create the repo's
# real ``data/jobsearch.db`` during collection. Tests that need their own DB
# still override JOBSEARCH_DB_PATH themselves.
# ---------------------------------------------------------------------------
# The offline suite must be DETERMINISTIC regardless of the developer's shell.
# A stray SUPABASE_DB_URL exported in the environment (e.g. the maintainer's own
# production Postgres) would otherwise make every test that imports the registry
# / DAL run against that live database — yielding order-dependent failures
# (CN09: stale _ALL_CSV_NAMES vs a 74-company prod DB; INT02: RealDictCursor
# mismatch) and, worse, silently reading/writing real data. Force the local
# SQLite backend for the whole test session by clearing the Supabase env before
# any test module imports config / company_registry / database_supabase.
#
# A test that genuinely needs Postgres must set SUPABASE_DB_URL itself and skip
# when it is absent; none in this suite do.
os.environ.pop("SUPABASE_DB_URL", None)
os.environ.pop("SUPABASE_DIRECT_URL", None)
os.environ.setdefault(
    "JOBSEARCH_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="ljp_default_db_"), "jobsearch.db"),
)

# ---------------------------------------------------------------------------
# Pin the user profile to the bundled EXAMPLE for the whole run. Without this,
# prompts._load_user_profile() falls back to the maintainer's real
# config/user_profile.md (gitignored), whose personal hard filters (e.g.
# exclude_title_keywords: volunteer, coordinator) drop fixture roles like
# "Volunteer Coordinator" and break otherwise-neutral fixtures. A clean clone
# has no such file and passes; this makes the developer's machine behave the
# same. Tests that need a specific profile still set USER_PROFILE_PATH + reload.
# ---------------------------------------------------------------------------
os.environ["USER_PROFILE_PATH"] = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "user_profile.example.md",
)


@pytest.fixture
def empty_db():
    return {
        "last_updated": None,
        "vacancies": {},
        "sources": {},
        "board_last_scraped": {},
    }


@pytest.fixture
def vacancy_record():
    return {
        "id": "abc123",
        "org": "Example Org",
        "tier": "A",
        "title": "Head of Community",
        "department": "",
        "org_url": "",
        "snippet": "Lead community efforts.",
        "full_description": "Lead community efforts globally for our nonprofit platform.",
        "relevance_score": 3,
        "location_match": True,
        "region": "europe",
        "first_seen": "2026-01-01",
        "last_seen": "2026-01-01",
        "locations": [
            {
                "id": "loc1",
                "external_id": "ext_001",
                "url": "https://example.com/job/1",
                "location": "London, UK",
                "location_match": True,
                "region": "europe",
                "compensation": "",
                "snippet": "",
                "full_description": "",
            }
        ],
    }


@pytest.fixture
def board_cfg():
    return {
        "name": "80,000 Hours",
        "url": "https://jobs.80000hours.org",
        "tier": "B",
    }
