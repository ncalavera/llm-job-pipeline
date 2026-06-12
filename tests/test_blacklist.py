"""Tests for _is_blacklisted (database_supabase) — title-level blacklist matching.

Pure logic, no DB.

NOTE FOR THE PUBLIC TEMPLATE: this repo ships a GENERIC example
GLOBAL_BLACKLIST / GLOBAL_BLACKLIST_SUBSTR in config.py. Test classes marked
``FIELD_SPECIFIC`` below assert blacklist entries that are tuning for one
particular job search (finance specialties, non-English working-language
requirements, field-deployment consultancies, etc.). They are skipped here
because the public config keeps those lists generic — uncomment the matching
patterns in config.py for your own search and remove the skip to enable them.
"""

import pytest
from database_supabase import _is_blacklisted

# Reason used to skip field-specific blacklist coverage in the public template.
FIELD_SPECIFIC = pytest.mark.skip(
    reason="asserts field-specific blacklist tuning kept generic in the public template"
)


# ---------------------------------------------------------------------------
# BL01-04: Sales gap roles (Account Director / quota-carrying)
# ---------------------------------------------------------------------------

class TestSalesGap:
    def test_BL01_account_director_blacklisted(self):
        # Account Executive/Manager already covered; Director was missing
        assert _is_blacklisted("Account Director") is True

    def test_BL02_account_director_with_suffix_blacklisted(self):
        assert _is_blacklisted("Senior Account Director - EMEA") is True

    def test_BL03_quota_carrying_blacklisted(self):
        assert _is_blacklisted("Quota Carrying Account Executive") is True

    def test_BL04_account_management_director_not_blacklisted_via_account_alone(self):
        # Sanity: 'account' alone must NOT be blacklisted (catches "Account Manager"
        # via existing pattern, not a bare 'account' word)
        assert _is_blacklisted("Strategic Account Partner") is False


# ---------------------------------------------------------------------------
# BL05-09: Finance specialty roles
# ---------------------------------------------------------------------------

class TestFinanceSpecialty:
    @FIELD_SPECIFIC
    def test_BL05_treasury_analyst_blacklisted(self):
        assert _is_blacklisted("Treasury Analyst") is True

    @FIELD_SPECIFIC
    def test_BL06_treasury_manager_blacklisted(self):
        assert _is_blacklisted("Senior Treasury Manager") is True

    @FIELD_SPECIFIC
    def test_BL07_sanctions_officer_blacklisted(self):
        assert _is_blacklisted("Sanctions Officer") is True

    @FIELD_SPECIFIC
    def test_BL08_operational_risk_analyst_blacklisted(self):
        assert _is_blacklisted("Operational Risk Analyst") is True

    @FIELD_SPECIFIC
    def test_BL09_risk_and_controls_blacklisted(self):
        assert _is_blacklisted("Risk & Controls Analyst") is True


# ---------------------------------------------------------------------------
# BL10-13: Motivation gap (pure marketing-ops / RevOps)
# ---------------------------------------------------------------------------

class TestMotivationGap:
    def test_BL10_marketing_operations_blacklisted(self):
        assert _is_blacklisted("Marketing Operations Manager") is True

    def test_BL11_marketing_automation_blacklisted(self):
        assert _is_blacklisted("Marketing Automation Specialist") is True

    @FIELD_SPECIFIC
    def test_BL12_revops_blacklisted(self):
        assert _is_blacklisted("RevOps Manager") is True

    @FIELD_SPECIFIC
    def test_BL13_revenue_operations_blacklisted(self):
        assert _is_blacklisted("Revenue Operations Lead") is True


# ---------------------------------------------------------------------------
# BL14-16: Scientific / clinical research specialty
# ---------------------------------------------------------------------------

class TestClinicalResearch:
    def test_BL14_clinical_research_lead_blacklisted(self):
        assert _is_blacklisted("Clinical Research Lead") is True

    def test_BL15_research_lead_clinical_blacklisted(self):
        assert _is_blacklisted("Research Lead, Clinical") is True

    @FIELD_SPECIFIC
    def test_BL16_climate_health_consultant_blacklisted(self):
        assert _is_blacklisted("Climate Health Consultant") is True


# ---------------------------------------------------------------------------
# BL17-20: Negative tests — non-blacklisted titles must remain unfiltered
# ---------------------------------------------------------------------------

