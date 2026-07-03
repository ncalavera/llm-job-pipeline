"""Profile-derived targeting — board recommendations + LinkedIn queries.

The "which boards fit me?" and "what should LinkedIn search for?" questions must
be answered from the USER's profile (target field, roles, geography), never from
a maintainer-shaped default baked into the shipped config (STRATEGY guardrail 1).
This module is the single, DB-free place that derives both:

  - ``recommend_boards()`` — proposes the boards whose own audience overlaps the
    profile. An engineer profile proposes engineering boards; it does NOT
    auto-receive six impact boards. LinkedIn (a ``general`` board) fits any field
    because it derives its own queries from the profile, so it is always proposed.
    Proposing is all it does — nothing is enabled here (STRATEGY guardrail 8);
    the user enables what they want via ``scripts/sources.py enable-board``.

  - ``resolve_linkedin_queries()`` — the LinkedIn search set. Resolution order:
      1. an explicit ``## LINKEDIN_QUERIES`` profile section wins (verbatim);
      2. else queries are DERIVED from ``## TARGET_ROLES`` (+ target geography);
      3. else an empty list (the shipped board carries NO queries).
    Editing the profile — never ``config/defaults.toml`` — changes the search.

Reuses ``prompts._load_user_profile`` so it reads exactly the same profile file
as scoring and the hard filters. Never raises: a missing profile / section
degrades to "no recommendation / no derived queries", never a crash.
"""

from __future__ import annotations

import functools
import re

import settings
from prompts import EXAMPLE_PROFILE_PATH, _load_user_profile

# Strip HTML comment blocks so the example lines inside a template's explanatory
# comment are never parsed as real values (matches hard_filters / scoring_settings).
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# The shipped example profile teaches ONE placeholder convention: guidance to
# delete sits inside square brackets ("The `[brackets]` show what goes where —
# delete the guidance once you have filled them in"), and pure format samples are
# bullet lines that open with "e.g." and quote a sample. A profile only
# half-edited from that template still carries those markers, and their topic
# words (a nurse / impact / public-policy example the user never deleted) would
# otherwise be keyword-matched as if the user had CHOSEN that field — the
# board-recommendation regression this module exists to prevent (an engineer
# handed impact/nonprofit boards). Both markers are stripped before any targeting
# keyword match. The rules key on the markers themselves, never on topic words,
# so a user's real prose passes through untouched.
#
# ``[^\]]`` spans newlines, so a placeholder wrapped across two lines
# (``[the fields … healthcare,\ngames …]``) is removed whole. The ``(?!\()``
# tail leaves a Markdown-link label ``[text](url)`` alone — that label is the
# user's own content, not template guidance.
_PLACEHOLDER_SPAN = re.compile(r"\[[^\]]*\](?!\()")


def _norm_line(line: str) -> str:
    """Normalise a line for comparison: drop a leading bullet + emphasis marks,
    collapse whitespace, lowercase — so a profile line matches the example's own
    sample lines regardless of bullet style or spacing."""
    line = re.sub(r"^\s*[-•*·]+\s*", "", line.strip())
    line = re.sub(r"\*+", "", line)
    return re.sub(r"\s+", " ", line).strip().lower()


@functools.lru_cache(maxsize=1)
def _example_scaffolding_lines() -> frozenset[str]:
    """Normalised lines of the SHIPPED example that are unedited scaffolding:

      * every ``e.g.``-prefixed FORMAT SAMPLE anywhere in the example (e.g. the
        "Professional experience" samples), and
      * every non-blank line of the example's ``## TARGET_ROLES`` section — its
        GUIDANCE PROSE included. That prose ("The exact job titles you want to
        see — one per line or comma-separated. Any field; … from different
        careers, …") splits on its commas/semicolons into stray one-word
        "roles" (``field``, ``careers``) that would otherwise become live
        LinkedIn search queries in a half-copied profile.

    A line is scaffolding only when it matches one of these VERBATIM (normalised)
    — the whole point of keying off the example's own text, never off a topic
    word. A user who typed their OWN roles over the template (so the line no
    longer matches) keeps them; nothing is dropped on a topic guess. Degrades to
    an empty set (strip nothing) if the example is unreadable — safer to
    under-strip than to delete a user's content.
    """
    try:
        text = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    out: set[str] = set()
    in_target_roles = False
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            in_target_roles = heading.group(1).upper().replace(" ", "_") == "TARGET_ROLES"
            continue
        norm = _norm_line(line)
        if not norm:
            continue
        if norm.startswith("e.g.") or in_target_roles:
            out.add(norm)
    return frozenset(out)


