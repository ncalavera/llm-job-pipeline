"""Single home for vacancy filtering — the quality axis (is this a real,
in-scope job posting?) plus the cross-board fuzzy-dedup matcher.

Pure functions only: no DB cursor, no clock, no network. Everything here
decides on the CONTENT of a vacancy (title, description, locations). Gates that
depend on time or neighbouring rows (archive cooldown, age splits) stay with
their callers — this module only supplies the predicates.

Dependency rule (enforced by a test): filters.py imports ONLY config, quality
and stdlib. It must NOT import the data-access layer (database_supabase /
db_conn / db_backend), so the DAL can import filters without a cycle.
"""

import html
import re
from difflib import SequenceMatcher

from config import (
    GLOBAL_BLACKLIST,
    GLOBAL_BLACKLIST_SUBSTR,
    GLOBAL_BLACKLIST_DESC_SUBSTR,
    COMPANY_TITLE_FILTERS,
    COMPANY_NEVER_FETCH,
    resolve_canonical_name,
)

# ---------------------------------------------------------------------------
# Blacklist constants — self-describing names, data sourced from config.
# config.py keeps the legacy GLOBAL_* names for backward compatibility; we only
# introduce clearer aliases here. Values are NOT changed.
# ---------------------------------------------------------------------------

#: Whole-word title blacklist (matched on \b boundaries, case-insensitive).
TITLE_BLACKLIST_WORDS = GLOBAL_BLACKLIST
#: Substring title blacklist (matched anywhere in lower(title), no boundaries).
TITLE_BLACKLIST_STEMS = GLOBAL_BLACKLIST_SUBSTR
#: Narrow kill phrases matched as substrings in lower(description).
DESCRIPTION_BLACKLIST_PHRASES = GLOBAL_BLACKLIST_DESC_SUBSTR


# ---------------------------------------------------------------------------
# Title / description blacklist
# ---------------------------------------------------------------------------


def build_title_blacklist_pattern(words) -> re.Pattern:
    """Compile the whole-word title-blacklist regex for an arbitrary keyword
    list — one alternation, keywords sorted by length desc to prevent shorter
    substrings matching prematurely. A trailing (?:es|s)? lets a singular
    keyword also catch its plural, so "developer" matches "developers",
    "fellow" -> "fellows", "coach" -> "coaches" without listing every plural by
    hand.

    Boundaries are computed PER KEYWORD from its own edge characters, not with a
    single ``\\b`` wrapping the whole alternation. A ``\\b`` only sits between a
    word char and a non-word char, so wrapping a keyword that ENDS in a non-word
    character (``c++``, ``c#``) in a trailing ``(?:es|s)?\\b`` produces a regex
    that can never match a real title — "C++ Developer" has a space after the
    "+", so the ``\\b`` fails and the keyword is a silent no-op. Each keyword
    therefore gets: a non-word-char guard on whichever edge is a word char (so it
    still matches as a whole token), the plural suffix ONLY when it ends in a
    word char, and a plain "not glued to another word char" guard on a
    punctuation edge.

    This is the ONE place the live title filter's matching semantics are
    defined — callers that need to know what the filter would do to a candidate
    word (e.g. the learning cycle's backtest) must call this instead of
    hand-rolling an equivalent regex, or the two can drift apart.
    """
    parts = []
    for kw in sorted(words, key=len, reverse=True):
        if not kw:
            continue
        head = r"\b" if kw[0].isalnum() or kw[0] == "_" else r"(?<!\w)"
        if kw[-1].isalnum() or kw[-1] == "_":
            tail = r"(?:es|s)?\b"  # word-ending keyword: plural + word boundary
        else:
            tail = r"(?!\w)"  # punctuation-ending keyword (c++, c#): no \b, no plural
        parts.append(head + re.escape(kw) + tail)
    if not parts:
        parts.append(r"(?!x)x")  # matches nothing — an empty keyword list drops nothing
    return re.compile("(?:" + "|".join(parts) + ")", re.IGNORECASE)


# Pre-compiled blacklist (~10-50x faster than iterating individual re.search()
# calls per vacancy) — compiled once at module load from the real keyword list.
_TITLE_BLACKLIST_PATTERN = build_title_blacklist_pattern(TITLE_BLACKLIST_WORDS)


