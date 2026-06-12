"""Shared fixtures for the llm-job-pipeline test suite."""
import os
import sys
import tempfile

import pytest

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
if not (os.environ.get("SUPABASE_DB_URL") or os.environ.get("SUPABASE_DIRECT_URL")):
    os.environ.setdefault(
        "JOBSEARCH_DB_PATH",
        os.path.join(tempfile.mkdtemp(prefix="ljp_default_db_"), "jobsearch.db"),
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