def _strip_scaffolding(text: str) -> str:
    """Remove unedited example-template scaffolding from ``text``.

    Three markers, each keyed off the example itself, never off a topic word:
      1. HTML comment blocks (``<!-- … -->``);
      2. lines that are VERBATIM scaffolding lines from the shipped example — its
         ``e.g.`` samples and its ``## TARGET_ROLES`` guidance prose (see
         ``_example_scaffolding_lines``); a user's own text edited over the
         template is not a shipped line, so it survives;
      3. ``[…]`` placeholder spans, except a Markdown-link label ``[text](url)``.
    """
    text = _HTML_COMMENT.sub("", text or "")
    samples = _example_scaffolding_lines()
    kept = [line for line in text.splitlines() if _norm_line(line) not in samples]
    return _PLACEHOLDER_SPAN.sub(" ", "\n".join(kept))


# Values that mean "the user left this empty".
_EMPTY_TOKENS = {"", "(none)", "none", "-", "n/a", "na"}

# A derived LinkedIn set is capped so a big TARGET_ROLES list can never fan out
# into dozens of throttled LinkedIn requests (the board rate-limits hard).
_MAX_DERIVED_QUERIES = 10
_MAX_DERIVED_ROLES = 6
_MAX_DERIVED_LOCATIONS = 2

# The neutral fallback location for a derived query: remote works for any field
# and any country, so a profile with no stated geography still gets usable
# queries. City/region targeting is opt-in via an explicit ## LINKEDIN_QUERIES.
_DEFAULT_LOCATION = "Remote"

# Line prefixes that begin the "roles I do NOT want" tail of TARGET_ROLES. Both
# the board matcher and the query derivation stop here so a negative term
# ("Not a target: sales, marketing") never pulls in a board or becomes a query.
_AVOID_PREFIXES = ("not a target", "**not a target", "avoid", "**avoid")


def _positive_roles_body(body: str) -> str:
    """The TARGET_ROLES body up to (not including) its 'Not a target' tail."""
    kept: list[str] = []
    for line in (body or "").splitlines():
        if line.strip().lower().startswith(_AVOID_PREFIXES):
            break
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Profile access
# ---------------------------------------------------------------------------


def _sections() -> dict[str, str]:
    """The parsed profile sections, or {} when the profile is unavailable."""
    try:
        return _load_user_profile()
    except Exception:
        return {}


def _profile_text(sections: dict[str, str]) -> str:
    """A lowercased haystack of the profile's targeting-relevant sections.

    Board matching looks for a board's audience tag anywhere the user described
    their field / roles / domain, so it reads USER_PROFILE + TARGET_ROLES (the
    positive targeting sections). EXCLUDE_PATTERNS is deliberately NOT included:
    a word the user wants to AVOID must not pull in a board. Unedited example
    scaffolding is stripped first (see ``_strip_scaffolding``) so a half-filled
    profile's leftover impact/nonprofit example words can't recommend a board.
    """
    parts = [
        sections.get("USER_PROFILE", ""),
        _positive_roles_body(sections.get("TARGET_ROLES", "")),
    ]
    return _strip_scaffolding("\n".join(parts)).lower()


# ---------------------------------------------------------------------------
# Board recommendations
# ---------------------------------------------------------------------------