def title_blacklist_match(title: str) -> str | None:
    """The substring stem or whole-word blacklist term the title hits, else None."""
    t = (title or "").lower()
    for kw in TITLE_BLACKLIST_STEMS:
        if kw in t:
            return kw
    m = _TITLE_BLACKLIST_PATTERN.search(t)
    return m.group(0) if m else None


def title_words_blacklisted(title: str) -> bool:
    """True when the title hits a substring stem or a whole-word blacklist term."""
    return title_blacklist_match(title) is not None


# ---------------------------------------------------------------------------
# Not-a-vacancy titles
#
# EA-ecosystem boards list programs, courses, grants and talent directories
# side by side with jobs. Each one that reaches scoring costs a real Opus call
# (five of the first twenty items on the night of 2026-08-27) and can never be
# a match, because it is not a role anyone is hired for.
#
# The discriminator is title SHAPE, not a keyword: a job title names a person's
# role, an offering names a thing. "Impact Accelerator Program" is a thing;
# "Senior Program Manager, Google DeepMind Impact Accelerator" is a person. So
# an offering word only counts when the title carries NO role noun at all —
# which is why "Program Manager", "Head of Courses", "OCDI Program and Grants
# Associate" and "Career Bootcamp Lead" are all untouched.
# ---------------------------------------------------------------------------

#: Words that name a thing on offer rather than a job.
_OFFERING_RE = re.compile(
    r"\b(?:program|programme|programs|programmes|course|courses|"
    r"accelerator|incubator|incubation|bootcamp|boot camp|"
    r"fellowship|fellowships|scholarship|scholarships|"
    r"grant|grants|funding round|cohort|curriculum|syllabus|"
    r"summit|conference|webinar|workshop|hackathon|"
    r"directory|talent pool|talent directory|newsletter)\b",
    re.IGNORECASE,
)

#: Nouns that name the PERSON doing a job. One of these anywhere in the title
#: means a human is being hired, whatever else the title mentions.
_ROLE_NOUN_RE = re.compile(
    r"\b(?:manager|managers|officer|officers|lead|leader|leads|director|"
    r"head|chief|president|principal|partner|founder|"
    r"associate|assistant|coordinator|specialist|analyst|adviser|advisor|"
    r"consultant|engineer|developer|designer|architect|scientist|researcher|"
    r"strategist|writer|editor|producer|recruiter|counsel|controller|"
    r"administrator|executive|supervisor|representative|"
    r"secretary|treasurer|steward|liaison|ambassador|expert|owner|"
    r"charg\u00e9|chargee|responsable|gestionnaire|"
    r"intern|internship|apprentice|trainee|volunteer|"
    r"vp|svp|evp|cto|ceo|coo|cfo|cpo|cmo)\b",
    re.IGNORECASE,
)

#: Job-description structure. Borrowed verbatim from quality.is_marketing_page,
#: which draws the same line between a real posting and a page about something
#: else. More than one of these and the text is a posting, whatever the title.
_JD_STRUCTURE_RE = re.compile(
    r"responsibilit|qualificat|requirement|you will|we(?:'re| are) looking|"
    r"what you(?:'ll| will)|the role|reports? to|how to apply|"
    r"application deadline|apply (?:now|here|for this|by|via|online)|"
    r"minimum .{0,20}years|job description|key duties|role purpose",
    re.IGNORECASE,
)
_OFFERING_MAX_JD_SIGNALS = 1

#: Recruiter test postings and placeholders. Narrow on purpose — these phrases
#: do not occur in a real title ("QA Test Engineer" carries none of them).
_TEST_POSTING_RE = re.compile(
    r"\bdo not apply\b|\btest job\b|\btest posting\b|\btest vacancy\b|"
    r"\bthis is a test\b|\bdummy (?:job|posting|vacancy)\b|\bignore this\b|"
    r"\bplease ignore\b|\bsample (?:job|posting)\b",
    re.IGNORECASE,
)


