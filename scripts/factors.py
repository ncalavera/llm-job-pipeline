"""The factor model — every user taste factor declared with a STRENGTH.

A "factor" is one thing the user cares about (a title word, a sector, a work
condition). Each factor is declared in the profile with exactly one strength:

  * ``filter``  — a HARD block: the role is dropped BEFORE scoring and never
                  costs a scoring request. Lives in ``## HARD_FILTERS``
                  (``exclude_title_keywords`` + the geo bans).
  * ``penalty`` — subtracts points DURING scoring. Fed to the scorer prompt as
                  ``## EXCLUDE_PATTERNS``; a role can still surface if strong
                  elsewhere.
  * ``note``    — display-only. NEVER reaches the scorer and NEVER changes
                  whether a role passes or its score. Lives in ``## NOTES``.

The same factor is a filter for one user and a penalty for another; the strength
is the user's choice, set at onboarding and changed in settings. This module is
the single reader of that declaration. The existing HARD_FILTERS vs
EXCLUDE_PATTERNS split in the profile is the embryo of this model; ``## NOTES``
completes it with the display-only strength.

Load-bearing invariant (guarded by a test): a NOTE is never fed to the scorer.
The scoring prompt is rendered from the profile sections EXCEPT the ones named in
``SCORER_HIDDEN_SECTIONS`` — otherwise a note would become an implicit penalty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Reuse the ONE profile reader so factors, hard filters and the scoring prompts
# all see exactly the same file (prompts.py lives next to this on sys.path).
from prompts import _load_user_profile

# Strength labels — the whole vocabulary of the factor model.
FILTER = "filter"
PENALTY = "penalty"
NOTE = "note"
STRENGTHS = (FILTER, PENALTY, NOTE)

#: Profile sections that must NEVER be substituted into a scorer prompt. A note
#: that reached the scorer would silently act as a penalty, so ``## NOTES`` is
#: display-only by construction: prompts.py renders without it, and this set is
#: the machine-readable statement of that contract (asserted by a test).
SCORER_HIDDEN_SECTIONS = frozenset({"NOTES"})

# A profile bullet line: "- text" or "* text".
_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
# HTML comment blocks (the section's explanatory template) — never real factors.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Factor:
    """One declared factor and the strength the user gave it."""

    text: str
    strength: str  # one of STRENGTHS
    section: str  # profile section it was declared in
    scorer_visible: bool  # True only for penalty factors

    @property
    def is_note(self) -> bool:
        return self.strength == NOTE


def _bullets(body: str | None) -> list[str]:
    """Return the bullet lines of a section body, dropping the template comment."""
    if not body:
        return []
    body = _HTML_COMMENT.sub("", body)
    out: list[str] = []
    for line in body.splitlines():
        m = _BULLET.match(line)
        if m:
            out.append(m.group(1).strip())
    return out


def _title_keywords(sections: dict[str, str]) -> list[str]:
    """The user's ``exclude_title_keywords`` — the movable FILTER factors.

    Parsed via the same loader hard_filters uses so the two never diverge."""
    from hard_filters import _parse_section  # local import: avoids an import cycle

    body = sections.get("HARD_FILTERS")
    if not body:
        return []
    return list(_parse_section(body).get("exclude_title_keywords", []))


def load_factors(sections: dict[str, str] | None = None) -> list[Factor]:
    """Return every declared factor with its strength.

    ``sections`` lets a caller pass an already-parsed profile (or a test double);
    by default the active profile is read. Never raises — a missing/empty profile
    yields an empty factor list, so a fresh clone declares nothing.
    """
    if sections is None:
        try:
            sections = _load_user_profile()
        except Exception:
            return []

    factors: list[Factor] = []

    # FILTER strength — personal title keywords (the geo bans are also filter
    # strength but are structured fields, handled by hard_filters/geo, not
    # movable free-text factors, so they are not enumerated here).
    for kw in _title_keywords(sections):
        factors.append(Factor(text=kw, strength=FILTER, section="HARD_FILTERS", scorer_visible=True))

    # PENALTY strength — the scorer-visible taste patterns.
    for text in _bullets(sections.get("EXCLUDE_PATTERNS")):
        factors.append(
            Factor(text=text, strength=PENALTY, section="EXCLUDE_PATTERNS", scorer_visible=True)
        )

    # NOTE strength — display-only, hidden from the scorer.
    for text in _bullets(sections.get("NOTES")):
        factors.append(Factor(text=text, strength=NOTE, section="NOTES", scorer_visible=False))

    return factors


def by_strength(factors: list[Factor], strength: str) -> list[Factor]:
    return [f for f in factors if f.strength == strength]


def notes(sections: dict[str, str] | None = None) -> list[str]:
    """The display-only note factors (never fed to the scorer)."""
    return [f.text for f in by_strength(load_factors(sections), NOTE)]


def summary(sections: dict[str, str] | None = None) -> dict[str, int]:
    """Count of declared factors per strength — for the settings/summary view."""
    factors = load_factors(sections)
    return {s: len(by_strength(factors, s)) for s in STRENGTHS}