def _board_tags(cfg: dict) -> list[str]:
    tags = cfg.get("audience_tags")
    return (
        [str(t).lower().strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
    )


def _tag_in_text(tag: str, text: str) -> bool:
    """Whole-word (or whole-phrase) match, so a short tag never false-matches a
    substring — e.g. 'un' must not match 'backgro-un-d', 'ai' not 'em-ai-l'."""
    return re.search(r"\b" + re.escape(tag) + r"\b", text) is not None


def recommend_boards(
    sections: dict[str, str] | None = None, boards: dict | None = None
) -> list[dict]:
    """Boards whose audience overlaps the profile — ``[{id, name, reason}]``.

    A board matches when one of its ``audience_tags`` (a neutral fact about the
    BOARD, e.g. Remotive = remote/engineering) appears in the profile's
    field/role text. ``general`` boards (LinkedIn) always match — they adapt to
    any field. When nothing else matches, only the general board(s) are returned
    with a "browse the catalogue" reason, so a niche profile still gets a start
    without six irrelevant boards auto-attached.

    Ordering: general boards first (they always fit), then tag matches sorted by
    how many tags overlapped (strongest first), then board id for stability.
    """
    if sections is None:
        sections = _sections()
    if boards is None:
        boards = settings.boards()
    text = _profile_text(sections)

    # Only recommend boards this repo can actually fetch. Catalogue entries
    # whose strategy has no registered fetcher (e.g. the consider_board VC
    # aggregators) would be enabled, return zero every run, and never say why —
    # the silent-degradation class the guards exist for (review finding on #44).
    from fetchers import BOARD_FETCHERS

    general: list[dict] = []
    matched: list[tuple[int, str, dict]] = []
    for bid, cfg in boards.items():
        if str(cfg.get("strategy", "")) not in BOARD_FETCHERS:
            continue
        name = str(cfg.get("name", bid))
        if cfg.get("general"):
            general.append(
                {
                    "id": bid,
                    "name": name,
                    "reason": "works for any field (queries come from your profile)",
                }
            )
            continue
        hits = [t for t in _board_tags(cfg) if t and _tag_in_text(t, text)]
        if hits:
            reason = "matches your profile: " + ", ".join(sorted(hits)[:4])
            matched.append((len(hits), bid, {"id": bid, "name": name, "reason": reason}))

    matched.sort(key=lambda m: (-m[0], m[1]))
    ordered_matches = [m[2] for m in matched]

    if not ordered_matches:
        for g in general:
            g["reason"] = (
                "works for any field; no niche board strongly matched your profile — "
                "browse the full catalogue and enable any that fit"
            )
    return general + ordered_matches


# ---------------------------------------------------------------------------
# LinkedIn queries
# ---------------------------------------------------------------------------


def _clean_role(phrase: str) -> str:
    """Normalise one raw role phrase into a LinkedIn keyword string, or ""."""
    # Drop a leading "**Label:**" style track name and any parenthetical aside.
    phrase = re.sub(r"\*+", "", phrase)
    phrase = re.sub(r"\([^)]*\)", " ", phrase)
    phrase = phrase.strip().strip("-•*·.").strip()
    # A user who typed their OWN roles into an example's `e.g. "…"` line without
    # deleting the decoration keeps that line (it is not a verbatim shipped
    # sample), so scrub the stray `e.g.` lead-in and quote marks here — a real
    # role keyword never carries either — leaving the role itself searchable.
    phrase = re.sub(r'["“”]', "", phrase)
    phrase = re.sub(r"^\s*e\.g\.[.,:]?\s*", "", phrase, flags=re.IGNORECASE)
    # A "Track name: role, role" prefix — keep only the part after the colon.
    if ":" in phrase:
        phrase = phrase.split(":", 1)[1]
    phrase = re.sub(r"\s+", " ", phrase).strip(" .,-")
    words = phrase.split()
    if not (1 <= len(words) <= 6):
        return ""
    return phrase


def _derive_role_keywords(target_roles_body: str) -> list[str]:
    """Extract clean role keyword strings from a TARGET_ROLES section body.

    Stops at a "Not a target"/"Avoid" line so excluded roles are never searched.
    Splits bullet/comma/semicolon lists, strips markdown and parentheticals, and
    de-dupes case-insensitively. Best-effort ("sane queries", not perfect
    parsing) — the clean path is an explicit ## LINKEDIN_QUERIES section.
    """
    body = _positive_roles_body(_strip_scaffolding(target_roles_body or ""))
    roles: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        for chunk in re.split(r"[,;]", line):
            role = _clean_role(chunk)
            if not role:
                continue
            key = role.lower()
            if key in seen or key in _EMPTY_TOKENS:
                continue
            seen.add(key)
            roles.append(role)
            if len(roles) >= _MAX_DERIVED_ROLES:
                return roles
    return roles


def _derive_locations(sections: dict[str, str]) -> list[str]:
    """Target locations for derived queries, from a ``Target locations:`` line.

    Scans every section body for a line like
    ``**Target locations:** Berlin (DE), London (UK), remote-EU`` and cleans each
    token (drop the parenthetical code, normalise any 'remote…' token to
    'Remote'). Falls back to ['Remote'] — a neutral default that works for any
    field and country. Capped so queries never fan out too wide.
    """
    raw_line = ""
    pat = re.compile(r"^(?P<prefix>.*?)target locations?\s*:\s*(?P<rest>.+)", re.IGNORECASE)
    # A negation right before the phrase means an EXCLUSION line
    # ("Not target locations: US") — deriving those as search locations would
    # search exactly where the user said not to (review nit on #44).
    negated = re.compile(r"\b(?:not|no|never|excluded?)\W*$", re.IGNORECASE)
    for body in sections.values():
        # Strip scaffolding so an unedited "**Target locations:** [where you'd
        # work — cities…]" placeholder is not parsed as real target locations.
        for line in _strip_scaffolding(body or "").splitlines():
            m = pat.search(line)
            if m and not negated.search(m.group("prefix")):
                raw_line = m.group("rest")
                break
        if raw_line:
            break
    if not raw_line:
        return [_DEFAULT_LOCATION]

    raw_line = re.sub(r"\*+", "", raw_line)
    out: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[,/]", raw_line):
        token = re.sub(r"\([^)]*\)", " ", token).strip(" .*-")
        token = re.sub(r"\s+", " ", token).strip()
        if not token or token.lower() in _EMPTY_TOKENS:
            continue
        if "remote" in token.lower():
            token = _DEFAULT_LOCATION
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= _MAX_DERIVED_LOCATIONS:
            break
    return out or [_DEFAULT_LOCATION]


def _parse_explicit_queries(body: str) -> list[dict]:
    """Parse a ``## LINKEDIN_QUERIES`` body into ``[{keywords, location}]``.

    One query per line, ``keywords | location`` (location optional). Blank lines
    and comment lines are ignored. A line with no keywords is skipped.
    """
    body = _HTML_COMMENT.sub("", body or "")
    out: list[dict] = []
    for line in body.splitlines():
        line = line.strip().lstrip("-•*").strip()
        if not line or line.startswith("#"):
            continue
        keywords, _, location = line.partition("|")
        keywords = keywords.strip()
        location = location.strip()
        if not keywords or keywords.lower() in _EMPTY_TOKENS:
            continue
        out.append({"keywords": keywords, "location": location})
    return out


def resolve_linkedin_queries(sections: dict[str, str] | None = None) -> list[dict]:
    """The effective LinkedIn query set for this profile — ``[{keywords, location}]``.

    Resolution order (see module docstring): explicit ``## LINKEDIN_QUERIES``
    wins; otherwise derive from ``## TARGET_ROLES`` × target geography; otherwise
    an empty list. Never raises.
    """
    if sections is None:
        sections = _sections()

    explicit = _parse_explicit_queries(sections.get("LINKEDIN_QUERIES", ""))
    if explicit:
        return explicit[:_MAX_DERIVED_QUERIES] if len(explicit) > _MAX_DERIVED_QUERIES else explicit

    roles = _derive_role_keywords(sections.get("TARGET_ROLES", ""))
    if not roles:
        return []
    locations = _derive_locations(sections)

    queries: list[dict] = []
    for role in roles:
        for loc in locations:
            queries.append({"keywords": role, "location": loc})
            if len(queries) >= _MAX_DERIVED_QUERIES:
                return queries
    return queries
