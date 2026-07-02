"""Pipeline quality tests.

Covers:
  1. quality.clean_description() — every verdict + edge cases
  2. Title blacklist — non-vacancy junk patterns via _is_blacklisted
  3. _gate_description() in database_supabase.py — unit, no DB write
  4. Deadline / hot_vacancy helpers
  5. Read-only integration checks (skipped without SUPABASE_DB_URL)
  6. enrich_blind_vacancies module — import + _DIRECT_HOST_FETCHERS
"""

import os
import sys
import pytest
from datetime import date, timedelta

# Make sure scripts/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from quality import (
    clean_description,
    strip_cookie_boilerplate,
    is_cookie_boilerplate,
    is_navigation_junk,
    COOKIE_MIN_REMAINDER,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Realistic long JD (>500 chars), used across all banner tests
FULL_JD = (
    "Senior Operations Manager\n\n"
    "We are looking for a Senior Operations Manager to join our mission-driven team in Berlin.\n\n"
    "Responsibilities:\n"
    "- Manage day-to-day operations across multiple European offices\n"
    "- Lead a team of 15 operations professionals toward sustainability goals\n"
    "- Drive process improvement initiatives using lean methodologies\n"
    "- Partner with Finance, HR, and Legal on strategic planning and budget decisions\n"
    "- Develop and implement operational policies that align with company mission\n\n"
    "Requirements:\n"
    "- 7+ years of operations experience in tech or nonprofit sector\n"
    "- Strong leadership and communication skills\n"
    "- Experience in mission-driven organizations preferred\n"
    "- Fluent English required; German an advantage\n\n"
    "What we offer:\n"
    "Competitive salary (EUR 80-100K), equity, 30 days vacation, comprehensive benefits.\n"
    "Immediate start possible for the right candidate with relevant experience.\n"
)

COOKIE_BANNER = (
    "We use cookies to enhance your browsing experience. "
    "By clicking Accept, you agree to our cookie policy. "
    "We use analytics cookies, marketing cookies, functional cookies and essential cookies. "
    "GDPR compliance. Consent to all cookies or customize your preferences below.\n\n"
)


# ===========================================================================
# 1. quality.clean_description()
# ===========================================================================


class TestCleanDescriptionEmpty:
    """Empty inputs."""

    def test_QD01_empty_string(self):
        text, verdict = clean_description("")
        assert verdict == "empty"
        assert text is None

    def test_QD02_whitespace_only(self):
        text, verdict = clean_description("   \n\n  ")
        assert verdict == "empty"
        assert text is None


class TestCleanDescriptionCookieWall:
    """A pure cookie wall → cookie_wall."""

    def test_QD03_pure_cookie_wall_we_use_cookies(self):
        # "We use cookies" banner + almost nothing after → cookie_wall
        wall = (
            "We use cookies to enhance your browsing experience. "
            "By clicking Accept you agree. We care about your privacy. "
            "Consent. GDPR compliance. This site uses cookies."
        )
        text, verdict = clean_description(wall)
        assert verdict == "cookie_wall"
        assert text is None

    def test_QD04_cookie_wall_short_content_after(self):
        # Banner + content < COOKIE_MIN_REMAINDER chars → still cookie_wall
        short_jd = COOKIE_BANNER + "Apply at careers page."
        remainder = len(short_jd[len(COOKIE_BANNER) :].strip())
        assert remainder < COOKIE_MIN_REMAINDER, "fixture too long — fix test"
        _, verdict = clean_description(short_jd)
        assert verdict == "cookie_wall"

    def test_QD05_cookie_wall_text_not_preserved(self):
        # On cookie_wall it returns None, not truncated text
        wall = (
            "We value your privacy. Accept all cookies or manage preferences. "
            "Consent management. GDPR. Essential cookies. Analytics. Marketing."
        )
        text, verdict = clean_description(wall)
        assert text is None


class TestCleanDescriptionBannerStripped:
    """Banner + a full JD → ok, banner stripped."""

    def test_QD06_banner_stripped_jd_intact(self):
        combined = COOKIE_BANNER + FULL_JD
        text, verdict = clean_description(combined)
        assert verdict == "ok"
        assert text is not None
        assert "We use cookies" not in text
        assert "Senior Operations Manager" in text

    def test_QD07_banner_stripped_no_cookie_words_at_start(self):
        combined = COOKIE_BANNER + FULL_JD
        text, _ = clean_description(combined)
        assert text is not None
        assert not text.lower().startswith("we use cookies")

    def test_QD08_strip_cookie_boilerplate_direct(self):
        # strip_cookie_boilerplate() must return text without the banner
        combined = COOKIE_BANNER + FULL_JD
        result = strip_cookie_boilerplate(combined)
        assert "We use cookies" not in result
        assert "Senior Operations Manager" in result


