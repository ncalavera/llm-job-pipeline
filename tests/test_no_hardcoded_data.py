"""Guard test — the permanent regression net for a PUBLIC, owner-agnostic repo.

This file is itself PUBLIC, so it must not embed any maintainer-private strings
(names, handles, infra, owner orgs). It therefore splits into two layers:

GENERIC guards (always run, contain zero private tokens — they describe SHAPES
that should never appear in ANY public fork):
  - Internal issue / ticket references (DHA-NNN, (#NNN), issue #NNN).
  - Owner-narration phrasing in comments ("the owner", "used to be", …).
  - Deleted owner-data constant *name shapes* (Devex / Impactpool-EU / prestige
    / manual-allowlist / animal-welfare / relevance families).
  - A non-empty literal ``board_blacklist=[...]`` inside scripts/*.py
    (boards must ship neutral; exclusion is user opt-in via the profile).
  - Universal junk that is actually discipline / format / career-stage flavored
    (bootcamp, fellowship, internship, volunteer, …).
  - Geography proper nouns welded into code (def names / category keys / branch
    literals).

PRIVATE guards (owner identity, owner orgs, owner infra/branding) — the literal
tokens live ONLY in an optional, gitignored local file
``tests/private_patterns.local.json`` (template:
``private_patterns.local.example.json``). The public test LOADS that file if
present and SKIPS the private checks if absent, so the public repo never ships
the maintainer's private strings while the maintainer can still scan locally.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
PROMPTS = SCRIPTS / "prompts"
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


def _prompt_files() -> list[Path]:
    """The LLM prompt templates under scripts/prompts/ (owner-agnostic by
    contract: the desirability rubric, salary benchmark and reference orgs come
    from the user profile, NOT from these shipped templates)."""
    if not PROMPTS.exists():
        return []
    return sorted(PROMPTS.glob("*.md"))


def _text_files() -> list[Path]:
    """Python + the LLM prompt templates + the public dashboard HTML + .claude
    command markdown."""
    files = _py_files(SCRIPTS, TESTS)
    files.extend(_prompt_files())
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
# Optional, gitignored local private-pattern source.
#
# Keeps maintainer-private literals (identity / orgs / infra) OUT of this public
# file. Present → the private checks run against it; absent → they skip.
# ---------------------------------------------------------------------------

PRIVATE_PATTERNS_FILE = TESTS / "private_patterns.local.json"


def _load_private_patterns() -> dict:
    if not PRIVATE_PATTERNS_FILE.exists():
        return {}
    try:
        return json.loads(PRIVATE_PATTERNS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _private_check(name: str) -> tuple[re.Pattern | None, re.Pattern | None]:
    """Compile the (case-insensitive, case-sensitive) regexes for one private
    check from the local file. Returns (None, None) when the file or the check's
    patterns are absent, so the caller can skip cleanly."""
    data = _load_private_patterns()
    spec = data.get(name)
    if not isinstance(spec, dict):
        return None, None
    ci_alts = [a for a in spec.get("ci", []) if a]
    cs_alts = [a for a in spec.get("cs", []) if a]
    ci = re.compile("|".join(ci_alts), re.IGNORECASE) if ci_alts else None
    cs = re.compile("|".join(cs_alts)) if cs_alts else None
    return ci, cs


def _run_private_check(name: str, files, label: str):
    ci, cs = _private_check(name)
    if ci is None and cs is None:
        pytest.skip(
            f"no local private patterns for {name!r} "
            f"(create tests/private_patterns.local.json from the .example.json "
            f"to enable the maintainer-only {label} scan)"
        )
    hits: list[str] = []
    if ci is not None:
        hits += _scan(files, ci)
    if cs is not None:
        hits += _scan(files, cs)
    assert not hits, f"{label} leaked:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 1. Owner identity  (PRIVATE — patterns loaded from the optional local file)
# ---------------------------------------------------------------------------


def test_no_owner_identity():
    _run_private_check("owner_identity", _text_files(), "Owner identity")


# ---------------------------------------------------------------------------
# 2. Owner-specific organisations  (PRIVATE — optional local file)
# ---------------------------------------------------------------------------


def test_no_owner_orgs():
    # Scan code, tests, the public dashboard HTML and .claude command docs.
    _run_private_check("owner_orgs", _text_files(), "Owner org names")


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
    "bootcamp",
    "fellowship",
    "internship",
    "intern",
    "graduate",
    "volunteer",
    "course",
    "summer school",
    "training on",
    "junior",
    "senior",
    "engineer",
    "developer",
    "nurse",
    "sales",
    "funding",
    "grant",
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
    "usa",
    "cis",
    "united_states",
    "us_only",
    "row_only",
    "canada",
    "russia",
    "georgia",
    "armenia",
    "turkey",
    "nigeria",
    "tbilisi",
    "istanbul",
    "lagos",
    "moscow",
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
    + r"(?:"
    + "|".join(_GEO_PROPER_NOUNS)
    + r")"
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
        "geography must be a data-table lookup, not a per-place branch:\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 8b. No literal money amount in the LLM prompt templates.
#
# The scoring rubric must take the candidate's salary benchmark and every other
# money anchor FROM THE USER PROFILE, never from a figure baked into the shipped
# template (the leak was a "€7k/month benchmark" welded into company-scoring.md).
# A currency-symbol-plus-digit, a currency-code-plus-digit (either order), or a
# "<n>k/month"-style pay figure (with "/", "per", or "a" as the connector) in a
# prompt is therefore always a regression. Score bands ("85–100"), runway
# durations ("18–24 months") and a bare "$X" placeholder carry no currency+digit
# pairing, so they do not trip this guard.
# ---------------------------------------------------------------------------

MONEY_LITERAL = re.compile(
    r"[€$£¥₹₽]\s?\d"  # €7, $5,000, £4000, ₹50000, ₽70000
    r"|\b\d[\d,.]*\s?k?\s?(?:/|per|a)\s?(?:month|mo|year|yr|hour|hr)\b"  # 7k/month, 7k per month, 5,000 a month
    r"|\b(?:USD|EUR|GBP|CAD|AUD|CHF|JPY)\s?\d[\d,.]*k?\b"  # USD 7k, EUR 5000 (code before amount)
    r"|\b\d[\d,.]*k?\s?(?:USD|EUR|GBP|CAD|AUD|CHF|JPY)\b"  # 5000 USD, 7k EUR (amount before code)
    r"|\b\d[\d,.]*\s?k?\s?(?:dollars?|euros?)\b",  # 7000 dollars, 5k euros (spelled out)
    re.IGNORECASE,
)


def test_no_money_amount_in_prompt_templates():
    hits = _scan(_prompt_files(), MONEY_LITERAL)
    assert not hits, (
        "A literal money amount leaked into an LLM prompt template — the salary "
        "benchmark and every money anchor must come from the user profile, not a "
        "figure baked into the shipped prompt:\n" + "\n".join(hits)
    )


# Regression cases for the MONEY_LITERAL regex itself (not file scans) — each
# form below was a mutation-confirmed miss of the previous, narrower pattern.
_MONEY_LITERAL_POSITIVE_CASES = [
    "€7,000/month",
    "$5000/mo",
    "£4000/yr",
    "5000 USD",
    "USD 7k",  # currency-code-first, no connector
    "USD 7k per month",  # currency-code-first + "per" connector, no slash
    "EUR 5000",  # currency-code-first
    "7k EUR",  # currency-code-suffix with "k" shorthand
    "7k per month",  # amount + "per month", no slash, no currency code
    "5,000 a month",  # amount + "a month", no slash, no currency code
    "CHF 6000",  # Swiss franc code
    "JPY 500000",  # Japanese yen code
    "₹50000",  # Indian rupee symbol
    "₽70000",  # Russian ruble symbol
    "7000 dollars",  # spelled-out currency
    "5k euros",  # spelled-out currency
]

_MONEY_LITERAL_NEGATIVE_CASES = [
    "Score bands: 85–100, 60–74",
    "18–24 months runway",
    "a bare $X placeholder",
    "Series C+/revenue-positive startup",
    "0→1 with cushion",
]


@pytest.mark.parametrize("text", _MONEY_LITERAL_POSITIVE_CASES)
def test_money_literal_regex_catches_known_forms(text):
    assert MONEY_LITERAL.search(text), f"MONEY_LITERAL should catch: {text!r}"


@pytest.mark.parametrize("text", _MONEY_LITERAL_NEGATIVE_CASES)
def test_money_literal_regex_ignores_non_money(text):
    assert not MONEY_LITERAL.search(text), f"MONEY_LITERAL should NOT catch: {text!r}"


# ---------------------------------------------------------------------------
# 8c. No impact-sector / social-good worldview language in the LLM prompt
# templates.
#
# The seven WANT dimensions in company-scoring.md must be defined relative to
# the CANDIDATE's own stated mission / values / domain interests (injected
# from the profile via USER_PROFILE / TARGET_ROLES / EXCLUDE_PATTERNS), never
# relative to a baked-in social-good/NGO/charity frame (the leak was
# mission_authenticity being defined as "is the social good direct", with
# grantmaking/CSR/"traditional-NGO" vocabulary throughout). A devtools company
# scored against an engineer profile must be able to score high on
# mission_authenticity without the template implying charity work is the
# reference point. Tokens are tuned to avoid legitimate false positives (e.g.
# "root cause", "CSRF", "grants", "nonprofit" as a funding-structure label are
# all still allowed).
# ---------------------------------------------------------------------------

WORLDVIEW_TOKEN = re.compile(
    r"social good"
    r"|\bNGOs?\b"
    r"|grantmaking"
    r"|charit(?:y|ies|able)"
    r"|\bcauses\b"
    r"|programme areas?\b"
    r"|\bCSR\b",
    re.IGNORECASE,
)


def test_no_worldview_frame_in_prompt_templates():
    hits = _scan(_prompt_files(), WORLDVIEW_TOKEN)
    assert not hits, (
        "Impact-sector/social-good worldview language leaked into an LLM prompt "
        "template — the WANT dimensions must be defined relative to the "
        "candidate's own stated mission/values from the profile, not a baked-in "
        "sector frame:\n" + "\n".join(hits)
    )


# ---------------------------------------------------------------------------
# 9. Owner-infra / owner-shell / owner-locale traces.  (PRIVATE — optional file)
#
# A public repo must not document or reference the maintainer's private infra
# (a private SSH service), branded project copy, the maintainer's shell
# bootstrap, or a hardcoded locale — these leak the owner and break a clean
# public setup. The literal tokens live ONLY in the optional local file under
# the "owner_infra" check, so this public guard names none of them.
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


# Owner-infra / owner-branding tokens are PRIVATE and live only in the optional
# local file under the "owner_infra" check. Case sensitivity is preserved there
# via the ci/cs split: identity-like tokens go in "ci" (matched any casing),
# while branding whose lowercase form is legitimate prose goes in "cs" (matched
# case-sensitively). The public test ships no such literal.


def test_no_owner_infra_or_branding_traces():
    _run_private_check(
        "owner_infra", _owner_trace_files(), "Owner infra / branding / shell / locale trace"
    )
