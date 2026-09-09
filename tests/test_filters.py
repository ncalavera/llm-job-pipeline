"""Unit tests for scripts/filters.py: classification, blacklist predicates, and
a frozen-reference diff test.

Absorbs, in order:
  * test_filters_module.py — classify_vacancy reason values, the pure content
    predicates' no-mutation guarantee, build_title_blacklist_pattern boundary
    handling, and the import-cycle guard (filters must not import the DAL).
  * test_blacklist.py — title/description blacklist predicates: universal junk
    (dropped for everyone), format/career-stage roles (never junk by default),
    disciplines (not blacklisted by default), false-positive guards, and the
    description-level visa/citizenship kill phrases.
  * test_blacklist_diff.py (trimmed) — a frozen-reference diff test proving the
    consolidated filters.py blacklist logic still matches the pre-refactor
    algorithm, over a POS/NEG/ADV/DSCF/DPOS corpus. Five polarity sanity tests
    that re-asserted the same corpus's polarity (redundant with the diff test
    itself and with test_blacklist.py's classes above) were dropped; see the
    comment above ``dal_impl`` in the corresponding section below.
"""

import copy
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import filters
from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR, GLOBAL_BLACKLIST_DESC_SUBSTR


# ===========================================================================
# --- from test_filters_module.py ---
# ===========================================================================

# ---------------------------------------------------------------------------
# classify_vacancy — reason axis
# ---------------------------------------------------------------------------


def test_classify_minimal_input_does_not_raise():
    """A bare {"title": "X"} (no locations/full_description/status) classifies
    without raising — it has no usable description."""
    assert filters.classify_vacancy({"title": "X"}) == "no_description"


def test_classify_empty_dict_does_not_raise():
    assert filters.classify_vacancy({}) == "no_description"


def test_classify_ready_with_real_description():
    vac = {
        "title": "Senior Product Manager",
        "full_description": "A real and sufficiently long job description body.",
    }
    assert filters.classify_vacancy(vac) == "ready"


def test_classify_blacklisted_title_is_wrong_role():
    assert GLOBAL_BLACKLIST, "blacklist must be non-empty for this test"
    word = GLOBAL_BLACKLIST[0]
    vac = {
        "title": f"Senior {word.title()} Specialist",
        "full_description": "Plenty of description text here for the gate.",
    }
    assert filters.classify_vacancy(vac) == "wrong_role"


def test_classify_content_junk_is_not_a_job():
    vac = {
        "title": "Engineer",
        "full_description": "Please complete the recaptcha to continue now ok",
    }
    assert filters.is_content_junk(vac["full_description"]) == "recaptcha_only"
    assert filters.classify_vacancy(vac) == "not_a_job"


@pytest.mark.skipif(
    not GLOBAL_BLACKLIST_DESC_SUBSTR, reason="no description kill phrases configured"
)
def test_description_kill_phrase_detected_by_predicate():
    """Description kill phrases are detected by the dedicated predicate, NOT by
    classify_vacancy — that gate lives only on the score step."""
    phrase = GLOBAL_BLACKLIST_DESC_SUBSTR[0]
    desc = f"Great team. {phrase}. Apply today and join us soon."
    assert filters.description_words_blacklisted(desc) is True
    # classify_vacancy ignores the description blacklist entirely.
    vac = {"title": "Senior Engineer", "full_description": desc}
    assert filters.classify_vacancy(vac) == "ready"


# ---------------------------------------------------------------------------
# build_title_blacklist_pattern — boundary handling for punctuation keywords
# ---------------------------------------------------------------------------


def test_blacklist_keyword_ending_in_nonword_char_matches():
    """A keyword ending in a non-word character (c++, c#) must still match.

    The old single ``\\b…(?:es|s)?\\b`` wrapper put a word boundary right after
    the keyword; after a "+"/"#" that boundary needs a following WORD char, so
    "C++ Developer" (space after) never matched and the filter was a silent
    no-op."""
    pat = filters.build_title_blacklist_pattern(["c++"])
    assert pat.search("C++ Developer")
    assert pat.search("Senior C++ Engineer")
    assert pat.search("Backend C++")  # keyword at end of the title
    # Still a WHOLE-token match: not a substring of a bigger word.
    assert not pat.search("abc++x developer")

    pat_hash = filters.build_title_blacklist_pattern(["c#"])
    assert pat_hash.search("C# Developer")


