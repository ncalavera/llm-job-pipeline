"""Guardrail: no private data is tracked in this PUBLIC GitHub repo.

The repo (`ncalavera/llm-job-pipeline`) is public, but the pipeline handles
private data: the candidate profile, scraped vacancies, the dashboard data file
(`public/data.js`, which embeds PII in scoring text), local databases, and
secrets. These are all gitignored — this test is the regression guard that the
gitignore (and the pre-commit hook) actually held: it fails if any sensitive
path ever becomes git-tracked.

Mirrors the pre-commit hook in `hooks/pre-commit`. The hook blocks at commit
time; this test catches anything that slipped through (e.g. a `--no-verify`
commit or a pre-existing leak).
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Private-DATA content signature — mirrors section 3 of hooks/pre-commit. The
# serialized triage-review fields, matched in their JSON shape (a quoted key, a
# colon, then a quoted STRING value). That value-quote is the discriminator:
# real serialized data pairs the key with a quoted string, while the source that
# BUILDS the payload pairs it with a variable (r.get(...), no quote), so this
# never fires on the generator or on plain `review.cv_notes` field references.
# These six keys are triage-only and appear nowhere else in the tree. Catches a
# PII leak by CONTENT (a renamed copy / export / dump of data.js), not just by
# the data.js path.
PII_CONTENT_RE = re.compile(
    r'"(cv_notes|first_impression|preparation|skip_reason'
    r'|research_question|network_contact)"\s*:\s*"'
)

# Patterns matched against tracked paths (relative to repo root). A tracked file
# matching any of these is a leak. `.env.example` and `*.sql` are explicitly
# allowed (templates / schema DDL, intended to be public).
SENSITIVE = [
    re.compile(r"^\.env$"),
    re.compile(r"^\.env\.local$"),
    re.compile(r"^\.env\.(?!example$).+"),  # any .env.* except .env.example
    re.compile(r"^config/user_profile\.md$"),
    re.compile(r"^public/data\.js$"),
    re.compile(r"^vacancies/"),
    re.compile(r"^evals/"),  # personal golden set (labelled vacancies + reasons)
    re.compile(r"^\.firecrawl/"),
    re.compile(r"^architecture-notes/"),
    re.compile(r"^\.claude-session-acceptance\.md$"),
    re.compile(r"\.(db|db-wal|db-shm|sqlite)$"),  # local databases (not *.sql)
]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def test_no_private_data_is_tracked():
    leaks: list[str] = []
    for path in _tracked_files():
        for pat in SENSITIVE:
            if pat.search(path):
                leaks.append(f"{path}  (matched /{pat.pattern}/)")
                break
    assert not leaks, (
        "Private data is tracked in the PUBLIC repo — remove it "
        "(git rm --cached <file>) and confirm it is gitignored:\n" + "\n".join(leaks)
    )


def test_no_private_field_content_is_tracked():
    """Content-level mirror of the hook's section 3: no tracked file carries a
    serialized triage-review blob, even under a name the path guard misses.

    Skips .claude/worktrees/ — live agent checkouts carry this project's own
    data by design and are never shipped (PR #43)."""
    leaks: list[str] = []
    for path in _tracked_files():
        if path.startswith(".claude/worktrees/"):
            continue
        f = REPO / path
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        if PII_CONTENT_RE.search(text):
            leaks.append(path)
    assert not leaks, (
        "Private triage-review data is tracked in the PUBLIC repo — this looks "
        "like a serialized dump of data.js. Remove it:\n" + "\n".join(leaks)
    )


def test_pii_content_signature_matches_data_not_code():
    """Guards the signature: it must catch serialized data but stay quiet on the
    generator source and on plain field references, or it is useless / noisy."""

    # Assemble the serialized `"field": "value"` shape here rather than as a
    # source literal, so this guard file does not itself carry the signature it
    # polices (which the pre-commit hook and the tracked-content scan would flag).
    def serialized(field: str, value: str, spacer: str = " ") -> str:
        return '"' + field + '":' + spacer + '"' + value + '"'

    # Serialized data (leak) — key paired with a quoted string value.
    assert PII_CONTENT_RE.search(serialized("cv_notes", "tailor the CV"))
    assert PII_CONTENT_RE.search(serialized("skip_reason", "too junior", ""))  # minified
    assert PII_CONTENT_RE.search(serialized("cv_notes", ""))  # empty string still data
    # Source that BUILDS the payload — value is a variable, not a quoted string.
    assert not PII_CONTENT_RE.search('"cv_notes": r.get(field, "")')
    # Plain field references in code — no quoted key at all.
    assert not PII_CONTENT_RE.search("meta += escHtml(review.cv_notes);")
    assert not PII_CONTENT_RE.search("const x = { cv_notes: note };")


def test_patterns_allow_safe_files_and_block_unsafe():
    """Guards the matcher so it cannot silently start passing everything."""

    def hit(p: str) -> bool:
        return any(pat.search(p) for pat in SENSITIVE)

    # blocked
    assert hit(".env")
    assert hit(".env.local")
    assert hit(".env.production")
    assert hit("config/user_profile.md")
    assert hit("public/data.js")
    assert hit("vacancies/jobs-archive/x.json")
    assert hit("evals/golden_set.jsonl")
    assert hit("architecture-notes/lesson.md")
    assert hit("data/jobsearch.db")
    assert hit("local.sqlite")
    # allowed (must NOT match)
    assert not hit(".env.example")
    assert not hit("sql/schema.sqlite.sql")  # DDL, ends in .sql
    assert not hit("public/app.js")
    assert not hit("config/user_profile.example.md")