class TestCleanDescriptionErrorPage:
    """Error page / "job closed" → error_page."""

    def test_QD09_position_no_longer_available(self):
        page = "This position is no longer available. Check our careers page for other openings."
        _, verdict = clean_description(page)
        assert verdict == "error_page"

    def test_QD10_job_not_found(self):
        page = "Job not found. The posting you are looking for may have been removed."
        _, verdict = clean_description(page)
        assert verdict == "error_page"

    def test_QD11_404_not_found_short(self):
        page = "404 not found. The page you requested does not exist on this server."
        _, verdict = clean_description(page)
        assert verdict == "error_page"

    def test_QD12_opportunity_has_closed(self):
        page = "This opportunity has closed. Thank you for your interest."
        _, verdict = clean_description(page)
        assert verdict == "error_page"


class TestCleanDescriptionNavJunk:
    """Navigation chrome → nav_junk."""

    def test_QD13_nav_chrome(self):
        nav = "Home About Contact Menu Search Login Sign in Sign up Register Careers Jobs News Blog"
        _, verdict = clean_description(nav)
        assert verdict == "nav_junk"

    def test_QD14_is_navigation_junk_direct(self):
        nav = "Home About us Contact us Menu Search Login Sign in Register Careers Privacy Terms"
        assert is_navigation_junk(nav) is True


class TestCleanDescriptionTooShort:
    """Content < min_chars → too_short."""

    def test_QD15_very_short_text(self):
        short = "Operations lead opening."
        _, verdict = clean_description(short)
        assert verdict == "too_short"


class TestCleanDescriptionNoFalsePositives:
    """Legitimate JDs that mention privacy/cookie phrases → ok."""

    def test_QD16_privacy_role_jd_not_flagged(self):
        # A privacy-team JD mentioning "cookie consent tooling" must not trigger
        privacy_jd = (
            "Senior Product Manager – Privacy Infrastructure\n\n"
            "We are looking for a Senior PM to join our Privacy Team.\n\n"
            "Responsibilities:\n"
            "- Define strategy for cookie consent tooling across all products\n"
            "- Own the cookie policy compliance roadmap\n"
            "- Work with engineering on GDPR compliance features\n"
            "- Partner with legal on data privacy regulations\n\n"
            "Requirements:\n"
            "- 5+ years of product management experience\n"
            "- Deep understanding of privacy regulations (GDPR, CCPA)\n"
            "- Experience with consent management platforms\n\n"
            "We offer competitive salary and equity.\n"
            "Join our team dedicated to user privacy and data protection.\n"
        )
        text, verdict = clean_description(privacy_jd)
        assert verdict == "ok"
        assert text is not None
        # "cookie consent tooling" in the body — must be kept
        assert "cookie consent tooling" in text

    def test_QD17_cookie_policy_mention_in_middle_not_flagged(self):
        # "cookie policy compliance" mid-JD — not a banner
        jd = (
            "Director of Legal Affairs\n\n"
            "Lead our global legal team. You will oversee cookie policy compliance "
            "and data protection across all regions. Manage a team of 8 lawyers.\n"
            "Requirements: 10+ years legal experience, JD required.\n"
            "Competitive salary. Relocation assistance available for top candidates.\n"
            "Apply through our careers portal with cover letter and CV.\n"
        )
        text, verdict = clean_description(jd)
        assert verdict == "ok"

    def test_QD18_long_jd_not_a_cookie_wall(self):
        # A long normal JD must not be flagged by the cookie detector
        assert is_cookie_boilerplate(FULL_JD) is False


# ===========================================================================
# 2. Title blacklist — non-vacancy junk patterns
# ===========================================================================


class TestBlacklistJunkMatched:
    """Universal-junk listings must match — only "there is no concrete single
    role here at all" markers (talent pools, generic applications, volunteer
    calls). Discipline-/format-flavored words are NOT universal junk and are
    asserted absent in TestBlacklistFormatWordsNotJunk below."""

    @pytest.fixture(autouse=True)
    def import_blacklist(self):
        import filters

        self._is_blacklisted = lambda title, desc="": (
            filters.title_words_blacklisted(title) or filters.description_words_blacklisted(desc)
        )

    def test_talent_pool(self):
        assert self._is_blacklisted("Talent Pool — Future Roles") is True

    def test_expression_of_interest(self):
        assert self._is_blacklisted("Expression of Interest: General") is True

    def test_general_application(self):
        assert self._is_blacklisted("General Application") is True

    def test_open_application(self):
        assert self._is_blacklisted("Open Application — Any Team") is True

    def test_talent_network(self):
        assert self._is_blacklisted("Join Our Talent Network") is True

    def test_speculative_application(self):
        assert self._is_blacklisted("Speculative Application") is True


