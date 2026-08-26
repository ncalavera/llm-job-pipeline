"""Schema integrity tests for the 2-table schema v2.

Validates that the Pydantic models, the Python status vocabulary and the SQL
CHECK constraints all say the same thing. Tests cover: enum values, NOT NULL
enforcement, value range constraints. Runtime validate_db() owns the
drift-detection contract.

The status list is READ from sql/schema.sql, never retyped here. A hand-written
copy is what let this file and scripts/models.py keep a stale ten-value list
after 'test_task', 'interview' and 'declined' shipped — drift that tested green.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.DOTALL | re.IGNORECASE)
_STATUS_CHECK_RE = re.compile(
    r"status\s+TEXT.*?CHECK \(status IN \((?P<values>[^)]*)\)\)", re.DOTALL | re.IGNORECASE
)


def sql_status_values(table: str, dialect: str = "") -> set[str]:
    """The values ``<table>.status``'s CHECK constraint allows, read from the
    frozen baseline schema for ``dialect`` ('' = Postgres, '.sqlite')."""
    sql = (REPO_ROOT / "sql" / f"schema{dialect}.sql").read_text(encoding="utf-8")
    for name, body in _TABLE_RE.findall(sql):
        if name.lower() != table:
            continue
        m = _STATUS_CHECK_RE.search(body)
        assert m, f"no status CHECK found on {table} in schema{dialect}.sql"
        return set(re.findall(r"'([^']+)'", m.group("values")))
    raise AssertionError(f"table {table} not found in schema{dialect}.sql")


VALID_COMPANY_STATUSES = sql_status_values("company")
VALID_VACANCY_STATUSES = sql_status_values("vacancy")
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
    """The model enum, the Python vocabulary and the SQL CHECK are one list.

    Any of the three drifting from the others fails here — that drift is why a
    role in the application funnel could not be validated by the model."""
    import database_supabase
    import statuses
    from models import VacancyStatus

    assert set(v.value for v in VacancyStatus) == VALID_VACANCY_STATUSES
    assert set(statuses.VALID_STATUSES) == VALID_VACANCY_STATUSES
    assert database_supabase.VALID_STATUSES is statuses.VALID_STATUSES


def test_both_dialects_allow_the_same_vacancy_statuses():
    """The SQLite baseline and the Postgres baseline must agree. They did not:
    the funnel statuses reached Postgres through migrations 0019/0020 while
    SQLite recorded those as "n/a (other dialect)" — see migration 0021."""
    assert sql_status_values("vacancy", ".sqlite") == VALID_VACANCY_STATUSES


def test_vacancy_llm_score_range():
    """LLM score must be 0-100."""
    from models import Vacancy

    with pytest.raises(Exception):
        Vacancy(
            id="00000000-0000-0000-0000-000000000001",
            company_id="00000000-0000-0000-0000-000000000002",
            title="Test",
            first_seen="2026-01-01",
            last_seen="2026-01-01",
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
    """Vacancy should have dedup_hash field for deduplication."""
    from models import Vacancy

    v = Vacancy(
        id="00000000-0000-0000-0000-000000000001",
        company_id="00000000-0000-0000-0000-000000000002",
        title="Test",
        first_seen="2026-01-01",
        last_seen="2026-01-01",
        dedup_hash="abc123",
    )
    assert v.dedup_hash == "abc123"


def test_company_description_field():
    """Company should have description field."""
    from models import Company

    c = Company(
        id="00000000-0000-0000-0000-000000000001",
        canonical_name="Test",
        description="SaaS platform",
    )
    assert c.description == "SaaS platform"


def test_company_notes_field():
    """Company should have notes field."""
    from models import Company

    c = Company(
        id="00000000-0000-0000-0000-000000000001",
        canonical_name="Test",
        notes="Great culture",
    )
    assert c.notes == "Great culture"
