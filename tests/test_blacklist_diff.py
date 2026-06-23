"""Diff-test: prove that the two existing _is_blacklisted copies agree on a
curated corpus.

Two live implementations under test:
  - database_supabase._is_blacklisted  (DAL impl — pre-compiled alternation regex)
  - score_vacancies._is_blacklisted    (scoring impl — per-keyword re.search loop)

Plus a frozen reference copy of the score-impl algorithm. The frozen copy is
inlined here so this test remains valid after the real score-copy is deleted
during the filters-refactor (task B1/B2). The frozen function is intentionally
NOT a mock: it is the exact loop logic from score_vacancies.py:108-119 at the
time this test was written and must never be updated to track score_vacancies.

Categories covered (60 cases total):
  POS  — positive by GLOBAL_BLACKLIST (exact whole-word match on title)
  SUBS — positive by GLOBAL_BLACKLIST_SUBSTR (substring match on title)
  NEG  — clean titles that must not be blacklisted
  ADV  — adversarial: blacklist word embedded inside a longer word (no \b match)
  DSCF — description false-positive guard: title is clean, desc contains a
         title-blacklist word → must NOT fire (only DESC_SUBSTR applies to desc)
  DPOS — positive by GLOBAL_BLACKLIST_DESC_SUBSTR (kill phrase in description)
"""

import re
import sys
import os

import pytest

# ---------------------------------------------------------------------------
# Ensure scripts/ is on the path (mirrors other test files in this suite)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import filters
from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_SUBSTR, GLOBAL_BLACKLIST_DESC_SUBSTR


def filters_impl(title: str, description: str = "") -> bool:
    """The consolidated filters.py implementation, composed exactly as the
    legacy _is_blacklisted(title, desc) did: title words/stems OR desc phrase."""
    return (filters.title_words_blacklisted(title)
            or filters.description_words_blacklisted(description))


# The DAL and score modules no longer expose their own blacklist copies after
# the filters-refactor consolidation — both now delegate to filters.*. The diff
# corpus below is preserved unchanged; the "dal" and "score" probes simply read
# the single filters implementation (the composition the old aliases performed).
dal_impl = filters_impl
score_impl = filters_impl


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
    if any(re.search(r'\b' + re.escape(kw) + r'\b', t) for kw in words):
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
    ("POS-11", "TALENT POOL — ALL CAPS TITLE", ""),         # case insensitivity
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
    ("NEG-11", "Talent Acquisition Specialist", ""),   # 'talent' alone is not in list
    ("NEG-12", "Application Engineer", ""),            # 'application' alone is not
    ("NEG-13", "Network Security Engineer", ""),       # 'network' alone is not
    ("NEG-14", "Future-Focused Policy Analyst", ""),   # 'future' alone is not
    ("NEG-15", "Interest Rate Analyst", ""),           # 'interest' alone is not
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
# Parametrized diff-test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_id,title,description", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_all_three_impls_agree(case_id, title, description):
    """All three implementations must return the identical boolean."""
    dal   = dal_impl(title, description)
    score = score_impl(title, description)
    ref   = frozen(title, description)

    # Report all three values on failure for easy diagnosis
    desc_snip = description[:120]
    assert dal == score == ref, (
        f"[{case_id}] Disagreement: dal={dal}, score={score}, frozen={ref}\n"
        f"  title={title!r}\n  desc={desc_snip!r}"
    )


# ---------------------------------------------------------------------------
# Stage 2 (filters-refactor): the consolidated filters.py implementation must
# match the FROZEN reference for every corpus case. This proves filters ==
# the old _is_blacklisted, independent of the now-aliased dal/score copies.
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


# ---------------------------------------------------------------------------
# Sanity: each category produces expected polarity (catches corpus mistakes)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case_id,title,description", _POS_CASES, ids=[c[0] for c in _POS_CASES])
def test_pos_cases_are_actually_blacklisted(case_id, title, description):
    """POS cases must evaluate to True (blacklisted) under dal_impl."""
    assert dal_impl(title, description) is True, (
        f"[{case_id}] Expected blacklisted but got False for {title!r}"
    )


@pytest.mark.parametrize("case_id,title,description", _NEG_CASES, ids=[c[0] for c in _NEG_CASES])
def test_neg_cases_are_not_blacklisted(case_id, title, description):
    """NEG cases must evaluate to False under dal_impl."""
    assert dal_impl(title, description) is False, (
        f"[{case_id}] Expected clean but got True for {title!r}"
    )


@pytest.mark.parametrize("case_id,title,description", _ADV_CASES, ids=[c[0] for c in _ADV_CASES])
def test_adv_cases_are_not_blacklisted(case_id, title, description):
    """ADV cases must evaluate to False: embedded words must not trigger the filter."""
    assert dal_impl(title, description) is False, (
        f"[{case_id}] Expected clean (adversarial) but got True for {title!r}"
    )


@pytest.mark.parametrize("case_id,title,description", _DSCF_CASES, ids=[c[0] for c in _DSCF_CASES])
def test_dscf_cases_are_not_blacklisted(case_id, title, description):
    """DSCF: clean title + blacklist phrase only in description must NOT fire."""
    desc_snip = description[:120]
    assert dal_impl(title, description) is False, (
        f"[{case_id}] Expected clean (desc false-positive guard) but got True\n"
        f"  title={title!r}\n  desc={desc_snip!r}"
    )


@pytest.mark.skipif(
    not GLOBAL_BLACKLIST_DESC_SUBSTR,
    reason="GLOBAL_BLACKLIST_DESC_SUBSTR is empty in this profile — DPOS cases skipped",
)
@pytest.mark.parametrize("case_id,title,description", _DPOS_CASES, ids=[c[0] for c in _DPOS_CASES])
def test_dpos_cases_are_blacklisted(case_id, title, description):
    """DPOS: kill phrase in description must fire."""
    desc_snip = description[:120]
    assert dal_impl(title, description) is True, (
        f"[{case_id}] Expected blacklisted (desc kill phrase) but got False\n"
        f"  title={title!r}\n  desc={desc_snip!r}"
    )
