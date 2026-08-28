"""Programs, courses, grants and test postings must not reach the scorer.

Five of the first twenty items scored on the night of 2026-08-27 were not job
postings — an accelerator programme, a grant-application page, a course
catalogue, a six-week career accelerator — and each one cost a real Opus call
out of a budget that ran out before the night finished. Item 013 was a
recruiter's placeholder titled "US TEST JOB 2026 - DO NOT APPLY".

The gate is deliberately a CONJUNCTION, because no single signal is safe. Every
title below was checked against all 4,927 rows of the live database: a
title-only rule dropped "Programme Strategy and Stakeholder Engagement Expert"
(scored 92) and "Policy Programs & Partnerships, Global Impact" (scored 78).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import filters  # noqa: E402

#: Text with the structure every real posting carries.
REAL_JD = (
    "About the role. You will own the programme end to end. "
    "Responsibilities include running the team. Qualifications: five years. "
    "How to apply: send a CV."
)
#: A board snippet for something you join, not a job you are hired for.
OFFERING_BLURB = (
    "A six-week cohort for people who want more impact. Applications close in "
    "October. Places are limited and the cohort meets weekly online."
)


# ---------------------------------------------------------------------------
# Test postings — title alone, the phrases are unambiguous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "US TEST JOB 2026 - DO NOT APPLY",
        "Test Job — please ignore",
        "Dummy posting",
        "This is a test",
    ],
)
def test_recruiter_placeholders_are_dropped(title):
    assert filters.not_a_vacancy_reason(title, REAL_JD) == "test posting, not a real job"


def test_a_real_testing_role_survives():
    """The word "test" is not the signal — the placeholder phrases are."""
    for title in ("QA Test Engineer", "Head of Test Automation", "Test Analyst"):
        assert filters.not_a_vacancy_reason(title, REAL_JD) is None


# ---------------------------------------------------------------------------
# Offerings — the three conditions must all hold
# ---------------------------------------------------------------------------

#: Real titles from the live database that the night wasted Opus calls on.
OFFERINGS = [
    "Impact Accelerator Program",
    "Introductory EA Program",
    "Grant, Transformative AI Fund",
    "Submit Your Profile to the High-Impact Talent Directory",
    "Run an EAGx or EA Summit",
    "Facilitating Virtual Programs",
    "Animal Advocacy Course",
    "Research Program, Fall 2026",
    "AI Civic Action Accelerator",
    "Fellowship (2026)",
    "Skoll Scholarship",
    "Research Incubator",
]

#: Real roles from the live database, with the score they actually earned.
REAL_ROLES = [
    ("Senior Program Manager, Google DeepMind Impact Accelerator", 80),
    ("Career Bootcamp Lead", 80),
    ("OCDI Program and Grants Associate", 17),
    ("Head of the EA Infrastructure Fund", 86),
    ("Associate Program Officer, Transformative AI Fund", 60),
    ("Programme Coordinator", 34),
    ("Head of Programmes", None),
    ("Head of Courses", None),
    ("Program Manager", None),
    ("Chief Program Officer", 38),
    ("Youth Program Associate", None),
    ("Programme Strategy and Stakeholder Engagement Expert", 92),
    ("Graduate Programme 2027: Product Owner (UX)", None),
]


@pytest.mark.parametrize("title", OFFERINGS)
def test_offering_without_a_role_or_a_description_is_dropped(title):
    assert (
        filters.not_a_vacancy_reason(title, OFFERING_BLURB)
        == "a program or grant to apply to, not a job"
    )


@pytest.mark.parametrize("title,score", REAL_ROLES)
def test_a_real_role_survives_whatever_its_title_mentions(title, score):
    """A role noun in the title means a person is being hired."""
    assert filters.not_a_vacancy_reason(title, OFFERING_BLURB) is None, score


@pytest.mark.parametrize(
    "title",
    [
        # Both scored well and neither title carries a role noun at all — only
        # the job-description structure keeps them.
        "Policy Programs & Partnerships, Global Impact",
        "GTM Strategy & Operations, Strategic Programs",
    ],
)
def test_a_real_posting_with_no_role_noun_is_saved_by_its_description(title):
    assert filters.not_a_vacancy_reason(title, OFFERING_BLURB) is not None  # title alone: dropped
    assert filters.not_a_vacancy_reason(title, REAL_JD) is None  # with the JD: kept


def test_an_ordinary_role_is_never_touched():
    for title in ("Software Engineer", "Volunteer Coordinator", "Research Fellow"):
        assert filters.not_a_vacancy_reason(title, "") is None


def test_empty_title_is_not_a_verdict():
    assert filters.not_a_vacancy_reason("", REAL_JD) is None
    assert filters.not_a_vacancy_reason(None, None) is None