def not_a_vacancy_reason(title: str, description: str = "") -> str | None:
    """Why this is not a job posting, or None when it is (or may be) one.

    Two classes only:

      * a recruiter's test placeholder ("US TEST JOB 2026 - DO NOT APPLY").
        Judged on the title alone — the phrases are unambiguous.

      * an offering: a program, course, grant, fellowship or directory. THREE
        conditions must hold together, because no single one is safe. The title
        names an offering, AND it names no person's role, AND the text carries
        no job-description structure. Measured against the whole live database,
        the third condition is what saves "Policy Programs & Partnerships,
        Global Impact" (a real Anthropic role, scored 78) and "GTM Strategy &
        Operations, Strategic Programs" (a real OpenAI role) — both are titles
        with no role noun at all, and both come with a full job description.
    """
    t = (title or "").strip()
    if not t:
        return None
    if _TEST_POSTING_RE.search(t):
        return "test posting, not a real job"
    if not _OFFERING_RE.search(t) or _ROLE_NOUN_RE.search(t):
        return None
    if len(_JD_STRUCTURE_RE.findall(description or "")) > _OFFERING_MAX_JD_SIGNALS:
        return None
    return "a program or grant to apply to, not a job"

# ---------------------------------------------------------------------------
# Per-company title INCLUDE-filters
#
# COMPANY_TITLE_FILTERS (profile ## COMPANY_TITLE_FILTERS) maps a company to a
# list of title include patterns. For a listed company, a role passes only when
# its title matches at least one pattern; unlisted companies are unaffected.
# Keys are ALIAS-PROOF: both the profile spelling and the queried org go through
# resolve_canonical_name (the same alias resolution find_duplicates uses), after
# HTML-entity unescaping, so a board delivering "WFP" still hits an include-list
# declared as "WFP - World Food Programme". Patterns are COMPILED once at import
# so the per-role check is a dict hit plus one regex search.
# ---------------------------------------------------------------------------