class TestNegativeNoFalsePositives:
    def test_BL17_head_of_operations_not_blacklisted(self):
        # 'operations' alone must not blacklist generic ops roles
        assert _is_blacklisted("Head of Operations") is False

    def test_BL18_chief_of_staff_not_blacklisted(self):
        assert _is_blacklisted("Chief of Staff") is False

    def test_BL19_program_manager_not_blacklisted(self):
        assert _is_blacklisted("Senior Program Manager") is False

    def test_BL20_head_of_marketing_not_blacklisted(self):
        # 'marketing' alone is NOT blacklisted — only 'marketing operations'/'automation'
        assert _is_blacklisted("Head of Marketing") is False


# ---------------------------------------------------------------------------
# BL21-25: Description-level kill phrases (issue #232 — visa/citizenship)
# ---------------------------------------------------------------------------

class TestDescriptionLevelBlacklist:
    def test_BL21_visa_sponsorship_in_description_blacklists(self):
        # Title alone is fine (Senior PM); description disqualifies via #232 fix
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
        # Sanity: positive visa mention must NOT trigger blacklist
        title = "Senior Product Manager"
        desc = "We welcome international applicants — visa sponsorship offered."
        assert _is_blacklisted(title, desc) is False

    def test_BL25_citizen_advocacy_role_not_blacklisted(self):
        # 'citizen' alone is too generic to blacklist
        title = "Citizen Engagement Lead"
        desc = "Help engage citizens with civic tech."
        assert _is_blacklisted(title, desc) is False


# ---------------------------------------------------------------------------
# BL26-32: Non-English working-language requirements (issue #233 §Layer 2)
# ---------------------------------------------------------------------------

class TestNonEnglishLanguageRequirements:
    @FIELD_SPECIFIC
    def test_BL26_french_is_required_in_desc(self):
        # IRC Project Coordinator (Ensemble) real-data sample
        desc = "French is required. Working knowledge of English is preferred."
        assert _is_blacklisted("Project Coordinator", desc) is True

    @FIELD_SPECIFIC
    def test_BL27_arabic_is_required_in_desc(self):
        desc = "Arabic is required (spoken and written)."
        assert _is_blacklisted("Consultancy Telehealth", desc) is True

    @FIELD_SPECIFIC
    def test_BL28_khmer_required_in_desc(self):
        desc = "Khmer required for fieldwork in Cambodia."
        assert _is_blacklisted("Programme Officer", desc) is True

    @FIELD_SPECIFIC
    def test_BL29_portuguese_is_required(self):
        desc = "Native or fluent Portuguese is required."
        assert _is_blacklisted("Country Director", desc) is True

    def test_BL30_french_desirable_not_blacklisted(self):
        # EBRD Analyst — "fluent French highly desirable" must NOT trigger.
        # Desirable ≠ required.
        desc = "fluent French and/or Arabic highly desirable. Relevant industry experience."
        assert _is_blacklisted("Analyst, NBFI", desc) is False

    def test_BL31_english_required_french_secondary_not_blacklisted(self):
        # UNCTAD Chief — "English is required. Either Spanish or Russian"
        # English-required + secondary mention must NOT trigger.
        desc = "English is required. Either Spanish or Russian is desirable."
        assert _is_blacklisted("Chief of Section", desc) is False

    def test_BL32_french_in_unrelated_context_not_blacklisted(self):
        # Random "French" mention without "required/is required" must NOT match.
        desc = "Our office is in the French Quarter. Bring your laptop."
        assert _is_blacklisted("Operations Lead", desc) is False


# ---------------------------------------------------------------------------
# BL33-35: Field consultancy title patterns (issue #233 §Layer 2)
# ---------------------------------------------------------------------------

class TestFieldConsultancyTitlePatterns:
    @FIELD_SPECIFIC
    def test_BL33_consultant_tanzania_in_title(self):
        # Pure Earth real-data sample
        assert _is_blacklisted("Program Management Consultant, Tanzania") is True

    @FIELD_SPECIFIC
    def test_BL34_consultant_cambodia_in_title(self):
        assert _is_blacklisted("Heat-Health Consultant, Cambodia") is True

    def test_BL35_consultant_in_eu_country_not_filtered_by_geo(self):
        # 'Consultant, Germany' must NOT trigger field-consultancy blacklist
        # (Germany is not a field-deployment country)
        assert _is_blacklisted("Consultant, Germany") is False
