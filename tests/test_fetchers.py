"""Tests for parse_markdown_jobs URL filtering (non-job URL exclusion)
and _parse_json_jobs url_filter behavior.
"""

import pytest
from fetchers import parse_markdown_jobs, _is_non_job_url, _parse_json_jobs


# ---------------------------------------------------------------------------
# _is_non_job_url — unit tests
# ---------------------------------------------------------------------------


class TestIsNonJobUrl:
    """Test the _is_non_job_url helper directly."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://acme.org/about/#partners",
            "https://acme.org/about/#advisory-board",
            "https://acme.org/about/",
            "https://example.org/about/our-story",
            "https://example.org/team/",
            "https://example.org/team/#leadership",
            "https://example.org/contact/",
            "https://example.org/privacy/",
            "https://example.org/donate/",
            "https://example.org/faq/",
            "https://example.org/board/members",
        ],
    )
    def test_rejects_non_job_urls(self, url):
        assert _is_non_job_url(url) is True, f"Expected {url} to be rejected"

    @pytest.mark.parametrize(
        "url",
        [
            "https://acme.org/careers/research-analyst",
            "https://acme.org/vacancies/program-manager",
            "https://example.org/jobs/senior-engineer",
            "https://boards.greenhouse.io/company/jobs/123",
            "https://example.org/opportunities/head-of-community",
            "https://governance.ai/post/summer-fellowship",
            "https://example.org/work-with-us",
        ],
    )
    def test_allows_job_urls(self, url):
        assert _is_non_job_url(url) is False, f"Expected {url} to be allowed"

    def test_rejects_fragment_only_partners(self):
        assert _is_non_job_url("https://example.org/about/#partners") is True

    def test_rejects_fragment_only_advisory_board(self):
        assert _is_non_job_url("https://example.org/about/#advisory-board") is True

    def test_allows_url_with_job_fragment(self):
        """A fragment like #apply or #position should not be rejected."""
        assert _is_non_job_url("https://example.org/careers/#apply") is False


# ---------------------------------------------------------------------------
# parse_markdown_jobs — integration tests for URL filtering
# ---------------------------------------------------------------------------


