"""Typed boundaries for the llm-job-pipeline Supabase schema (2 tables).

These Pydantic models mirror sql/schema.sql. The live database is the source
of truth; these models are a convenience for validation at trust boundaries
(API input, importers) and for the schema-integrity tests.
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CompanyStatus(str, Enum):
    candidate = "candidate"
    active = "active"
    inactive = "inactive"


class Tier(str, Enum):
    S = "S"
    A = "A"
    B = "B"
    C = "C"


class VacancyStatus(str, Enum):
    unseen = "unseen"
    liked = "liked"
    passed = "passed"
    to_apply = "to_apply"
    to_research = "to_research"
    to_network = "to_network"
    skipped = "skipped"
    applied = "applied"
    expiring = "expiring"
    archived = "archived"


class Company(BaseModel):
    id: UUID
    canonical_name: str
    description: Optional[str] = None
    category: Optional[str] = None
    website: Optional[str] = None
    careers_url: Optional[str] = None
    offices: Optional[str] = None
    notes: Optional[str] = None
    tier: Optional[Tier] = None
    experience_match: Optional[int] = Field(None, ge=0, le=10)
    personal_interest: Optional[int] = Field(None, ge=0, le=10)
    status: CompanyStatus = CompanyStatus.active
    status_reason: Optional[str] = None
    fetch_strategy: Optional[str] = None
    ats_slug: Optional[str] = None
    ats_config: Optional[dict] = None
    aliases: list[str] = Field(default_factory=list)
    about: Optional[dict] = None
    mission_fit: Optional[dict] = None
    alignment_score: Optional[float] = None
    enriched_at: Optional[datetime] = None
    last_fetched: Optional[datetime] = None
    vacancy_count: int = 0
    fetch_status: Optional[str] = None


class Vacancy(BaseModel):
    id: UUID
    dedup_hash: Optional[str] = None
    company_id: UUID
    title: str
    snippet: Optional[str] = None
    full_description: Optional[str] = None
    compensation: Optional[str] = None
    deadline: Optional[date] = None
    locations: list[dict] = Field(default_factory=list)
    llm_score: Optional[int] = Field(None, ge=0, le=100)
    llm_summary: Optional[str] = None
    llm_reasoning: Optional[str] = None
    status: VacancyStatus = VacancyStatus.unseen
    status_reason: Optional[str] = None
    status_updated_at: Optional[datetime] = None
    first_seen: date
    last_seen: date
    triage: Optional[list[dict]] = None