def test_blacklist_ordinary_and_plural_keywords_unchanged():
    """The word-ending keywords keep their plural handling (regression guard)."""
    pat = filters.build_title_blacklist_pattern(["engineer", "coach"])
    assert pat.search("Senior Engineer")
    assert pat.search("Engineers wanted")  # plural via (?:es|s)?
    assert pat.search("Head Coaches")  # coach -> coaches
    assert not pat.search("Product Manager")


def test_blacklist_empty_list_matches_nothing():
    """An empty keyword list drops nothing (never an accidental match-all)."""
    pat = filters.build_title_blacklist_pattern([])
    assert not pat.search("Any Title Whatsoever")


# ---------------------------------------------------------------------------
# No-mutation guarantee for the pure predicates
# ---------------------------------------------------------------------------


def test_classify_does_not_mutate_input():
    vac = {
        "title": "Engineer",
        "full_description": "  A real long description with leading space.  ",
        "snippet": "snip",
        "locations": [{"city": "Berlin"}],
    }
    before = copy.deepcopy(vac)
    filters.classify_vacancy(vac)
    assert vac == before


def test_has_enough_content_does_not_mutate_input():
    job = {"full_description": "x" * 80, "snippet": "", "url": ""}
    before = copy.deepcopy(job)
    filters.has_enough_content(job)
    assert job == before


def test_is_recently_archived_does_not_mutate_set():
    archived = {"abc", "def"}
    before = set(archived)
    assert filters.is_recently_archived(archived, "abc") is True
    assert filters.is_recently_archived(archived, "zzz") is False
    assert archived == before


# ---------------------------------------------------------------------------
# Import-cycle guard
# ---------------------------------------------------------------------------


def test_filters_does_not_import_dal():
    """filters.py must not import the data-access layer, so the DAL can import
    it without a cycle."""
    src_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "filters.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    for forbidden in (
        "import database_supabase",
        "from database_supabase",
        "import db_conn",
        "from db_conn",
        "import db_backend",
        "from db_backend",
    ):
        assert forbidden not in src, f"filters.py must not contain: {forbidden!r}"


# ===========================================================================
# --- from test_blacklist.py ---
#
# Pure logic, no DB.
#
# The default pre-score blacklist is UNIVERSAL_JUNK only — speculative / evergreen
# pipeline postings nobody wants on any job search (talent pools, expressions of
# interest, general/open applications) plus visa/citizenship kill phrases in the
# description. A specific job DISCIPLINE, format or career stage is NEVER dropped
# by default; that is a PERSONAL choice expressed via ``exclude_title_keywords``
# in the profile's ``## HARD_FILTERS`` section. The two test classes below prove
# that contract: with an empty profile a discipline title survives; with the
# keyword listed it is dropped.
# ===========================================================================


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


