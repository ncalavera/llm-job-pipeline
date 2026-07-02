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

import re
from difflib import SequenceMatcher

from config import (
    GLOBAL_BLACKLIST,
    GLOBAL_BLACKLIST_SUBSTR,
    GLOBAL_BLACKLIST_DESC_SUBSTR,
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

# Pre-compiled blacklist: single alternation regex, sorted by length desc to
# prevent shorter substrings matching prematurely. Compiled once at module load
# (~10-50x faster than iterating individual re.search() calls per vacancy).
# A trailing (?:es|s)? lets a singular keyword also catch its plural, so
# "developer" matches "developers", "fellow" → "fellows", "internship" →
# "internships" without listing every plural by hand.
_TITLE_BLACKLIST_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(kw) for kw in sorted(TITLE_BLACKLIST_WORDS, key=len, reverse=True))
    + r")(?:es|s)?\b",
    re.IGNORECASE,
)


def title_words_blacklisted(title: str) -> bool:
    """True when the title hits a substring stem or a whole-word blacklist term."""
    t = title.lower()
    if any(kw in t for kw in TITLE_BLACKLIST_STEMS):
        return True
    if _TITLE_BLACKLIST_PATTERN.search(t):
        return True
    return False


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
