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
import sys

# Reuse the single profile reader so HARD filters and the LLM prompts always
# read the same file. prompts.py lives next to this module on sys.path.
from prompts import _load_user_profile, profile_raw_text

# Values that mean "the user left this filter empty".
_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}

# Strip HTML comment blocks (<!-- ... -->) so example lines inside the template's
# explanatory comment are never parsed as real filter values.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# A markdown ATX heading at any level (# .. ######). Used ONLY to spot a heading
# the user meant as HARD_FILTERS but mistyped (wrong level, name typo, trailing
# punctuation) so we can warn instead of silently treating the section as absent.
_ATX_HEADING = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$")

# A "key: value" line whose key is a bare identifier (letters/underscores). Drives
# field parsing so a stray space before the colon or an unknown field name is
# detected rather than silently skipped.
_FIELD_LINE = re.compile(r"^([a-z][a-z_]*)\s*:")


def _warn(message: str) -> None:
    """Loud stderr warning, matching the profile-fallback warning style."""
    print(f"⚠  {message}", file=sys.stderr)


def _looks_like_hard_filters(heading_text: str) -> bool:
    """True if a heading was plainly meant as HARD_FILTERS.

    Ignores level, separators and case so it catches ``### HARD_FILTERS``,
    ``## HARD FILTER``, ``## HARDFILTERS`` and ``## Hard-Filters`` — anything
    carrying the intent but not parsing into the exact ``HARD_FILTERS`` key.
    """
    norm = heading_text.upper().replace(" ", "").replace("_", "").replace("-", "")
    return "HARD" in norm and "FILTER" in norm


def _warn_if_hard_filters_heading_mistyped() -> None:
    """Warn when the profile has a HARD_FILTERS-ish heading that didn't parse.

    Called only when no ``HARD_FILTERS`` section was found. If the raw profile
    still carries a heading clearly meant as HARD_FILTERS (wrong level or a
    typo), the ENTIRE section — every geo ban and title exclude — was silently
    dropped. Warn so the user learns their filters are inactive instead of
    guessing why nothing is filtered. A genuinely absent section (no such
    heading) is the documented default and stays quiet.
    """
    raw = profile_raw_text()
    if not raw:
        return
    for line in raw.splitlines():
        m = _ATX_HEADING.match(line)
        if m and _looks_like_hard_filters(m.group(1)):
            _warn(
                f"profile heading '{line.strip()}' looks like HARD_FILTERS but "
                "was not recognised — it must be exactly '## HARD_FILTERS' (two "
                "'#', that exact name). The ENTIRE HARD_FILTERS section is "
                "INACTIVE: no geo bans and no title excludes are applied. Fix the "
                "heading to re-enable them."
            )
            return


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
    """Parse a HARD_FILTERS section body into the filter lists + geo policy.

    Field lines are matched by KEY (identifier before the colon), so a stray
    ``ban_regions : africa`` still parses. A ``key: value`` line whose key is not
    a known field is a typo that would silently void just that filter — those are
    collected and warned about instead.
    """
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
    unknown_fields: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        m = _FIELD_LINE.match(stripped.lower())
        if not m:
            continue  # blank line, prose, bullet — not a field assignment.
        key = m.group(1)
        raw = stripped.split(":", 1)[1]
        kind = fields.get(key)
        if kind is None:
            unknown_fields.append(key)
            continue
        if kind == "csv":
            out[key] = _split_csv(raw)
        elif kind == "bool":
            out[key] = _parse_bool(raw)
        else:
            out[key] = _parse_int(raw)
    if unknown_fields:
        _warn(
            "profile HARD_FILTERS has unrecognised field name(s): "
            f"{', '.join(sorted(set(unknown_fields)))}. Each was IGNORED, so that "
            f"filter is INACTIVE. Known fields: {', '.join(fields)}."
        )
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
    if "HARD_FILTERS" not in sections:
        # No section by that exact key. Either the user deliberately omitted it
        # (fine — filters are empty by design) or they mistyped the heading and
        # silently lost every filter. Warn only in the latter case.
        _warn_if_hard_filters_heading_mistyped()
        return dict(_GEO_DEFAULTS)
    body = sections["HARD_FILTERS"]
    if not body:
        return dict(_GEO_DEFAULTS)  # section present but empty → intentional.
    return _parse_section(body)