def _normalize_company_key(name: str) -> str:
    """Lowercase + whitespace-collapse a company name for filter lookups."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _canonical_company_key(name: str) -> str:
    """Alias-proof lookup key for a company name.

    HTML entities are unescaped first (board feeds ship "&amp;" spellings), then
    the name is resolved through resolve_canonical_name — the same alias
    resolution the cross-board dedup uses — so the profile's canonical spelling
    and a board's alias spelling land on the same key.
    """
    return _normalize_company_key(resolve_canonical_name(html.unescape(name or "")))


def _build_company_title_include(company_filters: dict) -> dict[str, re.Pattern]:
    """``{canonical company key: compiled include-pattern}`` from the profile map.

    The alternation regex is compiled ONCE per company here — never per call.
    Two profile spellings that resolve to the same canonical company merge their
    pattern lists before compiling.
    """
    merged: dict[str, list[str]] = {}
    for company, patterns in company_filters.items():
        if not patterns:
            continue
        bucket = merged.setdefault(_canonical_company_key(company), [])
        for pattern in patterns:
            if pattern not in bucket:
                bucket.append(pattern)
    return {key: build_title_blacklist_pattern(pats) for key, pats in merged.items()}


_COMPANY_TITLE_INCLUDE = _build_company_title_include(COMPANY_TITLE_FILTERS)


def company_title_filter_reason(org: str, title: str) -> str | None:
    """Kill reason when a per-company title include-filter drops this role.

    Returns a reason string naming the rule when ``org`` has an include-list AND
    ``title`` matches none of its patterns; returns None when the company has no
    include-list (unaffected) or the title matches. The reason names the rule so
    a later review can trace the drop:
    ``"company_title_filter — not in <Company> include list"``.
    """
    compiled = _COMPANY_TITLE_INCLUDE.get(_canonical_company_key(org))
    if compiled is None:
        return None
    if compiled.search((title or "").lower()):
        return None
    return f"company_title_filter — not in {org} include list"


# ---------------------------------------------------------------------------
# Whole-company NEVER-FETCH list
#
# COMPANY_NEVER_FETCH (profile ## COMPANY_NEVER_FETCH) names companies the user
# wants nothing from at all. Keys go through the same alias resolution as the
# include-lists, so a board spelling still hits a ban declared under the
# canonical name. Empty list → nobody is banned.
# ---------------------------------------------------------------------------

_COMPANY_NEVER_FETCH = {_canonical_company_key(name) for name in COMPANY_NEVER_FETCH if name}


def company_never_fetch_reason(org: str) -> str | None:
    """Kill reason when the profile bans this company outright, else None.

    The reason names the rule so a later review can trace the drop:
    ``"company_never_fetch — <Company> is on the profile never-fetch list"``.
    """
    if _canonical_company_key(org) in _COMPANY_NEVER_FETCH:
        return f"company_never_fetch — {org} is on the profile never-fetch list"
    return None


def fetch_time_drop_reason(org: str, title: str) -> str | None:
    """Kill reason when the user's profile says this role must never be STORED.

    The union of the two profile rules that are decidable from org + title
    alone: the whole-company ban and the per-company title include-list. The
    fetch path calls this to drop a role BEFORE the save, so the pipeline never
    pays to store, enrich, score or report it. The filter stage keeps applying
    ``company_title_filter_reason`` as the safety net for rows stored before a
    profile change; for a freshly fetched role it now finds nothing left to do.
    """
    return company_never_fetch_reason(org) or company_title_filter_reason(org, title)


def description_words_blacklisted(desc: str) -> bool:
    """True when the description contains a narrow kill phrase.

    Description-level kills are deliberately narrow (visa/citizenship only): a
    full title blacklist applied to a JD body causes false positives.
    """
    if not desc:
        return False
    d = desc.lower()
    return any(kw in d for kw in DESCRIPTION_BLACKLIST_PHRASES)


# ---------------------------------------------------------------------------
# Content-junk detection (non-vacancy pages)
# ---------------------------------------------------------------------------


def is_content_junk(desc: str) -> str | None:
    """Detect non-vacancy content. Returns a reason string or None."""
    if not desc:
        return None
    d = desc[:500].lower()
    if "recaptcha" in d and len(desc) < 300:
        return "recaptcha_only"
    if any(p in d for p in ["every.org", "donate to a fund", "make a donation"]):
        return "donation_widget"
    if any(
        p in d
        for p in [
            "404 not found",
            "page not found",
            "error 404",
            "access denied",
            "cannot be displayed",
        ]
    ):
        return "error_page"
    if len(desc.strip()) < 50:
        return "navigation_snippet"
    return None


def has_enough_content(job: dict, min_chars: int = 50) -> bool:
    """True when the job carries enough text (desc or snippet) or at least a URL."""
    desc = job.get("full_description", "") or ""
    snip = job.get("snippet", "") or ""
    if len(desc.strip()) >= min_chars or len(snip.strip()) >= min_chars:
        return True
    if job.get("url", "").strip():
        return True
    return False


# ---------------------------------------------------------------------------
# Quality-axis classifier
# ---------------------------------------------------------------------------


def classify_vacancy(vacancy: dict) -> str:
    """Classify a vacancy on the quality axis. Returns one reason string.

    Reasons: not_a_job | no_description | wrong_role | wrong_location | ready.
    Composes the pure content checks above. Has no concept of time or
    neighbouring rows (those gates live in the caller). Tolerates a minimal
    vacancy like {"title": "X"} with no locations/full_description/status.

    - wrong_role     — title is blacklisted (judged on the title alone).
    - not_a_job      — description is content junk (recaptcha/donation/error).
    - no_description — no usable description text at all.
    - wrong_location — (reserved) geo exclusion is decided by the caller.
    - ready          — passes every content check.

    Description-level blacklist phrases are deliberately NOT consulted here —
    that kill lives only on the score step (score_vacancies calls
    filters.description_words_blacklisted explicitly).
    """
    title = vacancy.get("title", "") or ""
    desc = vacancy.get("full_description", "") or ""

    if title_words_blacklisted(title):
        return "wrong_role"
    if is_content_junk(desc):
        return "not_a_job"
    if not desc.strip():
        return "no_description"
    return "ready"


# ---------------------------------------------------------------------------
# Time / neighbour gate predicate (pure; the SET is chosen by the caller)
# ---------------------------------------------------------------------------


def is_recently_archived(archived_hashes: set[str], dedup_hash: str) -> bool:
    """True when dedup_hash is in the supplied archived-hash set.

    The include_gone semantics are carried by the CALLER's choice of set
    (direct ATS excludes 'gone_from_source'; boards use the full set).
    """
    return dedup_hash in archived_hashes


# ---------------------------------------------------------------------------
# Cross-board fuzzy duplicate matcher (ported from filter_vacancies)
# ---------------------------------------------------------------------------

_FUZZY_THRESHOLD: float = 0.85
_PROTECTED_STATUSES: frozenset[str] = frozenset(
    {
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
        "archived",
    }
)


def _strip_punct(title: str) -> str:
    """Strip all punctuation, lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()