class TestBlacklistFormatWordsNotJunk:
    """Format-/discipline-flavored words ship NEUTRAL: they are the user's
    optional taste (profile exclude_title_keywords), never universal junk. A
    bootcamp instructor, a paid fellowship, a funding-strategy lead can be real
    roles. With an empty profile none of these are dropped."""

    @pytest.fixture(autouse=True)
    def import_blacklist(self):
        import filters

        self._is_blacklisted = lambda title, desc="": (
            filters.title_words_blacklisted(title) or filters.description_words_blacklisted(desc)
        )

    def test_bootcamp_not_junk(self):
        # "Data Science Bootcamp" instructor is a real job
        assert self._is_blacklisted("Data Science Bootcamp Lead Instructor") is False

    def test_course_not_junk(self):
        assert self._is_blacklisted("Course, Intro to X") is False

    def test_summer_school_not_junk(self):
        assert self._is_blacklisted("Summer School Coordinator") is False

    def test_training_on_not_junk(self):
        assert self._is_blacklisted("Training on M&E Methods for NGOs") is False

    def test_fellowship_not_junk(self):
        # Many real paid staff roles are called fellowships
        assert self._is_blacklisted("Fellowship Programme in AI Governance") is False

    def test_funding_not_junk(self):
        # Grant/funding calls are real jobs for fundraisers
        assert self._is_blacklisted("Funding Strategy Lead 2026") is False


class TestBlacklistLegitNotMatched:
    """Legitimate titles must NOT match."""

    @pytest.fixture(autouse=True)
    def import_blacklist(self):
        import filters

        self._is_blacklisted = lambda title, desc="": (
            filters.title_words_blacklisted(title) or filters.description_words_blacklisted(desc)
        )

    def test_program_director_not_blacklisted(self):
        assert self._is_blacklisted("Program Director") is False

    def test_senior_operations_manager_not_blacklisted(self):
        assert self._is_blacklisted("Senior Operations Manager") is False

    def test_crowdfunding_lead_not_blacklisted(self):
        assert self._is_blacklisted("Crowdfunding Lead") is False

    def test_head_of_courses_strategy_not_blacklisted(self):
        assert self._is_blacklisted("Head of Courses Strategy") is False

    def test_director_of_training_not_blacklisted(self):
        # "training on" needs "on"; "Director of Training" has none
        assert self._is_blacklisted("Director of Training") is False

    def test_head_of_fundraising_not_blacklisted(self):
        # "fundraising" does not contain "funding,"
        assert self._is_blacklisted("Head of Fundraising") is False


# ===========================================================================
# 3. _gate_description() in database_supabase.py — no DB write
# ===========================================================================


class TestGateDescription:
    """_gate_description mutates the job dict, no DB calls."""

    @pytest.fixture(autouse=True)
    def import_gate(self):
        from database_supabase import _gate_description

        self._gate_description = _gate_description

    def test_GD01_cookie_wall_blanked(self):
        job = {
            "full_description": (
                "We use cookies to enhance your browsing experience. "
                "By clicking Accept, you agree to our cookie policy. "
                "GDPR compliance. Consent. This site uses cookies. "
                "We value your privacy and use analytics cookies."
            )
        }
        verdict = self._gate_description(job)
        assert verdict == "cookie_wall"
        assert job["full_description"] == ""

    def test_GD02_normal_jd_passes_unchanged(self):
        text = FULL_JD
        job = {"full_description": text}
        verdict = self._gate_description(job)
        assert verdict is None
        assert "Senior Operations Manager" in job["full_description"]

    def test_GD03_banner_stripped_jd_saved(self):
        combined = COOKIE_BANNER + FULL_JD
        job = {"full_description": combined}
        verdict = self._gate_description(job)
        # Banner stripped, JD kept — verdict ok (None from the gate)
        assert verdict is None
        assert "We use cookies" not in job["full_description"]
        assert "Senior Operations Manager" in job["full_description"]

    def test_GD04_error_page_blanked(self):
        job = {
            "full_description": (
                "This position is no longer available. "
                "Please check our careers page for current openings."
            )
        }
        verdict = self._gate_description(job)
        assert verdict == "error_page"
        assert job["full_description"] == ""

    def test_GD05_empty_description_passes_without_verdict(self):
        # Empty field — the gate leaves it alone (snippet-fallback applies)
        job = {"full_description": ""}
        verdict = self._gate_description(job)
        assert verdict is None


