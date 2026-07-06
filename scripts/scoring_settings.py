"""Cost/volume settings from the user profile's ``## VOLUME`` section.

Four knobs live here, all tied to the user's plan tier rather than to neutral
tool mechanics — so they belong in ``config/user_profile.md`` (gitignored,
personal), not in ``config/defaults.toml``:

  - ``scoring_model`` — the STRONG model tier that scores the finalists in the
    two-pass flow. The model is the MAIN cost dial (STRATEGY guardrail
    3): a budget plan defaults to the cheaper ``sonnet``; a bigger plan can set
    ``opus``. Chosen at onboarding, changed in one line.
  - ``screen_model`` — the CHEAP model that gives every new vacancy a fast first
    score (the two-pass SCREEN); only roles that clear ``escalate_threshold`` are
    re-scored by ``scoring_model``. Defaults to ``haiku`` (the cheapest tier) and
    is clamped so it can never cost more than the strong model — a screen as
    expensive as the final pass would defeat the saving.
  - ``escalate_threshold`` — the screen-score floor at/above which a role is
    escalated to the strong model. Calibrated against the golden set so the cheap
    screen drops zero of the strong model's true positives: the lowest
    golden role the strong model rated a fit screened at 70 on the cheap model, so
    any floor <= 70 preserves every role the strong pass would surface. The
    default (50) leaves a 20-point safety margin under that boundary while still
    diverting the weak majority; raising it saves more but narrows the margin.
  - ``max_per_run`` — a spike-day SAFETY NET, not the primary lever. A quiet day
    scores 20-30 vacancies; a burst day (hundreds of new roles at once) must not
    silently burn the plan. When a run has no explicit ``--limit``, scoring stops
    at this cap and reports "scored X of Y" so the rest is offered next run. In
    the two-pass flow the cap bounds the SCREEN set; the strong pass is a subset,
    so the cap protects both passes at once.

Section format (same ``key: value`` shape as ``## HARD_FILTERS``)::

    ## VOLUME
    scoring_model: sonnet
    screen_model: haiku
    escalate_threshold: 50
    max_per_run: 150

A missing file, missing section, missing key, ``(none)`` placeholder, or garbage
value all fall back to the neutral defaults below. Never raises. Reuses the
single profile reader (``prompts._load_user_profile``) so it sees exactly the
same profile file as scoring and the hard filters.
"""

from __future__ import annotations

import re

import settings

# Reuse the single profile reader so these settings and the LLM prompts always
# read the same file. prompts.py lives next to this module on sys.path.
from prompts import _load_user_profile

# Neutral defaults. Sonnet is the cheap, budget-plan model (no personal anchor in
# a shipped default). The per-run cap default is the NEUTRAL volume dial
# ([volume] daily_scoring_limit); DEFAULT_MAX_PER_RUN is only the ultimate
# fallback used when even that is unreadable. It mirrors the shipped
# daily_scoring_limit (150): a spike-day guard ~5-7x a normal daily batch
# (20-30), well under a several-hundred-vacancy burst.
DEFAULT_SCORING_MODEL = "sonnet"
DEFAULT_MAX_PER_RUN = 150


def _default_max_per_run() -> int:
    """The neutral scoring-limit default: [volume] daily_scoring_limit.

    This is what wires the profile-less path to the single volume window. The
    profile's ## VOLUME max_per_run still overrides it (see ``max_per_run``).
    """
    try:
        return int(settings.volume()["daily_scoring_limit"])
    except Exception:
        return DEFAULT_MAX_PER_RUN


# The cheap two-pass screen model and the escalation floor. haiku is
# the cheapest tier — it maximises the two-pass saving (Haiku 4.5 costs ~1/5 of
# Opus 4.8 and ~1/3 of Sonnet 5 per token). The 50 floor was calibrated against
# the golden set: the cheap model scores ~12 points hotter than the strong model,
# and the lowest golden role the strong model rated a fit screened at 70 on the
# cheap model. A floor of 50 therefore clears every strong-model true positive by
# a 20-point margin while diverting the weak majority (a 40 floor would escalate
# ~55% of a realistic day — the majority — and erase most of the saving).
DEFAULT_SCREEN_MODEL = "haiku"
DEFAULT_ESCALATION_THRESHOLD = 50

# The cheap company pre-filter model — a low-cost relevance screen that drops
# clearly-irrelevant board-discovered companies BEFORE any paid enrichment
# (Firecrawl scrape + Exa search). Same cost lever as the vacancy screen (the
# model tier is the dial), defaults to the cheapest tier, and is clamped so it
# can never cost more than the strong scoring model.
DEFAULT_COMPANY_SCREEN_MODEL = "haiku"

