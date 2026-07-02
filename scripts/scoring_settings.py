"""Cost/volume settings from the user profile's ``## VOLUME`` section.

Two knobs live here, both tied to the user's plan tier rather than to neutral
tool mechanics — so they belong in ``config/user_profile.md`` (gitignored,
personal), not in ``config/defaults.toml``:

  - ``scoring_model`` — which model tier scores each vacancy. The model is the
    MAIN cost dial (STRATEGY guardrail 3): a budget plan defaults to the cheaper
    ``sonnet``; a bigger plan can set ``opus``. Chosen at onboarding, changed in
    one line.
  - ``max_per_run`` — a spike-day SAFETY NET, not the primary lever. A quiet day
    scores 20-30 vacancies; a burst day (hundreds of new roles at once) must not
    silently burn the plan. When a run has no explicit ``--limit``, scoring stops
    at this cap and reports "scored X of Y" so the rest is offered next run.

Section format (same ``key: value`` shape as ``## HARD_FILTERS``)::

    ## VOLUME
    scoring_model: sonnet
    max_per_run: 150

A missing file, missing section, missing key, ``(none)`` placeholder, or garbage
value all fall back to the neutral defaults below. Never raises. Reuses the
single profile reader (``prompts._load_user_profile``) so it sees exactly the
same profile file as scoring and the hard filters.
"""

from __future__ import annotations

import re

# Reuse the single profile reader so these settings and the LLM prompts always
# read the same file. prompts.py lives next to this module on sys.path.
from prompts import _load_user_profile

# Neutral defaults. Sonnet is the cheap, budget-plan model (no personal anchor in
# a shipped default). The cap is a spike-day guard: ~5-7x a normal daily batch
# (20-30), well under a several-hundred-vacancy burst, and mid-range of a
# sensible 100-300 safety-net band.
DEFAULT_SCORING_MODEL = "sonnet"
DEFAULT_MAX_PER_RUN = 150

# Model tiers the runbook knows how to launch as a subagent.
_ALLOWED_MODELS = {"haiku", "sonnet", "opus"}

# Values that mean "the user left this field empty".
_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}

# Strip HTML comment blocks so example lines inside the template's explanatory
# comments are never parsed as real values (matches hard_filters.py).
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _volume_fields() -> dict[str, str]:
    """Parse the ``## VOLUME`` section body into a ``{key: raw_value}`` dict.

    Returns ``{}`` when the profile, the section, or the reader is unavailable.
    """
    try:
        sections = _load_user_profile()
    except Exception:
        return {}
    body = sections.get("VOLUME")
    if not body:
        return {}
    body = _HTML_COMMENT.sub("", body)
    out: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        out[key.strip().lower()] = val.strip()
    return out


def scoring_model() -> str:
    """Return the configured scoring model, or the neutral default (sonnet).

    Unknown / empty / placeholder values fall back to the default rather than
    raising, so a typo never crashes the run.
    """
    val = _volume_fields().get("scoring_model", "").strip().lower()
    if val in _EMPTY_TOKENS or val not in _ALLOWED_MODELS:
        return DEFAULT_SCORING_MODEL
    return val


def max_per_run() -> int:
    """Return the per-run scoring cap, or the neutral default (150).

    A non-positive or garbage value falls back to the default — the cap is a
    safety net and must never resolve to "score nothing" or crash.
    """
    val = _volume_fields().get("max_per_run", "").strip()
    if val.lower() in _EMPTY_TOKENS:
        return DEFAULT_MAX_PER_RUN
    try:
        n = int(val)
    except ValueError:
        return DEFAULT_MAX_PER_RUN
    return n if n > 0 else DEFAULT_MAX_PER_RUN