# ===========================================================================
# --- from test_blacklist_diff.py (trimmed) ---
#
# Diff-test: prove the consolidated filters.py blacklist logic still matches
# the pre-refactor algorithm on a curated corpus.
#
# database_supabase.py and score_vacancies.py used to each carry their own
# ``_is_blacklisted`` copy; both now delegate to ``filters.py``. The real diff
# is against a frozen reference copy of the original algorithm, inlined here
# so it stays valid even if filters.py's internals change shape. The frozen
# function is intentionally NOT a mock: it is the exact loop logic from
# score_vacancies.py:108-119 at the time this test was written and must never
# be updated to track score_vacancies or filters.
#
# Categories covered (60 cases total):
#   POS  — positive by GLOBAL_BLACKLIST (exact whole-word match on title)
#   SUBS — positive by GLOBAL_BLACKLIST_SUBSTR (substring match on title)
#   NEG  — clean titles that must not be blacklisted
#   ADV  — adversarial: blacklist word embedded inside a longer word (no \b match)
#   DSCF — description false-positive guard: title is clean, desc contains a
#          title-blacklist word → must NOT fire (only DESC_SUBSTR applies to desc)
#   DPOS — positive by GLOBAL_BLACKLIST_DESC_SUBSTR (kill phrase in description)
#
# TRIM (2026-08-26): five polarity sanity tests that used to live here
# (test_pos_cases_are_actually_blacklisted, test_neg_cases_are_not_blacklisted,
# test_adv_cases_are_not_blacklisted, test_dscf_cases_are_not_blacklisted,
# test_dpos_cases_are_blacklisted) were dropped, along with the ``dal_impl``
# alias they called through. They re-asserted polarity on the same corpus that
# test_filters_matches_frozen_reference below already exercises against
# production code, and TestUniversalJunk / TestFormatRolesNotJunk /
# TestDisciplinesNotBlacklistedByDefault / TestNegativeNoFalsePositives above
# cover the same contract directly against filters.py with an independent
# corpus. A former ``test_all_three_impls_agree`` (diffing a ``score_impl``
# alias against this frozen reference) was removed earlier still, before this
# merge, once the filters-refactor made ``dal_impl``/``score_impl`` the same
# object as ``filters_impl`` — that check had reduced to a tautology.
# ===========================================================================


def filters_impl(title: str, description: str = "") -> bool:
    """The consolidated filters.py implementation, composed exactly as the
    legacy _is_blacklisted(title, desc) did: title words/stems OR desc phrase."""
    return filters.title_words_blacklisted(title) or filters.description_words_blacklisted(
        description
    )


# ---------------------------------------------------------------------------
# Frozen reference implementation (exact copy of score_vacancies.py:108-119)
# Do NOT edit this function to track future changes in score_vacancies.py.
# ---------------------------------------------------------------------------


def _frozen_score_impl(
    title: str,
    desc: str,
    words: list,
    substr: list,
    desc_substr: list,
) -> bool:
    """Frozen reference: score_vacancies._is_blacklisted logic as of filters-refactor baseline."""
    t = title.lower()
    if any(kw in t for kw in substr):
        return True
    if any(re.search(r"\b" + re.escape(kw) + r"\b", t) for kw in words):
        return True
    if desc:
        d = desc.lower()
        if any(kw in d for kw in desc_substr):
            return True
    return False


# ---------------------------------------------------------------------------
# Helper: wrap frozen impl with runtime config (called once per param case)
# ---------------------------------------------------------------------------


def frozen(title: str, description: str = "") -> bool:
    return _frozen_score_impl(
        title,
        description,
        GLOBAL_BLACKLIST,
        GLOBAL_BLACKLIST_SUBSTR,
        GLOBAL_BLACKLIST_DESC_SUBSTR,
    )


# ---------------------------------------------------------------------------
# Corpus
#
# Each entry: (category_id, title, description)
# All three impls must agree on the result for every entry.
# ---------------------------------------------------------------------------

# --- POS: positive by GLOBAL_BLACKLIST whole-word ---
# Words actually present in config at test-write time (verified via config dump):
#   'expression of interest', 'talent pool', 'general application',
#   'talent community', 'speculative application', 'future opportunities',
#   'open application', 'talent network', 'join our talent'
_POS_CASES = [
    ("POS-01", "Expression of Interest — Global Programmes", ""),
    ("POS-02", "Talent Pool: Future Engineering Roles", ""),
    ("POS-03", "General Application", ""),
    ("POS-04", "Open Application — Any Team", ""),
    ("POS-05", "Join Our Talent Community", ""),
    ("POS-06", "Speculative Application for Finance", ""),
    ("POS-07", "Future Opportunities at OpenAI", ""),
    ("POS-08", "Talent Network Sign-up", ""),
    ("POS-09", "Join Our Talent Pipeline", ""),
    ("POS-10", "expression of interest (lowercase entire title)", ""),
    ("POS-11", "TALENT POOL — ALL CAPS TITLE", ""),  # case insensitivity
    ("POS-12", "Talent Community Manager (title contains phrase)", ""),
    ("POS-13", "Interest in a Talent Pool Registration", ""),
    ("POS-14", "General Application — Product, Growth, Ops", ""),
    ("POS-15", "Join Our Talent — Engineering Track", ""),
]