def _pick_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """Pick winner/loser for a duplicate pair.

    Priority: protected status → llm_score → description length → first_seen.
    """
    a_protected = a.get("status", "unseen") in _PROTECTED_STATUSES
    b_protected = b.get("status", "unseen") in _PROTECTED_STATUSES

    if a_protected and not b_protected:
        return a, b
    if b_protected and not a_protected:
        return b, a
    if a_protected and b_protected:
        return a, b  # both protected — caller handles as manual_review

    # Both unseen — compare quality
    a_score = a.get("llm_score") or 0
    b_score = b.get("llm_score") or 0
    if a_score != b_score:
        return (a, b) if a_score > b_score else (b, a)

    a_desc = len(a.get("full_description") or "")
    b_desc = len(b.get("full_description") or "")
    if a_desc != b_desc:
        return (a, b) if a_desc > b_desc else (b, a)

    a_seen = a.get("first_seen", "9999")
    b_seen = b.get("first_seen", "9999")
    return (a, b) if a_seen <= b_seen else (b, a)


def find_duplicates(vacancies: dict) -> list[dict]:
    """Find fuzzy title duplicates within company groups (cross-board aware).

    Groups vacancies by resolved canonical company name, then compares
    normalized titles with SequenceMatcher. Returns list of match dicts.
    """
    # Group by canonical company name (cross-board dedup via alias resolution)
    by_company: dict[str, list[tuple[str, dict]]] = {}
    for vid, vac in vacancies.items():
        # Skip protected vacancies
        if vac.get("status", "unseen") in _PROTECTED_STATUSES:
            continue
        org = vac.get("org", "")
        canonical = resolve_canonical_name(org) if org else org
        by_company.setdefault(canonical, []).append((vid, vac))

    pairs = []
    seen_pairs: set[tuple[str, str]] = set()

    for canonical_org, vac_list in by_company.items():
        if len(vac_list) < 2:
            continue

        # Pre-compute normalized titles
        normed = [(_strip_punct(v.get("title") or ""), vid, v) for vid, v in vac_list]

        matcher = SequenceMatcher(None, autojunk=False)
        for i in range(len(normed)):
            n_i, id_i, v_i = normed[i]
            if not n_i:
                continue
            matcher.set_seq1(n_i)
            for j in range(i + 1, len(normed)):
                n_j, id_j, v_j = normed[j]
                if not n_j:
                    continue

                pair_key = tuple(sorted([id_i, id_j]))
                if pair_key in seen_pairs:
                    continue

                # Exact normalized match
                if n_i == n_j:
                    seen_pairs.add(pair_key)
                    winner, loser = _pick_winner({**v_i, "id": id_i}, {**v_j, "id": id_j})
                    pairs.append(
                        {
                            "id_a": id_i,
                            "id_b": id_j,
                            "org": canonical_org,
                            "title_a": v_i.get("title", ""),
                            "title_b": v_j.get("title", ""),
                            "norm_title": n_i,
                            "similarity": 1.0,
                            "match_type": "normalized_exact",
                            "source_a": v_i.get("source", ""),
                            "source_b": v_j.get("source", ""),
                            "desc_len_a": len(v_i.get("full_description") or ""),
                            "desc_len_b": len(v_j.get("full_description") or ""),
                        }
                    )
                    continue

                # Fuzzy match
                matcher.set_seq2(n_j)
                ratio = matcher.ratio()
                if ratio >= _FUZZY_THRESHOLD:
                    seen_pairs.add(pair_key)
                    pairs.append(
                        {
                            "id_a": id_i,
                            "id_b": id_j,
                            "org": canonical_org,
                            "title_a": v_i.get("title", ""),
                            "title_b": v_j.get("title", ""),
                            "norm_title": f"{n_i} / {n_j}",
                            "similarity": round(ratio, 4),
                            "match_type": "fuzzy",
                            "source_a": v_i.get("source", ""),
                            "source_b": v_j.get("source", ""),
                            "desc_len_a": len(v_i.get("full_description") or ""),
                            "desc_len_b": len(v_j.get("full_description") or ""),
                        }
                    )

    return pairs