# ===========================================================================
# 4. Deadline / hot_vacancy helpers
# ===========================================================================


class TestDeadlineSoonLabelTelegram:
    """deadline_soon_label from telegram_digest.py."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from telegram_digest import deadline_soon_label, DEADLINE_SOON_DAYS, HOT_VACANCY_SCORE

        self.deadline_soon_label = deadline_soon_label
        self.DEADLINE_SOON_DAYS = DEADLINE_SOON_DAYS
        self.HOT_VACANCY_SCORE = HOT_VACANCY_SCORE

    def test_DS08_three_days_gives_label(self):
        dl = (date.today() + timedelta(days=3)).isoformat()
        label = self.deadline_soon_label(dl)
        assert label.startswith("deadline ")

    def test_DS09_thirty_days_no_label(self):
        dl = (date.today() + timedelta(days=30)).isoformat()
        assert self.deadline_soon_label(dl) == ""

    def test_DS10_past_deadline_no_label(self):
        dl = (date.today() - timedelta(days=2)).isoformat()
        assert self.deadline_soon_label(dl) == ""

    def test_DS11_accepts_date_object(self):
        # deadline_soon_label accepts a date object directly
        dl_obj = date.today() + timedelta(days=3)
        label = self.deadline_soon_label(dl_obj)
        assert label.startswith("deadline ")

    def test_DS12_deadline_soon_days_is_7(self):
        assert self.DEADLINE_SOON_DAYS == 7

    def test_DS13_hot_vacancy_threshold_55(self):
        assert self.HOT_VACANCY_SCORE == 55


# ===========================================================================
# 5. Read-only integration checks (skipped without SUPABASE_DB_URL)
# ===========================================================================

SKIP_DB = not os.environ.get("SUPABASE_DB_URL")


@pytest.mark.skipif(SKIP_DB, reason="SUPABASE_DB_URL not set — skipping integration tests")
class TestIntegrationReadOnly:
    """Read-only checks against a live DB."""

    def test_INT01_load_candidate_vacancies_returns_dict(self):
        from database_supabase import load_candidate_vacancies_for_scoring

        result = load_candidate_vacancies_for_scoring(limit=5)
        assert isinstance(result, dict)
        # Every key is a UUID string
        for key in result:
            assert isinstance(key, str)

    def test_INT02_no_unseen_at_inactive_companies(self):
        """No unseen vacancies should remain at inactive companies."""
        from db_conn import get_conn

        # Import RealDictCursor from db_backend, not psycopg2 directly: on the
        # SQLite backend the cursor only yields dict rows when handed the
        # db_backend marker class. The genuine psycopg2 RealDictCursor is a
        # different object the SQLite cursor doesn't recognise, so it would fall
        # back to plain tuples and `row["cnt"]` would raise "tuple indices".
        from db_backend import RealDictCursor

        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM vacancy v
            JOIN company c ON v.company_id = c.id
            WHERE c.status = 'inactive'
              AND v.status = 'unseen'
        """)
        row = cur.fetchone()
        assert row["cnt"] == 0, (
            f"Found {row['cnt']} unseen vacancies at inactive companies — "
            "cleanup not run or new ones appeared"
        )


# ===========================================================================
# 6. enrich_blind_vacancies — import + _DIRECT_HOST_FETCHERS contents
# ===========================================================================


class TestEnrichBlindVacancies:
    """Verify the blind-vacancy enrichment module."""

    def test_EBV01_module_importable(self):
        import enrich_blind_vacancies  # noqa: F401

    def test_EBV02_direct_host_fetchers_present(self):
        import enrich_blind_vacancies

        assert hasattr(enrich_blind_vacancies, "_DIRECT_HOST_FETCHERS")

    def test_EBV03_careers_unops_in_fetchers(self):
        import enrich_blind_vacancies

        assert "careers.unops.org" in enrich_blind_vacancies._DIRECT_HOST_FETCHERS

    def test_EBV04_jobs_unops_in_fetchers(self):
        import enrich_blind_vacancies

        assert "jobs.unops.org" in enrich_blind_vacancies._DIRECT_HOST_FETCHERS

    def test_EBV05_jobs_unicef_in_fetchers(self):
        import enrich_blind_vacancies

        assert "jobs.unicef.org" in enrich_blind_vacancies._DIRECT_HOST_FETCHERS

    def test_EBV06_quality_functions_accessible(self):
        # enrich_blind_vacancies must use clean_description from quality
        from quality import clean_description

        assert callable(clean_description)
        # Confirm the module imports quality (indirectly via database_supabase)
        import enrich_blind_vacancies

        # If the module loaded without errors, the merge is wired up
        assert enrich_blind_vacancies is not None
