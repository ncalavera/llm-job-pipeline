"""Shared fixtures for the llm-job-pipeline test suite."""
import sys
import pytest


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
