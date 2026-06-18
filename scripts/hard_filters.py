"""Personal HARD filters, loaded from the user profile.

HARD filters are deterministic, pre-score row drops driven entirely by the
USER's profile. They are EMPTY by default, so a fresh clone drops nothing
personal before scoring.

Two kinds:
  - exclude_countries     — drop a vacancy when EVERY location is in one of
                            these countries (geography exclusion).
  - exclude_title_keywords — drop a vacancy whose TITLE contains one of these
                            words, matched on word boundaries.

Read them from the ``## HARD_FILTERS`` section of the profile file pointed to by
USER_PROFILE_PATH (default: config/user_profile.md, falling back to the bundled
example). The section format is two comma-separated fields::

    ## HARD_FILTERS
    exclude_countries: united states, canada
    exclude_title_keywords: engineer, developer

A field value of ``(none)`` (or empty) means "no filter". This module reuses
``prompts._load_user_profile`` so it sees exactly the same profile file as the
scoring prompts.
"""

from __future__ import annotations

import re

# Reuse the single profile reader so HARD filters and the LLM prompts always
# read the same file. prompts.py lives next to this module on sys.path.
from prompts import _load_user_profile

# Values that mean "the user left this filter empty".
_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}

# Strip HTML comment blocks (<!-- ... -->) so example lines inside the template's
# explanatory comment are never parsed as real filter values.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated field into a clean, lowercased list.

    Returns [] when the value is empty or a "(none)" placeholder.
    """
    if not value:
        return []
    items: list[str] = []
    for raw in value.split(","):
        token = raw.strip().lower()
        if token and token not in _EMPTY_TOKENS:
            items.append(token)
    return items


def _parse_section(body: str) -> dict[str, list[str]]:
    """Parse a HARD_FILTERS section body into the two filter lists."""
    body = _HTML_COMMENT.sub("", body or "")
    countries: list[str] = []
    title_keywords: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("exclude_countries:"):
            countries = _split_csv(stripped.split(":", 1)[1])
        elif low.startswith("exclude_title_keywords:"):
            title_keywords = _split_csv(stripped.split(":", 1)[1])
    return {
        "exclude_countries": countries,
        "exclude_title_keywords": title_keywords,
    }


def load_hard_filters() -> dict[str, list[str]]:
    """Return the user's HARD filters from the active profile.

    Always returns ``{"exclude_countries": [...], "exclude_title_keywords":
    [...]}`` with empty lists when the profile, the section, or a field is
    absent or set to ``(none)``. Never raises — a missing/broken profile yields
    empty filters (drop nothing) so the pipeline still runs out of the box.
    """
    empty = {"exclude_countries": [], "exclude_title_keywords": []}
    try:
        sections = _load_user_profile()
    except Exception:
        return empty
    body = sections.get("HARD_FILTERS")
    if not body:
        return empty
    return _parse_section(body)