class TestParseMarkdownJobsUrlFiltering:
    """Test that parse_markdown_jobs excludes /about/ page links."""

    def _make_markdown_with_link(self, title, url, snippet_text=None):
        """Helper: create markdown with a link that looks like a job listing."""
        if snippet_text is None:
            snippet_text = (
                "This is a long enough snippet to pass the 50 character minimum "
                "that the parser requires for valid job entries in markdown parsing."
            )
        return f"[{title}]({url})\n\n{snippet_text}\n"

    def test_excludes_about_partners(self):
        md = self._make_markdown_with_link("Partners", "https://acme.org/about/#partners")
        jobs = parse_markdown_jobs(md, "Acme Foundation")
        assert len(jobs) == 0

    def test_excludes_about_advisory_board(self):
        md = self._make_markdown_with_link(
            "Advisory Board", "https://acme.org/about/#advisory-board"
        )
        jobs = parse_markdown_jobs(md, "Acme Foundation")
        assert len(jobs) == 0

    def test_keeps_legitimate_job_url(self):
        md = self._make_markdown_with_link(
            "Research Analyst",
            "https://acme.org/careers/research-analyst",
        )
        jobs = parse_markdown_jobs(md, "Acme Foundation")
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Research Analyst"

    def test_excludes_about_page_in_mixed_markdown(self):
        """When markdown has both real jobs and /about/ links, only jobs survive."""
        md = (
            "[Research Analyst](https://acme.org/careers/research-analyst)\n\n"
            "Join our team to conduct impactful research on global catastrophic risks "
            "and help shape policy recommendations for decision-makers worldwide.\n\n"
            "[Partners](https://acme.org/about/#partners)\n\n"
            "We work with leading organizations in the effective altruism community "
            "and global catastrophic risk reduction to maximize our collective impact.\n\n"
            "[Advisory Board](https://acme.org/about/#advisory-board)\n\n"
            "Our advisory board includes distinguished researchers and policy experts "
            "who guide our strategic direction and ensure research quality standards.\n"
        )
        jobs = parse_markdown_jobs(md, "Acme Foundation")
        titles = [j["title"] for j in jobs]
        assert "Research Analyst" in titles
        assert "Partners" not in titles
        assert "Advisory Board" not in titles

    def test_excludes_team_page_url(self):
        md = self._make_markdown_with_link(
            "Head of Operations", "https://example.org/team/leadership"
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 0

    def test_heading_pattern_excludes_about_urls(self):
        """Heading-pattern branch also filters out /about/ URLs."""
        md = (
            "## Program Director\n\n"
            "Lead our grantmaking programs and manage a portfolio of grants.\n\n"
            "[Learn more](https://acme.org/about/#partners)\n\n"
            "## Research Analyst\n\n"
            "Conduct research on existential risks and help shape our strategy.\n\n"
            "[Apply here](https://acme.org/careers/research-analyst)\n"
        )
        jobs = parse_markdown_jobs(md, "Acme Foundation")
        # Program Director's only link goes to /about/ — should have no URL
        # Research Analyst links to /careers/ — should have a URL
        research_jobs = [j for j in jobs if j["title"] == "Research Analyst"]
        assert len(research_jobs) == 1
        assert "careers" in research_jobs[0]["url"]


# ---------------------------------------------------------------------------
# _parse_json_jobs — non-job URL filtering
# ---------------------------------------------------------------------------


class TestParseJsonJobsUrlFiltering:
    """Test that _parse_json_jobs also excludes non-job URLs."""

    def _make_json_data(self, jobs_list):
        return {"jobs": jobs_list}

    def test_excludes_about_page_url(self):
        data = self._make_json_data(
            [
                {
                    "title": "Partners",
                    "url": "https://acme.org/about/#partners",
                    "location": "",
                    "department": "",
                    "snippet": "",
                },
            ]
        )
        jobs = _parse_json_jobs(data, "Acme Foundation", "https://acme.org")
        assert len(jobs) == 0

    def test_keeps_legitimate_job_url(self):
        data = self._make_json_data(
            [
                {
                    "title": "Research Analyst",
                    "url": "https://acme.org/careers/analyst",
                    "location": "London",
                    "department": "Research",
                    "snippet": "Conduct research.",
                },
            ]
        )
        jobs = _parse_json_jobs(data, "Acme Foundation", "https://acme.org")
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Research Analyst"

    def test_filters_mixed_json_jobs(self):
        data = self._make_json_data(
            [
                {
                    "title": "Research Analyst",
                    "url": "https://acme.org/careers/analyst",
                    "location": "",
                    "department": "",
                    "snippet": "",
                },
                {
                    "title": "Advisory Board",
                    "url": "https://acme.org/about/#advisory-board",
                    "location": "",
                    "department": "",
                    "snippet": "",
                },
                {
                    "title": "Head of Operations",
                    "url": "https://acme.org/team/ops",
                    "location": "",
                    "department": "",
                    "snippet": "",
                },
            ]
        )
        jobs = _parse_json_jobs(data, "Acme Foundation", "https://acme.org")
        titles = [j["title"] for j in jobs]
        assert "Research Analyst" in titles
        assert "Advisory Board" not in titles
        assert "Head of Operations" not in titles


# ---------------------------------------------------------------------------
# GovAI url_filter tests
# External links (stripe.com, lever.co) must be excluded when url_filter
# is set to governance.ai/post/ pattern
# ---------------------------------------------------------------------------

GOVAI_MARKDOWN_INLINE = """\
Check out these opportunities:

[Head of Community](https://www.governance.ai/post/head-of-community) — Lead engagement efforts for our growing community of AI governance researchers and practitioners.

[Product Manager](https://stripe.com/jobs/product-manager) — A great role at Stripe for payments infrastructure with competitive compensation and benefits package.

[Research Associate](https://www.governance.ai/post/research-associate-2026) — An intensive 8-week fellowship for researchers focused on AI policy and governance topics.

[Senior Engineer](https://jobs.lever.co/some-company/senior-engineer) — Join a fast-growing startup to build infrastructure for AI safety research and development teams.
"""

GOVAI_MARKDOWN_HEADINGS = """\
# Opportunities

## Head of Community

We are looking for a Head of Community to lead engagement efforts for our growing community of AI governance researchers.

[Read more](https://www.governance.ai/post/head-of-community)

## Research Associate — Applied Track

An intensive 8-week fellowship for researchers focused on AI policy and governance, working alongside senior researchers.

[Read more](https://www.governance.ai/post/research-associate-2026)

## Product Manager

A leading fintech company is hiring a Product Manager for payments infrastructure and platform scalability improvements.

[Read more](https://stripe.com/jobs/product-manager)

## Senior Engineer

Join a fast-growing startup to build infrastructure for AI safety research and development teams worldwide.

[Read more](https://jobs.lever.co/some-company/senior-engineer)
"""


class TestGovAIMarkdownUrlFilter:
    """Tests for url_filter in parse_markdown_jobs (inline link pattern)."""

    def test_GV01_no_filter_captures_all_links(self):
        """Without url_filter, all links with job-like titles are captured."""
        jobs = parse_markdown_jobs(GOVAI_MARKDOWN_INLINE, "GovAI")
        urls = [j["url"] for j in jobs]
        assert any("governance.ai" in u for u in urls)
        assert any("stripe.com" in u for u in urls)

    def test_GV02_filter_keeps_only_matching_urls(self):
        """With url_filter, only governance.ai/post/ links are kept."""
        jobs = parse_markdown_jobs(
            GOVAI_MARKDOWN_INLINE, "GovAI", url_filter=r"governance\.ai/post/"
        )
        assert len(jobs) >= 2
        for job in jobs:
            assert "governance.ai/post/" in job["url"], f"External link leaked: {job['url']}"

    def test_GV03_filter_rejects_stripe(self):
        """stripe.com links are excluded by governance.ai filter."""
        jobs = parse_markdown_jobs(
            GOVAI_MARKDOWN_INLINE, "GovAI", url_filter=r"governance\.ai/post/"
        )
        urls = [j["url"] for j in jobs]
        assert not any("stripe.com" in u for u in urls)

    def test_GV04_filter_rejects_lever(self):
        """lever.co links are excluded by governance.ai filter."""
        jobs = parse_markdown_jobs(
            GOVAI_MARKDOWN_INLINE, "GovAI", url_filter=r"governance\.ai/post/"
        )
        urls = [j["url"] for j in jobs]
        assert not any("lever.co" in u for u in urls)


class TestGovAIHeadingUrlFilter:
    """Tests for url_filter in heading+lookahead pattern (GovAI/Webflow)."""

    def test_GH01_heading_pattern_with_filter(self):
        """With filter, heading pattern only captures governance.ai/post/ links."""
        jobs = parse_markdown_jobs(
            GOVAI_MARKDOWN_HEADINGS, "GovAI", url_filter=r"governance\.ai/post/"
        )
        for job in jobs:
            if job["url"]:
                assert "governance.ai/post/" in job["url"], (
                    f"External link leaked in heading pattern: {job['url']}"
                )

    def test_GH02_no_external_urls_in_heading_jobs(self):
        """No stripe.com or lever.co URLs in heading-pattern results."""
        jobs = parse_markdown_jobs(
            GOVAI_MARKDOWN_HEADINGS, "GovAI", url_filter=r"governance\.ai/post/"
        )
        urls = [j["url"] for j in jobs if j["url"]]
        assert not any("stripe.com" in u for u in urls)
        assert not any("lever.co" in u for u in urls)


class TestParseJsonJobsGovAIUrlFilter:
    """Tests for url_filter in _parse_json_jobs (JSON extraction path).

    Root cause being guarded: _parse_json_jobs had no url_filter parameter,
    so the JSON extraction path always accepted external URLs.
    """

    SAMPLE_JSON = {
        "jobs": [
            {
                "title": "Head of Community",
                "url": "https://www.governance.ai/post/head-of-community",
                "location": "Oxford, UK",
                "department": "",
                "snippet": "Lead community engagement for AI governance.",
            },
            {
                "title": "Product Manager",
                "url": "https://stripe.com/jobs/product-manager",
                "location": "San Francisco",
                "department": "Product",
                "snippet": "Build payments infrastructure at scale.",
            },
            {
                "title": "Research Associate",
                "url": "https://www.governance.ai/post/research-associate-2026",
                "location": "Oxford, UK",
                "department": "Research",
                "snippet": "Conduct applied research on AI governance policy.",
            },
            {
                "title": "Senior Engineer",
                "url": "https://jobs.lever.co/some-company/senior-engineer",
                "location": "Remote",
                "department": "Engineering",
                "snippet": "Build infrastructure for AI safety research.",
            },
        ]
    }

    def test_PJ01_no_filter_returns_all(self):
        """Without url_filter, all jobs with valid titles are returned."""
        jobs = _parse_json_jobs(
            self.SAMPLE_JSON, "GovAI", "https://www.governance.ai/opportunities"
        )
        assert len(jobs) == 4

    def test_PJ02_filter_keeps_only_matching(self):
        """With url_filter, only governance.ai/post/ URLs are kept."""
        jobs = _parse_json_jobs(
            self.SAMPLE_JSON,
            "GovAI",
            "https://www.governance.ai/opportunities",
            url_filter=r"governance\.ai/post/",
        )
        assert len(jobs) == 2
        urls = [j["url"] for j in jobs]
        assert all("governance.ai/post/" in u for u in urls)

    def test_PJ03_filter_rejects_stripe(self):
        """stripe.com URL is excluded by governance.ai filter."""
        jobs = _parse_json_jobs(
            self.SAMPLE_JSON,
            "GovAI",
            "https://www.governance.ai/opportunities",
            url_filter=r"governance\.ai/post/",
        )
        urls = [j["url"] for j in jobs]
        assert not any("stripe.com" in u for u in urls)

    def test_PJ04_filter_rejects_lever(self):
        """lever.co URL is excluded by governance.ai filter."""
        jobs = _parse_json_jobs(
            self.SAMPLE_JSON,
            "GovAI",
            "https://www.governance.ai/opportunities",
            url_filter=r"governance\.ai/post/",
        )
        urls = [j["url"] for j in jobs]
        assert not any("lever.co" in u for u in urls)

    def test_PJ05_empty_url_kept_when_filter_set(self):
        """Jobs with empty URLs are kept even when url_filter is set."""
        json_data = {
            "jobs": [
                {
                    "title": "Research Associate",
                    "url": "",
                    "location": "Oxford",
                    "department": "",
                    "snippet": "Research role in AI governance.",
                },
            ]
        }
        jobs = _parse_json_jobs(
            json_data,
            "GovAI",
            "https://www.governance.ai/opportunities",
            url_filter=r"governance\.ai/post/",
        )
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Research Associate"

    def test_PJ06_relative_url_resolved_then_filtered(self):
        """Relative URLs are resolved before filtering."""
        json_data = {
            "jobs": [
                {
                    "title": "Research Director",
                    "url": "/post/research-director",
                    "location": "Oxford",
                    "department": "",
                    "snippet": "Lead research at GovAI.",
                },
            ]
        }
        jobs = _parse_json_jobs(
            json_data,
            "GovAI",
            "https://www.governance.ai/opportunities",
            url_filter=r"governance\.ai/post/",
        )
        assert len(jobs) == 1
        assert "governance.ai/post/research-director" in jobs[0]["url"]


# ---------------------------------------------------------------------------
# parse_markdown_jobs — department heading rejection
# ---------------------------------------------------------------------------


class TestParseMarkdownJobsRejectsDeptHeadings:
    """A careers homepage fetcher grabbed dept-heading aggregations like
    'Senior Director's Office, Chief Financial Officer (1)' as pseudo-vacancies.
    Two parser-level guards: trailing-count suffix in title, and a
    careers-homepage boilerplate signature in snippet.
    """

    LONG_SNIPPET = (
        "Snippet long enough to surpass the fifty character minimum that "
        "parse_markdown_jobs enforces for valid job entries."
    )

    def test_rejects_dept_heading_single_count_suffix(self):
        md = (
            "[Senior Director's Office, Chief Financial Officer (1)]"
            "(https://careers.example.org/vacancy/find/results/)\n\n"
            f"{self.LONG_SNIPPET}\n"
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 0

    def test_rejects_dept_heading_double_digit_count_suffix(self):
        md = (
            "[Programmes (12)](https://careers.example.org/vacancy/find/results/)\n\n"
            f"{self.LONG_SNIPPET}\n"
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 0

    def test_keeps_real_role_without_count_suffix(self):
        md = (
            "[Programme Coordinator]"
            "(https://careers.example.org/vacancy/find/desc/12345/programme-coordinator)\n\n"
            "We seek a Programme Coordinator with experience in human rights "
            "research and advocacy across our regional offices in Europe."
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Programme Coordinator"

    def test_rejects_careers_homepage_boilerplate_in_snippet(self):
        md = (
            "[Programme Coordinator]"
            "(https://careers.example.org/vacancy/find/desc/12345/programme-coordinator)\n\n"
            "FREEDOM, JUSTICE, EQUALITY LET'S GET TO WORK Search login register "
            "Career FAQs apply now navigation links and login form placeholders.\n"
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 0

    def test_count_suffix_check_is_case_insensitive_and_stripped(self):
        """Whitespace before the (N) suffix should still trigger rejection."""
        md = (
            "[Communications  (3)](https://careers.example.org/vacancy/find/results/)\n\n"
            f"{self.LONG_SNIPPET}\n"
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 0

    def test_keeps_role_with_year_in_parens_not_count(self):
        """Title containing '(2026)' should NOT be confused with a count suffix.
        Real-world example: cohort years. Distinguish 1-2-digit count from
        4-digit year.
        """
        md = (
            "[Research Director (2026)]"
            "(https://example.org/jobs/research-director-2026)\n\n"
            "We seek a Research Director for our 2026 cohort working on policy "
            "analysis across multiple countries with a strong track record."
        )
        jobs = parse_markdown_jobs(md, "Example Org")
        assert len(jobs) == 1, (
            "Year-in-parens should not be confused with dept count suffix "
            "(implementation must distinguish (1)/(12) from (2026))"
        )
