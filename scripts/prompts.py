"""LLM prompt loader.

Reads .md prompt templates from scripts/prompts/ and substitutes user-specific
placeholders ({{USER_PROFILE}}, {{TARGET_ROLES}}, {{EXCLUDE_PATTERNS}}, etc.)
from the user profile file pointed to by USER_PROFILE_PATH env var
(default: config/user_profile.md in the repo root).

Edit your profile in one place; both prompts see the change.
"""

import os
import re
import sys
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PROFILE_PATH = _REPO_ROOT / "config" / "user_profile.md"
EXAMPLE_PROFILE_PATH = _REPO_ROOT / "config" / "user_profile.example.md"


def _load_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


# Parsed-profile cache keyed by the resolved profile-source key. Both the
# scoring prompts (this module) and the HARD filters (hard_filters.py) call
# ``_load_user_profile()``; without a cache each call re-parses the file AND
# re-prints the "no profile" warning, so the warning fired twice per command.
# Caching makes the parse + warning happen once. The cache stores
# ``(mtime, sections)`` and re-checks the file mtime on every lookup, so an edit
# to config/user_profile.md in a long-lived process (e.g. the dashboard server)
# or in a test is picked up on the next call instead of serving a stale parse.
_profile_cache: dict[str, tuple[float, dict[str, str]]] = {}


def clear_profile_cache() -> None:
    """Drop all cached parsed profiles (e.g. in tests, or after an edit)."""
    _profile_cache.clear()


def _resolve_profile_path() -> "tuple[Path | None, bool]":
    """Decide which profile file to read and whether it's the EXAMPLE fallback.

    Returns ``(path, warn_example)``. ``path`` is None only when neither a real
    nor an example profile exists. An explicit ``USER_PROFILE_PATH`` is honoured
    verbatim even if it does not exist — the caller surfaces the read error, so
    a mistyped override fails loudly rather than silently using the example.
    """
    path_env = os.environ.get("USER_PROFILE_PATH")
    if path_env:
        return Path(path_env).expanduser().resolve(), False
    if DEFAULT_PROFILE_PATH.exists():
        return DEFAULT_PROFILE_PATH, False
    if EXAMPLE_PROFILE_PATH.exists():
        return EXAMPLE_PROFILE_PATH, True
    return None, False


def profile_raw_text() -> "str | None":
    """Return the raw text of the ACTIVE profile file, or None if none exists.

    Lets callers inspect the file's literal structure — e.g. hard_filters.py
    detecting a mistyped ``## HARD_FILTERS`` heading — while seeing exactly the
    same file _load_user_profile() parses. Never warns, never raises: a missing
    or unreadable file yields None.
    """
    path, _ = _resolve_profile_path()
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _load_user_profile() -> dict[str, str]:
    """Load user profile from a Markdown file with H2 sections.

    Each `## SECTION_NAME` block becomes `placeholders["SECTION_NAME"]`.
    Section names are uppercased and spaces replaced with underscores so
    they match `{{USER_PROFILE}}`, `{{TARGET_ROLES}}` etc.

    Cached per resolved source file so repeat callers reuse the same parse —
    and the missing-profile warning prints once. The cache keys on the file
    that was actually chosen, so a changed profile path (e.g. in tests) is a
    cache miss and the "no profile found" case is never cached as a success.
    """
    path, warn_example = _resolve_profile_path()
    if path is None:
        raise FileNotFoundError(
            "No user profile found. Create config/user_profile.md "
            "(copy from config/user_profile.example.md and fill in)."
        )

    # Cache hit on the resolved file with an unchanged mtime → reuse parse, and
    # (crucially) do not re-emit the example-fallback warning. This is what makes
    # the warning fire once even though prompts.py and hard_filters.py both load
    # it, while still picking up edits (mtime changes invalidate the entry).
    cache_key = str(path)
    mtime = path.stat().st_mtime
    cached = _profile_cache.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    if warn_example:
        # Falling back to the bundled EXAMPLE profile (a fictional person). Scores
        # produced against it are meaningless. Warn loudly so a user never scores
        # an entire run against the example without knowing.
        print(
            "⚠  No config/user_profile.md found — using the EXAMPLE profile "
            "(a fictional person). Scores are MEANINGLESS until you create your "
            "own: copy config/user_profile.example.md to config/user_profile.md "
            "and fill it in.",
            file=sys.stderr,
        )

    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_body: list[str] = []

    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current_key is not None:
                sections[current_key] = "\n".join(current_body).strip()
            current_key = match.group(1).upper().replace(" ", "_")
            current_body = []
        elif current_key is not None:
            current_body.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_body).strip()

    _profile_cache[cache_key] = (mtime, sections)
    return sections


def _render(template: str, sections: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders. Unknown keys are left as-is."""

    def sub(match: re.Match) -> str:
        key = match.group(1).strip().upper().replace(" ", "_")
        return sections.get(key, match.group(0))

    return re.sub(r"\{\{\s*([A-Z_][A-Z0-9_ ]*)\s*\}\}", sub, template)


_profile = _load_user_profile()

#: The JSON key the company-scoring prompt asks the LLM to emit for the optional
#: custom boost. Read from the profile's CUSTOM_BOOST_FIELD section so the prompt
#: and the score-ingestion code agree on ONE name. Falls back to the example
#: default. The legacy key "mpa_narrative_boost" is still accepted downstream.
CUSTOM_BOOST_FIELD = _profile.get("CUSTOM_BOOST_FIELD", "").strip() or "career_narrative_boost"

#: Keys downstream code accepts as the custom boost (configured first, then the
#: legacy name) — back-compat for older enrichment payloads.
CUSTOM_BOOST_KEYS = tuple(dict.fromkeys([CUSTOM_BOOST_FIELD, "mpa_narrative_boost"]))

VACANCY_SCORING_PROMPT = _render(_load_template("vacancy-scoring.md"), _profile)
COMPANY_SCORING_PROMPT = _render(_load_template("company-scoring.md"), _profile)

# Vacancy scoring is strictly independent of company scoring (KTD4): the role's
# fit is judged on its own merits, so the company alignment_score is deliberately
# NOT passed in here.
VACANCY_SCORING_USER_TEMPLATE = """Score this vacancy for the candidate:

**Organization:** {org} (Tier {tier})
**Job Title:** {title}
**Location:** {location}

**Full Description:**
{description}"""

COMPANY_SCORING_USER_TEMPLATE = """Analyze company "{name}" ({url}).
{csv_context}
=== COMPANY DATA ===
{content}"""