# Model tiers the runbook knows how to launch as a subagent.
_ALLOWED_MODELS = {"haiku", "sonnet", "opus"}

# Price order, cheapest first — used to clamp the screen model so it can never be
# pricier than the strong model.
_MODEL_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}

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


def screen_model() -> str:
    """Return the CHEAP model tier for the two-pass screen.

    Every new vacancy gets a fast score from this model; only the finalists that
    clear ``escalation_threshold`` are re-scored by ``scoring_model``. Unknown /
    empty / placeholder values fall back to the default (haiku, the cheapest
    tier). The result is CLAMPED to be no pricier than ``scoring_model`` — a
    screen that costs as much as the final pass defeats the two-pass saving, so a
    profile that sets, say, ``screen_model: opus`` with a ``sonnet`` strong model
    is clamped down to ``sonnet``.
    """
    val = _volume_fields().get("screen_model", "").strip().lower()
    if val in _EMPTY_TOKENS or val not in _ALLOWED_MODELS:
        val = DEFAULT_SCREEN_MODEL
    strong = scoring_model()
    if _MODEL_RANK[val] > _MODEL_RANK[strong]:
        return strong
    return val


def company_screen_model() -> str:
    """Return the CHEAP model tier for the company pre-filter screen.

    Every newly discovered candidate company gets a fast keep/drop relevance
    check from this model BEFORE any paid enrichment runs on it, so the strong
    scoring model (and Firecrawl/Exa) only ever see plausible fits. Unknown /
    empty / placeholder values fall back to the default (haiku, the cheapest
    tier). Clamped to be no pricier than ``scoring_model`` — a screen that costs
    as much as the final pass defeats the saving.
    """
    val = _volume_fields().get("company_screen_model", "").strip().lower()
    if val in _EMPTY_TOKENS or val not in _ALLOWED_MODELS:
        val = DEFAULT_COMPANY_SCREEN_MODEL
    strong = scoring_model()
    if _MODEL_RANK[val] > _MODEL_RANK[strong]:
        return strong
    return val


# A configured floor at/above this effectively turns the strong pass off: real
# screen scores rarely if ever land this high, so escalation becomes a
# theoretical possibility rather than a practical one. See
# escalation_threshold_warning().
NEAR_CEILING_THRESHOLD = 95


def escalation_threshold() -> int:
    """Return the screen-score floor at/above which a role is escalated to the
    strong model.

    Calibrated against the golden set so the cheap screen drops zero of the strong
    model's true positives (the default, 50, sits 20 points under the lowest such
    golden role's screen score of 70). A garbage / empty value falls back to the
    default; the result is clamped to 0..100 so the bound itself is never
    impossible — but the clamp does NOT guarantee escalation stays live: 100 (or
    anything close to it) is a valid, silently-accepted value that in practice
    escalates nothing, since real screen scores rarely reach it. Callers that act
    on a fresh value should also check escalation_threshold_warning().
    """
    val = _volume_fields().get("escalate_threshold", "").strip()
    if val.lower() in _EMPTY_TOKENS:
        return DEFAULT_ESCALATION_THRESHOLD
    try:
        n = int(val)
    except ValueError:
        return DEFAULT_ESCALATION_THRESHOLD
    return min(100, max(0, n))


def escalation_threshold_warning(threshold: int) -> str | None:
    """A loud one-liner when ``threshold`` means the strong pass will realistically
    escalate nothing (clamping accepts it, but it is never what a user wants) —
    ``None`` otherwise.
    """
    if threshold >= NEAR_CEILING_THRESHOLD:
        return (
            f"⚠  escalate_threshold is {threshold} — at or above the near-ceiling cutoff "
            f"({NEAR_CEILING_THRESHOLD}). Real screen scores rarely reach that high, so the "
            "strong pass will effectively escalate nothing this run; every role keeps its "
            "cheap screen score. If that's not intended, lower '[## VOLUME] escalate_threshold'."
        )
    return None


def max_per_run() -> int:
    """Return the per-run scoring cap.

    Resolution: the profile's ``## VOLUME max_per_run`` (personal plan-tier knob)
    wins; otherwise the neutral ``[volume] daily_scoring_limit`` from
    defaults.toml. A non-positive or garbage profile value falls back to that
    neutral default — the cap is a safety net and must never resolve to "score
    nothing" or crash.
    """
    val = _volume_fields().get("max_per_run", "").strip()
    if val.lower() in _EMPTY_TOKENS:
        return _default_max_per_run()
    try:
        n = int(val)
    except ValueError:
        return _default_max_per_run()
    return n if n > 0 else _default_max_per_run()