# --- SUBS: positive by GLOBAL_BLACKLIST_SUBSTR (substring, no word boundary) ---
# GLOBAL_BLACKLIST_SUBSTR is empty in the example profile (only UNIVERSAL_JUNK_SUBSTR
# which ships empty by default). We build cases only if the list is non-empty at
# runtime; otherwise this category is skipped with a clear message.
_SUBS_CASES_TEMPLATE = []  # populated dynamically below

# --- NEG: clean titles — must NOT be blacklisted ---
_NEG_CASES = [
    ("NEG-01", "Senior Product Manager", ""),
    ("NEG-02", "Backend Software Engineer", ""),
    ("NEG-03", "Head of Operations", ""),
    ("NEG-04", "Chief of Staff", ""),
    ("NEG-05", "Director of Strategy", ""),
    ("NEG-06", "Research Lead, AI Safety", ""),
    ("NEG-07", "Community Engagement Manager", ""),  # 'community' alone is not in list
    ("NEG-08", "Volunteer Coordinator", ""),
    ("NEG-09", "Research Fellowship Program", ""),
    ("NEG-10", "Summer Internship — Data Science", ""),
    ("NEG-11", "Talent Acquisition Specialist", ""),  # 'talent' alone is not in list
    ("NEG-12", "Application Engineer", ""),  # 'application' alone is not
    ("NEG-13", "Network Security Engineer", ""),  # 'network' alone is not
    ("NEG-14", "Future-Focused Policy Analyst", ""),  # 'future' alone is not
    ("NEG-15", "Interest Rate Analyst", ""),  # 'interest' alone is not
]

# --- ADV: adversarial — blacklist word embedded inside a larger word ---
# Purpose: verify that \b word-boundary prevents substring-inside-word false positives.
# e.g. "talent" is in GLOBAL_BLACKLIST as part of multi-word phrases, but checking
# purely for "pool" — let's use the actual phrases. The real adversarial case is
# when a single token from the multi-word phrase appears as part of another word.
# Since GLOBAL_BLACKLIST contains multi-word phrases we test a different angle:
# embedding the full phrase (or a key word) inside a longer compound.
_ADV_CASES = [
    # A clean title that incidentally contains the word "pool" — not the phrase "talent pool"
    ("ADV-01", "Swimming Pool Facility Manager", ""),
    # "opportunity" is close to "future opportunities" but is not the phrase
    ("ADV-02", "Growth Opportunity Analyst", ""),
    # "application" by itself — present in "general application" but not alone
    ("ADV-03", "Application Security Engineer", ""),
    # "network" alone — present in "talent network" phrase but not alone
    ("ADV-04", "Network Operations Center Lead", ""),
    # "interest" alone — present in "expression of interest" but not alone
    ("ADV-05", "Community Interest Company Operations", ""),
    # Phrase split across word boundary: "talentpool" (no space) must not fire
    ("ADV-06", "TalentPool Product Lead", ""),
    # "generalapplication" as one word
    ("ADV-07", "Generalapplication Engineer", ""),
    # Misspelled / partial match — "Expresion of Interest" (one s) — should NOT fire
    ("ADV-08", "Expresion of Interest Coordinator", ""),
    # "talent" in compound: "multi-talented" — must not fire
    ("ADV-09", "Multi-talented Creative Director", ""),
    # Partial overlap: "open" + "application" separated by 2 words
    ("ADV-10", "Open Source Application Framework Engineer", ""),
]

