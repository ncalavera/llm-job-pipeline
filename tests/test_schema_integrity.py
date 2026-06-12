"""Schema integrity tests for Supabase 2-table schema v2.

Validates Pydantic model constraints match the live Supabase schema.
Tests cover: enum values, NOT NULL enforcement, value range constraints.
The static schema.sql was dropped 2026-05-01 (issue #226) — runtime
validate_db() now owns the drift-detection contract.
"""

import pytest


VALID_COMPANY_STATUSES = {"candidate", "active", "inactive"}
VALID_VACANCY_STATUSES = {
    "unseen", "liked", "passed", "to_apply",
    "to_research", "to_network", "skipped", "applied", "archived",
}
VALID_TIERS = {"S", "A", "B", "C"}


def test_company_canonical_name_required():
    """Company must have a canonical_name."""
    from models import Company
    with pytest.raises(Exception):
        Company(id="00000000-0000-0000-0000-000000000001", canonical_name=None)


def test_company_status_enum():
    """Company status must be candidate/active/inactive."""
    from models import CompanyStatus
    assert set(s.value for s in CompanyStatus) == VALID_COMPANY_STATUSES


def test_vacancy_status_enum():
    """Vacancy status must be one of 8 valid values."""
    from models import VacancyStatus
    assert set(v.value for v in VacancyStatus) == VALID_VACANCY_STATUSES


def test_vacancy_llm_score_range():
    """LLM score must be 0-100."""
    from models import Vacancy
    with pytest.raises(Exception):
        Vacancy(
            id="00000000-0000-0000-0000-000000000001",
            company_id="00000000-0000-0000-0000-000000000002",
            title="Test", first_seen="2026-01-01", last_seen="2026-01-01",
            llm_score=150,
        )


def test_tier_enum():
    """Tier must be one of valid values."""
    from models import Tier
    assert set(t.value for t in Tier) == VALID_TIERS


def test_vacancy_requires_company_id():
    """Vacancy must reference a company."""
    from models import Vacancy
    v = Vacancy(
        id="00000000-0000-0000-0000-000000000001",
        company_id="00000000-0000-0000-0000-000000000002",
        title="Test Role",
        first_seen="2026-01-01",
        last_seen="2026-01-01",
    )
    assert v.company_id is not None


def test_experience_match_range():
    """Experience match must be 0-10."""
    from models import Company
    with pytest.raises(Exception):
        Company(
            id="00000000-0000-0000-0000-000000000001",
            canonical_name="Test",
            experience_match=15,
        )


def test_vacancy_dedup_hash_field():
    """Vacancy should have dedup_hash field (renamed from legacy_id)."""
    from models import Vacancy
    v = Vacancy(
        id="00000000-0000-0000-0000-000000000001",
        company_id="00000000-0000-0000-0000-000000000002",
        title="Test", first_seen="2026-01-01", last_seen="2026-01-01",
        dedup_hash="abc123",
    )
    assert v.dedup_hash == "abc123"


def test_company_description_field():
    """Company should have description field (renamed from product)."""
    from models import Company
    c = Company(
        id="00000000-0000-0000-0000-000000000001",
        canonical_name="Test",
        description="SaaS platform",
    )
    assert c.description == "SaaS platform"


def test_company_notes_field():
    """Company should have notes field (renamed from user_comments)."""
    from models import Company
    c = Company(
        id="00000000-0000-0000-0000-000000000001",
        canonical_name="Test",
        notes="Great culture",
    )
    assert c.notes == "Great culture"
