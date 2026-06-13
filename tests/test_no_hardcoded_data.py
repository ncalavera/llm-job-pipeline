"""Guard test — the permanent regression net for a PUBLIC, owner-agnostic repo.

Fails if any of the following creep back into the codebase:

  1. Owner identity (names, handles, infra, private repo/DB ids).
  2. Owner-specific organisations used as hardcoded data or fixtures.
  3. Internal issue / ticket references (DHA-NNN, (#NNN), issue #NNN).
  4. Owner-narration phrasing in comments ("the owner", "used to be", …).
  5. Deleted owner-data constants (the Devex / Impactpool-EU / prestige /
     manual-allowlist / animal-welfare / relevance families).
  6. A non-empty literal ``board_blacklist=[...]`` inside scripts/*.py
     (boards must ship neutral; exclusion is user opt-in via the profile).
  7. Universal junk that is actually discipline / format / career-stage
     flavored (bootcamp, fellowship, internship, volunteer, …) — those are
     the USER's optional taste, never a shipped default.

The intent: a grep for owner identity, owner orgs, ticket ids and the deleted
constant names across the public surface returns NOTHING.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TESTS = REPO / "tests"
CONFIG = REPO / "config"
DOCS_INDEX = REPO / "docs" / "index.html"
CLAUDE = REPO / ".claude"

# This guard file itself names the forbidden tokens (to assert on them), so it
# is always excluded from the scans below.
SELF = Path(__file__).name


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" in p.parts or p.name == SELF:
                continue
            out.append(p)
    return out


def _text_files() -> list[Path]:
    """Python + the public dashboard HTML + .claude command markdown."""
    files = _py_files(SCRIPTS, TESTS)
    if DOCS_INDEX.exists():
        files.append(DOCS_INDEX)
    if CLAUDE.exists():
        for p in CLAUDE.rglob("*.md"):
            files.append(p)
    return files


def _scan(files, pattern: re.Pattern) -> list[str]:
    hits = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    return hits


# ---------------------------------------------------------------------------
# 1. Owner identity
# ---------------------------------------------------------------------------

OWNER_IDENTITY = re.compile(
    r"nikita|solov|ndsolovev|wajbrmky|nikitasdaysbot|hetzner|"
    r"dharma-initiative|job-search-2026",
    re.IGNORECASE,
)


def test_no_owner_identity():
    hits = _scan(_text_files(), OWNER_IDENTITY)
    assert not hits, "Owner identity leaked:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 2. Owner-specific organisations
# ---------------------------------------------------------------------------

OWNER_ORGS = re.compile(
    r"\bfundraiseup\b|\blongview\b|\bamnesty\b|good food institute|"
    r"wellcome trust|centre for effective altruism|\bGFI\b|\bMiro\b",
    re.IGNORECASE,
)


def test_no_owner_orgs():
    # Scan code, tests, the public dashboard HTML and .claude command docs.
    hits = _scan(_text_files(), OWNER_ORGS)
    assert not hits, "Owner org names leaked:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 3. Internal ticket references (in .py only — HTML/CSS would false-positive on
#    hex colours and entities, which are not ticket ids)
# ---------------------------------------------------------------------------

TICKET = re.compile(r"DHA-\d+|\(#\d+\)|issue #\d+|#\d{3,4}\b")
# A line may legitimately carry a #NNN-shaped token that is NOT a ticket:
# 6/8-digit CSS hex colours (#1a2b3c), HTML numeric entities (&#1234;), or a
# 3-digit token used in an explicit colour/style context. A bare "#192" in prose
# (3 digits, no colour context) is treated as a ticket — only skip the line when
# such a colour/entity token is present.
_NOT_A_TICKET = re.compile(
    r"#[0-9a-fA-F]{6}\b|&#\d+;|(?:color|background)[^;]*#[0-9a-fA-F]{3}\b|"
    r"--\w[\w-]*\s*:\s*#[0-9a-fA-F]{3}\b"
)


def test_no_internal_ticket_ids():
    hits = []
    for p in _py_files(SCRIPTS, TESTS):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if TICKET.search(line) and not _NOT_A_TICKET.search(line):
                hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, "Internal ticket references leaked:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 4. Owner-narration phrasing
# ---------------------------------------------------------------------------

NARRATION = re.compile(
    r"\bthe owner\b|owner's personal|used to be|NEW MODEL|"
    r"historical behaviou?r|was dropping|hardcoded in config",
    re.IGNORECASE,
)


def test_no_owner_narration():
    hits = _scan(_py_files(SCRIPTS, TESTS), NARRATION)
    assert not hits, "Owner-narration phrasing leaked:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 5. Deleted owner-data constants
# ---------------------------------------------------------------------------

DELETED_CONSTS = re.compile(
    r"_DEVEX_[A-Z_]+|_IMPACTPOOL_EU_LOCATIONS|_IMPACTPOOL_EU_KEYWORDS|"
    r"_IMPACTPOOL_NON_EU_HINTS|_IMPACTPOOL_SENIORITY_BAD|_MPP_PRESTIGE_HINTS|"
    r"MANUAL_ALLOWLIST|_ANIMAL_WELFARE|RELEVANCE_[A-Z_]+|PRESTIGE_[A-Z_]+",
)


def test_no_deleted_owner_constants():
    hits = _scan(_py_files(SCRIPTS, TESTS), DELETED_CONSTS)
    assert not hits, "A deleted owner-data constant reappeared:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 6. No non-empty literal board_blacklist in scripts
# ---------------------------------------------------------------------------

# A hardcoded literal: board_blacklist = ["something", ...]. A list COMPREHENSION
# reading from config (board_blacklist = [kw for kw in cfg.get(...)]) is fine —
# require a quote right after the opening bracket to flag only string literals.
NONEMPTY_BLACKLIST = re.compile(r"""board_blacklist\s*=\s*\[\s*['"]""")


def test_no_hardcoded_board_blacklist():
    hits = _scan(_py_files(SCRIPTS), NONEMPTY_BLACKLIST)
    assert not hits, (
        "A non-empty board_blacklist literal leaked into scripts/ — board "
        "exclusion must be user opt-in via the profile:\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 7. Universal junk stays neutral — config sources it from TOML and the shipped
#    junk holds ONLY speculative/pipeline markers, never discipline / format /
#    career-stage / sector words.
# ---------------------------------------------------------------------------

def _shipped_junk_words() -> list[str]:
    import sys
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import settings
    j = settings.junk()
    return [w.lower() for w in (j["words"] + j["substr"])]


FLAVORED_NOT_JUNK = [
    "bootcamp", "fellowship", "internship", "intern", "graduate", "volunteer",
    "course", "summer school", "training on", "junior", "senior", "engineer",
    "developer", "nurse", "sales", "funding", "grant",
]


def test_universal_junk_has_no_flavored_words():
    junk = _shipped_junk_words()
    leaked = [w for w in FLAVORED_NOT_JUNK if any(w in entry for entry in junk)]
    assert not leaked, (
        "Discipline/format/career-stage words must NOT ship as universal junk "
        "(a student/career-changer wants them) — move to profile "
        "exclude_title_keywords instead: " + ", ".join(leaked)
    )


def test_universal_junk_is_sourced_from_toml():
    """config.UNIVERSAL_JUNK must equal the TOML data, proving it is not a
    hardcoded literal in config.py."""
    import sys
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import config
    import settings
    assert config.UNIVERSAL_JUNK == settings.junk()["words"]
    assert config.UNIVERSAL_JUNK_SUBSTR == settings.junk()["substr"]


# ---------------------------------------------------------------------------
# 8. Geography symmetry — no country/region proper noun may be BAKED INTO CODE.
#
# Geography preference is profile data, not code. A country/region proper noun
# is allowed ONLY as a data value (a list item, a dict VALUE, a string in a data
# table). It must NEVER appear as:
#   (a) part of a `def` function name           — e.g. def _is_usa_only(...)
#   (b) a category-key string literal            — e.g. "delete_usa", "delete_cis"
#   (c) a string literal inside an if/elif test  — e.g. if region == "usa":
# These are the three ways geography gets "branched in code", privileging one
# place. The neutral data lives in config/defaults.toml; the LOGIC stays
# place-agnostic (see geo.py — a table walk, not a per-country branch).
# ---------------------------------------------------------------------------

# Proper nouns that name a specific country / region. Bucket labels that are
# inherently structural-and-neutral ("us"/"uk"/"europe"/"americas") are the data
# vocabulary geo.py legitimately RETURNS and stores; the guard does not flag them
# as data values, only when they are welded into a def name, category key, or
# branch literal. We therefore include the country proper nouns that have no
# business being in code at all, PLUS the legacy geo category fragments.
_GEO_PROPER_NOUNS = (
    "usa", "cis", "united_states", "us_only", "row_only",
    "canada", "russia", "georgia", "armenia", "turkey", "nigeria",
    "tbilisi", "istanbul", "lagos", "moscow",
)

# (a) def names containing a geo proper noun.
_DEF_GEO = re.compile(
    r"\bdef\s+\w*(?:" + "|".join(_GEO_PROPER_NOUNS) + r")\w*\s*\(",
    re.IGNORECASE,
)

# (b) category-key string literals carrying a geo proper noun. Catches the
#     historical delete_usa / delete_cis / delete_row family and any sibling.
_CATEGORY_GEO = re.compile(
    r"""["'](?:delete|cat|category|bucket)_(?:usa|us|cis|row|canada)["']""",
    re.IGNORECASE,
)

# (c) if/elif comparing a variable to a geo proper-noun STRING LITERAL. This is a
#     per-place branch (privileging one country). A comparison against a NAMED
#     SET/var (e.g. `country in _COUNTRY_MAP[key]`) is data-driven and allowed.
_IF_GEO_LITERAL = re.compile(
    r"""\b(?:if|elif)\b.*(?:==|!=|\bin\b)\s*\(?\s*["']"""
    + r"(?:" + "|".join(_GEO_PROPER_NOUNS) + r")"
    + r"""["']""",
    re.IGNORECASE,
)


def _scan_lines(files):
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            yield p, i, line


def test_no_geo_proper_noun_in_def_names():
    hits = []
    for p, i, line in _scan_lines(_py_files(SCRIPTS)):
        if _DEF_GEO.search(line):
            hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, (
        "A country/region proper noun is welded into a function name — geography "
        "must be profile data, not a named branch:\n" + "\n".join(hits)
    )


def test_no_geo_proper_noun_in_category_keys():
    hits = []
    for p, i, line in _scan_lines(_py_files(SCRIPTS)):
        if _CATEGORY_GEO.search(line):
            hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, (
        "A geography category key (delete_usa / delete_cis / …) leaked — collapse "
        "geo deletes into one neutral 'delete_geo' key:\n" + "\n".join(hits)
    )


def test_no_geo_proper_noun_in_branch_literals():
    hits = []
    for p, i, line in _scan_lines(_py_files(SCRIPTS)):
        if _IF_GEO_LITERAL.search(line):
            hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()[:120]}")
    assert not hits, (
        "An if/elif branches on a country/region proper-noun literal — "
        "geography must be a data-table lookup, not a per-place branch:\n"
        + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 9. Owner-infra / owner-shell / owner-locale traces.
#
# A public repo must not document or reference the maintainer's private infra
# (OpenClaw SSH service), branded project copy ("Mission-Driven", "Job Search
# 2026"), the maintainer's shell bootstrap ("source ~/.zshrc"), or a hardcoded
# locale ("ru-RU"). These leak the owner and break a clean public setup.
#
# Scanned surface: scripts/, tests/, all of .claude/, docs/index.html, and the
# root README / INSTALL / AGENTS docs. (Other agents own the non-python
# occurrences; the orchestrator runs this guard last, after every agent is done.)
# ---------------------------------------------------------------------------

ROOT_DOCS = ("README.md", "INSTALL.md", "INSTALL-EASY.md", "AGENTS.md")


def _owner_trace_files() -> list[Path]:
    """Every public-surface file this guard polices for owner traces."""
    files: list[Path] = []
    # All python under scripts/ + tests/ (minus this guard + caches).
    files.extend(_py_files(SCRIPTS, TESTS))
    # The whole .claude/ tree (commands, skills, configs — any text file).
    if CLAUDE.exists():
        for p in CLAUDE.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                files.append(p)
    # Public dashboard HTML.
    if DOCS_INDEX.exists():
        files.append(DOCS_INDEX)
    # Root onboarding docs.
    for name in ROOT_DOCS:
        p = REPO / name
        if p.exists():
            files.append(p)
    # De-dup while preserving order.
    seen = set()
    out = []
    for p in files:
        if p not in seen and p.name != SELF:
            seen.add(p)
            out.append(p)
    return out


# Owner-infra / owner-branding tokens. Case sensitivity is chosen per token:
#   - "openclaw"          : case-insensitive (private SSH service, any casing).
#   - "Mission-Driven"    : case-SENSITIVE owner branding — lowercase
#                           "mission-driven" is legitimate prose in a job posting.
#   - "Job Search 2026"   : case-sensitive owner project name.
#   - "source ~/.zshrc"   : the maintainer's shell bootstrap.
#   - "ru-RU"             : a hardcoded locale (the pipeline ships language-neutral).
_OWNER_TRACE_CI = re.compile(r"openclaw", re.IGNORECASE)
_OWNER_TRACE_CS = re.compile(
    r"Mission-Driven|Job Search 2026|source ~/\.zshrc|ru-RU"
)


def test_no_owner_infra_or_branding_traces():
    files = _owner_trace_files()
    hits = _scan(files, _OWNER_TRACE_CI) + _scan(files, _OWNER_TRACE_CS)
    assert not hits, (
        "Owner infra / branding / shell / locale trace leaked "
        "(openclaw / Mission-Driven / Job Search 2026 / source ~/.zshrc / "
        "ru-RU):\n" + "\n".join(hits)
    )
