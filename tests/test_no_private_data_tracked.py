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
    assert hit("architecture-notes/lesson.md")
    assert hit("data/jobsearch.db")
    assert hit("local.sqlite")
    # allowed (must NOT match)
    assert not hit(".env.example")
    assert not hit("sql/schema.sqlite.sql")  # DDL, ends in .sql
    assert not hit("public/app.js")
    assert not hit("config/user_profile.example.md")
