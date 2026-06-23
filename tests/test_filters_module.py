"""Unit tests for scripts/filters.py — the consolidated filtering module.

Covers:
  * classify_vacancy reason values and tolerance of a minimal input dict.
  * the pure content predicates do not mutate their input (no side effects).
  * the import-cycle guard: filters must not import the DAL.
"""

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import filters
from config import GLOBAL_BLACKLIST, GLOBAL_BLACKLIST_DESC_SUBSTR


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
    vac = {"title": "Senior Product Manager",
           "full_description": "A real and sufficiently long job description body."}
    assert filters.classify_vacancy(vac) == "ready"


def test_classify_blacklisted_title_is_wrong_role():
    assert GLOBAL_BLACKLIST, "blacklist must be non-empty for this test"
    word = GLOBAL_BLACKLIST[0]
    vac = {"title": f"Senior {word.title()} Specialist",
           "full_description": "Plenty of description text here for the gate."}
    assert filters.classify_vacancy(vac) == "wrong_role"


def test_classify_content_junk_is_not_a_job():
    vac = {"title": "Engineer",
           "full_description": "Please complete the recaptcha to continue now ok"}
    assert filters.is_content_junk(vac["full_description"]) == "recaptcha_only"
    assert filters.classify_vacancy(vac) == "not_a_job"


@pytest.mark.skipif(not GLOBAL_BLACKLIST_DESC_SUBSTR,
                    reason="no description kill phrases configured")
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
# No-mutation guarantee for the pure predicates
# ---------------------------------------------------------------------------

def test_classify_does_not_mutate_input():
    vac = {"title": "Engineer",
           "full_description": "  A real long description with leading space.  ",
           "snippet": "snip", "locations": [{"city": "Berlin"}]}
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
    for forbidden in ("import database_supabase", "from database_supabase",
                      "import db_conn", "from db_conn",
                      "import db_backend", "from db_backend"):
        assert forbidden not in src, f"filters.py must not contain: {forbidden!r}"
