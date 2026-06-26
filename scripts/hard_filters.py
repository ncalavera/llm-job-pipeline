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


_TRUE_TOKENS = {"yes", "true", "1", "on", "y"}


def _parse_bool(value: str, default: bool = False) -> bool:
    """Parse a yes/no field. Empty/placeholder → default."""
    token = (value or "").strip().lower()
    if token in _EMPTY_TOKENS:
        return default
    return token in _TRUE_TOKENS


def _parse_int(value: str, default: int = 0) -> int:
    """Parse an integer field. Empty/placeholder/garbage → default."""
    token = (value or "").strip()
    if token.lower() in _EMPTY_TOKENS:
        return default
    try:
        return int(token)
    except ValueError:
        return default


# Default geo policy when the profile says nothing: drop nothing, penalise
# nothing. A fresh clone stays geography-neutral until configured in the profile.
_GEO_DEFAULTS: dict = {
    "exclude_countries": [],
    "exclude_title_keywords": [],
    "ban_regions": [],
    "keep_countries": [],
    "ban_countries": [],
    "ban_us_only": False,
    "onsite_ok_regions": [],
    "onsite_penalty": 0,
}


def _parse_section(body: str) -> dict:
    """Parse a HARD_FILTERS section body into the filter lists + geo policy."""
    body = _HTML_COMMENT.sub("", body or "")
    out = dict(_GEO_DEFAULTS)
    # field name → kind: "csv" | "bool" | "int".
    fields = {
        "exclude_countries": "csv",
        "exclude_title_keywords": "csv",
        "ban_regions": "csv",
        "keep_countries": "csv",
        "ban_countries": "csv",
        "ban_us_only": "bool",
        "onsite_ok_regions": "csv",
        "onsite_penalty": "int",
    }
    for line in body.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        for name, kind in fields.items():
            if low.startswith(name + ":"):
                raw = stripped.split(":", 1)[1]
                if kind == "csv":
                    out[name] = _split_csv(raw)
                elif kind == "bool":
                    out[name] = _parse_bool(raw)
                else:
                    out[name] = _parse_int(raw)
                break
    return out


def load_hard_filters() -> dict:
    """Return the user's HARD filters + geo policy from the active profile.

    Always returns the full geo-policy dict (see ``_GEO_DEFAULTS``) with neutral
    defaults when the profile, the section, or a field is absent or ``(none)``.
    Never raises — a missing/broken profile yields neutral filters (drop and
    penalise nothing) so the pipeline still runs out of the box.
    """
    try:
        sections = _load_user_profile()
    except Exception:
        return dict(_GEO_DEFAULTS)
    body = sections.get("HARD_FILTERS")
    if not body:
        return dict(_GEO_DEFAULTS)
    return _parse_section(body)
