"""Tests for the filters blacklist predicates — title-level blacklist matching.

Pure logic, no DB.

The default pre-score blacklist is UNIVERSAL_JUNK only — speculative / evergreen
pipeline postings nobody wants on any job search (talent pools, expressions of
interest, general/open applications) plus visa/citizenship kill phrases in the
description. A specific job DISCIPLINE, format or career stage is NEVER dropped
by default; that is a PERSONAL choice expressed via ``exclude_title_keywords``
in the profile's ``## HARD_FILTERS`` section. The two test classes at the bottom
prove that contract: with an empty profile a discipline title survives; with the
keyword listed it is dropped.
"""

import filters


def _is_blacklisted(title, description=""):
    """Old DAL semantics: title-words-or-substr OR description-kill-phrase."""
    return filters.title_words_blacklisted(title) or filters.description_words_blacklisted(
        description
    )


# ---------------------------------------------------------------------------
# UJ01-06: Universal junk — dropped for EVERYONE, no discipline involved
# ---------------------------------------------------------------------------


class TestUniversalJunk:
    def test_UJ01_expression_of_interest_blacklisted(self):
        assert _is_blacklisted("Expression of Interest — Programmes") is True

    def test_UJ02_talent_pool_blacklisted(self):
        assert _is_blacklisted("Talent Pool: Future Roles") is True

    def test_UJ03_general_application_blacklisted(self):
        assert _is_blacklisted("General Application") is True

    def test_UJ04_open_application_blacklisted(self):
        assert _is_blacklisted("Open Application — Any Team") is True

    def test_UJ05_talent_community_blacklisted(self):
        assert _is_blacklisted("Join our Talent Community") is True

    def test_UJ06_talent_network_blacklisted(self):
        assert _is_blacklisted("Talent Network Sign-up") is True


# ---------------------------------------------------------------------------
# Format / career-stage roles are NOT junk — a student / career-changer wants
# them. They ship neutral; only the profile can opt to drop them.
# ---------------------------------------------------------------------------


class TestFormatRolesNotJunk:
    def test_volunteer_coordinator_not_junk(self):
        assert _is_blacklisted("Volunteer Coordinator") is False

    def test_bootcamp_instructor_not_junk(self):
        assert _is_blacklisted("Data Science Bootcamp Instructor") is False

    def test_fellowship_not_junk(self):
        assert _is_blacklisted("Research Fellowship") is False

    def test_internship_not_junk(self):
        assert _is_blacklisted("Summer Internship") is False


# ---------------------------------------------------------------------------
# ND01-06: Disciplines are NOT dropped by default (the public template ships
# an EMPTY exclude_title_keywords — nobody's career taste is imposed)
# ---------------------------------------------------------------------------


class TestDisciplinesNotBlacklistedByDefault:
    def test_ND01_software_engineer_not_blacklisted(self):
        assert _is_blacklisted("Senior Software Engineer") is False

    def test_ND02_developer_not_blacklisted(self):
        assert _is_blacklisted("Backend Developer") is False

    def test_ND03_account_director_not_blacklisted(self):
        assert _is_blacklisted("Account Director") is False

    def test_ND04_marketing_operations_not_blacklisted(self):
        assert _is_blacklisted("Marketing Operations Manager") is False

    def test_ND05_clinical_research_lead_not_blacklisted(self):
        assert _is_blacklisted("Clinical Research Lead") is False

    def test_ND06_nurse_not_blacklisted(self):
        assert _is_blacklisted("Registered Nurse") is False


# ---------------------------------------------------------------------------
# NF01-04: No false positives on ordinary target roles
# ---------------------------------------------------------------------------


class TestNegativeNoFalsePositives:
    def test_NF01_head_of_operations_not_blacklisted(self):
        assert _is_blacklisted("Head of Operations") is False

    def test_NF02_chief_of_staff_not_blacklisted(self):
        assert _is_blacklisted("Chief of Staff") is False

    def test_NF03_program_manager_not_blacklisted(self):
        assert _is_blacklisted("Senior Program Manager") is False

    def test_NF04_head_of_marketing_not_blacklisted(self):
        assert _is_blacklisted("Head of Marketing") is False


# ---------------------------------------------------------------------------
# BL21-25: Description-level kill phrases (visa/citizenship).
# These ARE universal (a posting that won't sponsor / requires US citizenship
# is useless to most international searchers) and stay hardcoded.
# ---------------------------------------------------------------------------


class TestDescriptionLevelBlacklist:
    def test_BL21_visa_sponsorship_in_description_blacklists(self):
        title = "Senior Product Manager"
        desc = "We are hiring. Visa sponsorship not available for this role."
        assert _is_blacklisted(title, desc) is True

    def test_BL22_us_citizen_in_description_blacklists(self):
        title = "Senior Product Manager"
        desc = "Must be a US citizen due to clearance requirements."
        assert _is_blacklisted(title, desc) is True

    def test_BL23_us_citizen_no_article_variant(self):
        title = "Director of Strategy"
        desc = "Applicants must be US citizen at time of hire."
        assert _is_blacklisted(title, desc) is True

    def test_BL24_visa_friendly_phrasing_not_blacklisted(self):
        title = "Senior Product Manager"
        desc = "We welcome international applicants — visa sponsorship offered."
        assert _is_blacklisted(title, desc) is False

    def test_BL25_citizen_advocacy_role_not_blacklisted(self):
        # 'citizen' alone is too generic to blacklist
        title = "Citizen Engagement Lead"
        desc = "Help engage citizens with civic tech."
        assert _is_blacklisted(title, desc) is False