# --- DSCF: description false-positive guard ---
# title is clean; description contains a word that appears in GLOBAL_BLACKLIST
# (as a phrase). Since title-blacklist is only applied to the title, these
# must NOT be blacklisted. Only GLOBAL_BLACKLIST_DESC_SUBSTR applies to description.
_DSCF_CASES = [
    (
        "DSCF-01",
        "Product Manager",
        "We are a talent pool of researchers working on AI Safety topics.",
    ),
    (
        "DSCF-02",
        "Data Scientist",
        "Submit a general application through our portal and we'll reach out.",
    ),
    (
        "DSCF-03",
        "Policy Analyst",
        "This role is open to anyone interested in future opportunities in policy.",
    ),
    (
        "DSCF-04",
        "Software Engineer",
        "Expression of interest forms will be reviewed separately.",
    ),
    (
        "DSCF-05",
        "Operations Lead",
        "Join our talent community newsletter for updates.",  # phrase in desc, not title
    ),
    (
        "DSCF-06",
        "Head of Research",
        "We run speculative application reviews annually.",
    ),
    (
        "DSCF-07",
        "AI Safety Researcher",
        # Anthropic-style JD mentioning ai safety in body — must not fire
        "At Anthropic we work on ai safety and alignment research.",
    ),
    (
        "DSCF-08",
        "Communications Manager",
        "Open application window for volunteer co-leads closes Friday.",
    ),
    (
        "DSCF-09",
        "Program Manager",
        "talent network is one channel through which we recruit.",
    ),
    (
        "DSCF-10",
        "Growth Manager",
        "talent pool submissions are reviewed quarterly.",
    ),
]

# --- DPOS: positive by GLOBAL_BLACKLIST_DESC_SUBSTR (kill phrase in description) ---
# Kill phrases at test-write time (from config dump):
#   'visa sponsorship not available', 'must be a us citizen', 'must be us citizen'
_DPOS_CASES = [
    (
        "DPOS-01",
        "Senior Product Manager",
        "Please note: visa sponsorship not available for this position.",
    ),
    (
        "DPOS-02",
        "Director of Engineering",
        "Applicants must be a US citizen or permanent resident.",
    ),
    (
        "DPOS-03",
        "Policy Analyst",
        "Due to clearance requirements, must be us citizen at time of hire.",
    ),
    (
        "DPOS-04",
        "Research Scientist",
        # Uppercase variant — substring match is case-insensitive (both impls lower() first)
        "VISA SPONSORSHIP NOT AVAILABLE. This is a US-only position.",
    ),
    (
        "DPOS-05",
        "Software Engineer",
        "Note: Must Be A US Citizen. We cannot sponsor visas.",
    ),
]


# ---------------------------------------------------------------------------
# Build final parametrized list
# ---------------------------------------------------------------------------


def _make_corpus():
    corpus = []
    # POS
    corpus.extend(_POS_CASES)
    # SUBS — only if the list is non-empty at runtime
    if GLOBAL_BLACKLIST_SUBSTR:
        for kw in GLOBAL_BLACKLIST_SUBSTR[:3]:  # sample up to 3
            corpus.append((f"SUBS-{kw[:12]}", f"Lead role with {kw} focus", ""))
    # NEG
    corpus.extend(_NEG_CASES)
    # ADV
    corpus.extend(_ADV_CASES)
    # DSCF
    corpus.extend(_DSCF_CASES)
    # DPOS — only if DESC_SUBSTR list is non-empty
    if GLOBAL_BLACKLIST_DESC_SUBSTR:
        corpus.extend(_DPOS_CASES)
    return corpus


_CORPUS = _make_corpus()


# ---------------------------------------------------------------------------
# Parametrized diff-test: the consolidated filters.py implementation must
# match the FROZEN reference for every corpus case. This proves filters ==
# the old _is_blacklisted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,title,description", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_filters_matches_frozen_reference(case_id, title, description):
    """filters.title_words_blacklisted OR description_words_blacklisted must
    return exactly what the frozen legacy _is_blacklisted returned."""
    got = filters_impl(title, description)
    ref = frozen(title, description)
    desc_snip = description[:120]
    assert got == ref, (
        f"[{case_id}] filters != frozen: filters={got}, frozen={ref}\n"
        f"  title={title!r}\n  desc={desc_snip!r}"
    )
