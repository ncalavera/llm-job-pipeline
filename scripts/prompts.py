"""LLM prompt loader.

Reads .md prompt templates from scripts/prompts/ and substitutes user-specific
placeholders ({{USER_PROFILE}}, {{TARGET_ROLES}}, {{EXCLUDE_PATTERNS}}, etc.)
from the user profile file pointed to by USER_PROFILE_PATH env var
(default: config/user_profile.md in the repo root).

Edit your profile in one place; both prompts see the change.
"""

import os
import re
import subprocess
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

# Memo for the linked-worktree → main-checkout profile lookup. The git topology
# is fixed for the life of a process, and ``_resolve_profile_path()`` runs on
# every profile load (to compute the cache key); without this the fallback would
# spawn ``git rev-parse`` on every call. Keyed by the repo root so a test that
# repoints ``_REPO_ROOT`` re-evaluates. Cleared alongside the parsed cache. The
# sentinel distinguishes "not looked up yet" from "looked up, found nothing".
_UNRESOLVED = object()
_worktree_profile_memo: dict[str, "Path | None"] = {}


def clear_profile_cache() -> None:
    """Drop all cached parsed profiles (e.g. in tests, or after an edit)."""
    _profile_cache.clear()
    _worktree_profile_memo.clear()


def _worktree_main_profile() -> "Path | None":
    """Locate ``config/user_profile.md`` in the MAIN checkout when this process
    runs inside a LINKED git worktree that lacks its own copy.

    ``config/user_profile.md`` is gitignored (personal data), so a ``git
    worktree`` never carries it — a pipeline launched from a worktree would fall
    through to the bundled EXAMPLE and bake default settings into shared state
    (the same failure class as the wave-3 ".env priority" trap). Detection is
    read-only: ``git rev-parse --git-common-dir`` points at the shared ``.git``
    of the main checkout; in a linked worktree it differs from ``--git-dir``. The
    main checkout root is the parent of that common ``.git`` directory.

    Returns the resolved path only when it actually exists there, else None (so
    the caller keeps its existing EXAMPLE fallback). Memoized per repo root;
    never raises (git missing / not a repo → None).
    """
    key = str(_REPO_ROOT)
    cached = _worktree_profile_memo.get(key, _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached

    result: "Path | None" = None
    try:
        common = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_dir = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if common.returncode == 0 and git_dir.returncode == 0:
            common_dir = Path(common.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = _REPO_ROOT / common_dir
            common_dir = common_dir.resolve()
            gd = Path(git_dir.stdout.strip())
            if not gd.is_absolute():
                gd = _REPO_ROOT / gd
            gd = gd.resolve()
            # Only a LINKED worktree has a per-worktree git dir distinct from the
            # shared common dir; in the main checkout they're the same → no-op.
            if gd != common_dir:
                candidate = common_dir.parent / "config" / "user_profile.md"
                if candidate.exists():
                    result = candidate
    except (OSError, subprocess.SubprocessError):
        result = None

    _worktree_profile_memo[key] = result
    return result


def _resolve_profile_path() -> "tuple[Path | None, bool, bool]":
    """Decide which profile file to read.

    Returns ``(path, warn_example, from_worktree)``. ``warn_example`` is True
    when falling back to the bundled EXAMPLE; ``from_worktree`` is True when the
    profile was recovered from the MAIN checkout because this linked worktree
    lacks its own (gitignored) copy. ``path`` is None only when neither a real
    nor an example profile exists. An explicit ``USER_PROFILE_PATH`` is honoured
    verbatim even if it does not exist — the caller surfaces the read error, so
    a mistyped override fails loudly rather than silently using the example.
    """
    path_env = os.environ.get("USER_PROFILE_PATH")
    if path_env:
        return Path(path_env).expanduser().resolve(), False, False
    if DEFAULT_PROFILE_PATH.exists():
        return DEFAULT_PROFILE_PATH, False, False
    # Linked-worktree trap: config/user_profile.md is gitignored, so a worktree
    # doesn't have it. Recover the REAL profile from the main checkout before
    # degrading to the meaningless EXAMPLE.
    main_profile = _worktree_main_profile()
    if main_profile is not None:
        return main_profile, False, True
    if EXAMPLE_PROFILE_PATH.exists():
        return EXAMPLE_PROFILE_PATH, True, False
    return None, False, False


def has_real_profile() -> bool:
    """True when profile resolution finds a genuine profile — the default file,
    an explicit ``USER_PROFILE_PATH``, or the linked-worktree fallback to the
    main checkout. False when it would silently use the bundled EXAMPLE (implicit
    fallback) or finds nothing at all.

    A production writer (the shared dashboard snapshot) uses this to refuse to
    bake example/default values into shared state — the linked-worktree trap.
    """
    path, warn_example, _ = _resolve_profile_path()
    return path is not None and not warn_example


def profile_raw_text() -> "str | None":
    """Return the raw text of the ACTIVE profile file, or None if none exists.

    Lets callers inspect the file's literal structure — e.g. hard_filters.py
    detecting a mistyped ``## HARD_FILTERS`` heading — while seeing exactly the
    same file _load_user_profile() parses. Never warns, never raises: a missing
    or unreadable file yields None.
    """
    path, _, _ = _resolve_profile_path()
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
    path, warn_example, from_worktree = _resolve_profile_path()
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
    elif from_worktree:
        # Gitignored profile doesn't travel with a linked worktree — say once,
        # on stderr, which file is actually being read so the recovery is visible.
        print(
            f"  (profile: config/user_profile.md is absent in this git worktree "
            f"(gitignored) — reading the main checkout's {path})",
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
# Screening preparation: extraction + profile comparison, no score.
VACANCY_SCREENING_PROMPT = _render(_load_template("vacancy-screening.md"), _profile)
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
