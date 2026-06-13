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


def _load_user_profile() -> dict[str, str]:
    """Load user profile from a Markdown file with H2 sections.

    Each `## SECTION_NAME` block becomes `placeholders["SECTION_NAME"]`.
    Section names are uppercased and spaces replaced with underscores so
    they match `{{USER_PROFILE}}`, `{{TARGET_ROLES}}` etc.
    """
    path_env = os.environ.get("USER_PROFILE_PATH")
    if path_env:
        path = Path(path_env).expanduser().resolve()
    elif DEFAULT_PROFILE_PATH.exists():
        path = DEFAULT_PROFILE_PATH
    elif EXAMPLE_PROFILE_PATH.exists():
        path = EXAMPLE_PROFILE_PATH
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
    else:
        raise FileNotFoundError(
            "No user profile found. Create config/user_profile.md "
            "(copy from config/user_profile.example.md and fill in)."
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
CUSTOM_BOOST_FIELD = (_profile.get("CUSTOM_BOOST_FIELD", "").strip()
                      or "career_narrative_boost")

#: Keys downstream code accepts as the custom boost (configured first, then the
#: legacy name) — back-compat for older enrichment payloads.
CUSTOM_BOOST_KEYS = tuple(dict.fromkeys([CUSTOM_BOOST_FIELD, "mpa_narrative_boost"]))

VACANCY_SCORING_PROMPT = _render(_load_template("vacancy-scoring.md"), _profile)
COMPANY_SCORING_PROMPT = _render(_load_template("company-scoring.md"), _profile)

VACANCY_SCORING_USER_TEMPLATE = """Score this vacancy for the candidate:

**Organization:** {org} (Tier {tier})
**Company Alignment Score:** {alignment_score}
**Job Title:** {title}
**Location:** {location}

**Full Description:**
{description}"""

COMPANY_SCORING_USER_TEMPLATE = """Analyze company "{name}" ({url}).
{csv_context}
=== COMPANY DATA ===
{content}"""
