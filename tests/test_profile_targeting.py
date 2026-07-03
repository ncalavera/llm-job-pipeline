"""Tests for profile-derived targeting (scripts/profile_targeting.py).

Covers the two things the fix must guarantee:

  1. Board recommendations come from the PROFILE, not a maintainer default — a
     synthetic engineer profile proposes engineering boards and NOT the impact
     boards; a general board (LinkedIn) is always proposed.
  2. LinkedIn queries resolve profile-first: an explicit ## LINKEDIN_QUERIES
     wins; otherwise they DERIVE from ## TARGET_ROLES (+ geography); with neither
     the set is empty (the shipped config carries no queries).

Everything is person/sector-agnostic and offline: synthetic profiles are passed
in as parsed-section dicts (no file, no DB), plus one slice against the real
shipped boards to prove the engineer-vs-impact separation end to end.
"""

import sys
from pathlib import Path

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import profile_targeting as pt  # noqa: E402
import settings  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic boards — a tiny neutral fixture so matching is deterministic.
# ---------------------------------------------------------------------------

# Every synthetic board carries a REAL registered strategy: recommend_boards
# skips catalogue entries it cannot actually fetch (review finding on #44).
SYNTH_BOARDS = {
    "linkedin": {"name": "LinkedIn", "general": True, "strategy": "linkedin_guest"},
    "tech_remote": {
        "name": "Tech Remote",
        "strategy": "remotive_api",
        "audience_tags": ["engineering", "software", "remote"],
    },
    "impact": {
        "name": "Impact Board",
        "strategy": "reliefweb_api",
        "audience_tags": ["nonprofit", "humanitarian", "policy"],
    },
    "clinical": {
        "name": "Clinical Board",
        "strategy": "wwr_rss",
        "audience_tags": ["nurse", "clinical", "healthcare"],
    },
}


# ---------------------------------------------------------------------------
# recommend_boards
# ---------------------------------------------------------------------------


def test_engineer_gets_tech_board_not_impact():
    sections = {
        "USER_PROFILE": "Backend software engineer, distributed systems, remote-first.",
        "TARGET_ROLES": "- Senior Software Engineer, Platform Engineer.\n**Not a target:** nonprofit policy.",
    }
    recs = pt.recommend_boards(sections, SYNTH_BOARDS)
    ids = [r["id"] for r in recs]
    assert "tech_remote" in ids
    assert "impact" not in ids  # the negative "nonprofit policy" must NOT pull it in
    assert "clinical" not in ids
    assert ids[0] == "linkedin"  # general board is always first


def test_general_board_always_recommended_even_with_no_matches():
    sections = {"USER_PROFILE": "Something niche", "TARGET_ROLES": "- Zookeeper"}
    recs = pt.recommend_boards(sections, SYNTH_BOARDS)
    ids = [r["id"] for r in recs]
    assert ids == ["linkedin"]  # only the general board; no tag matched
    assert "browse" in recs[0]["reason"].lower()  # nudge to the catalogue


def test_matches_sorted_by_overlap_strength():
    sections = {
        "USER_PROFILE": "nurse, clinical, healthcare",
        "TARGET_ROLES": "- ICU Nurse",
    }
    recs = pt.recommend_boards(sections, SYNTH_BOARDS)
    ids = [r["id"] for r in recs]
    assert ids[0] == "linkedin"
    assert "clinical" in ids
    assert "tech_remote" not in ids and "impact" not in ids


def test_missing_profile_sections_do_not_crash():
    assert pt.recommend_boards({}, SYNTH_BOARDS)[0]["id"] == "linkedin"


def test_short_tag_does_not_substring_false_match():
    """A short tag must match a whole word, not a substring — 'un' (United
    Nations) must NOT be pulled in by 'background', nor 'ai' by 'email'."""
    boards = {
        "linkedin": {"name": "LinkedIn", "general": True, "strategy": "linkedin_guest"},
        "un_board": {"name": "UN Board", "strategy": "reliefweb_api", "audience_tags": ["un"]},
        "ai_board": {"name": "AI Board", "strategy": "remotive_api", "audience_tags": ["ai"]},
    }
    sections = {
        "USER_PROFILE": "Nurse with a clinical background; handles email.",
        "TARGET_ROLES": "- ICU Nurse",
    }
    ids = {r["id"] for r in pt.recommend_boards(sections, boards)}
    assert ids == {"linkedin"}  # neither un_board nor ai_board false-matched


def test_real_shipped_boards_keep_engineer_off_impact():
    """End-to-end against the real config: an engineer profile must not surface
    the humanitarian / nonprofit-only boards."""
    sections = {
        "USER_PROFILE": "Senior backend engineer, Go and Rust, remote.",
        "TARGET_ROLES": "- Software Engineer, Site Reliability Engineer, DevOps Engineer.",
    }
    recs = pt.recommend_boards(sections, settings.boards())
    ids = {r["id"] for r in recs}
    assert "linkedin" in ids
    # Humanitarian / nonprofit-only boards must NOT match a pure engineer profile.
    for impact_only in ("reliefweb", "impactpool", "idealist"):
        assert impact_only not in ids, f"{impact_only} should not match an engineer"


def test_unfetchable_catalogue_boards_are_never_recommended():
    """Review finding on #44: catalogue entries whose strategy has no registered
    fetcher (the consider_board VC aggregators) must not be proposed — enabling
    one yields silent zero every run. A startup-engineer profile is exactly the
    bait for a16z/sequoia."""
    from fetchers import BOARD_FETCHERS

    sections = {
        "USER_PROFILE": "Startup engineer, venture-backed product companies, tech.",
        "TARGET_ROLES": "- Software Engineer, Product Engineer.",
    }
    real_boards = settings.boards()
    recs = pt.recommend_boards(sections, real_boards)
    for r in recs:
        strategy = str(real_boards[r["id"]].get("strategy", ""))
        assert strategy in BOARD_FETCHERS, f"recommended unfetchable board {r['id']} ({strategy})"
    unfetchable = {
        bid
        for bid, cfg in real_boards.items()
        if str(cfg.get("strategy", "")) not in BOARD_FETCHERS
    }
    assert not unfetchable & {r["id"] for r in recs}


# ---------------------------------------------------------------------------
# resolve_linkedin_queries — resolution order
# ---------------------------------------------------------------------------


def test_explicit_queries_win():
    sections = {
        "LINKEDIN_QUERIES": "Head Chef | Paris\nSous Chef | Remote",
        "TARGET_ROLES": "- Line Cook",  # ignored because explicit wins
    }
    q = pt.resolve_linkedin_queries(sections)
    assert q == [
        {"keywords": "Head Chef", "location": "Paris"},
        {"keywords": "Sous Chef", "location": "Remote"},
    ]


def test_explicit_query_without_location_is_blank():
    q = pt.resolve_linkedin_queries({"LINKEDIN_QUERIES": "Data Analyst"})
    assert q == [{"keywords": "Data Analyst", "location": ""}]


def test_derives_from_target_roles_and_geography():
    sections = {
        "USER_PROFILE": "**Target locations:** Berlin (DE), remote-EU",
        "TARGET_ROLES": "- Product Manager, Growth Manager.\n**Not a target:** intern.",
    }
    q = pt.resolve_linkedin_queries(sections)
    kw = {item["keywords"] for item in q}
    locs = {item["location"] for item in q}
    assert kw == {"Product Manager", "Growth Manager"}
    assert locs == {"Berlin", "Remote"}  # (DE) stripped, remote-EU normalised


def test_derivation_defaults_location_to_remote():
    q = pt.resolve_linkedin_queries({"TARGET_ROLES": "- UX Designer"})
    assert q == [{"keywords": "UX Designer", "location": "Remote"}]


def test_negated_target_locations_line_is_not_a_search_location():
    """Review nit on #44: "Not target locations: US" is an exclusion — deriving
    US as a SEARCH location would search exactly where the user opted out."""
    sections = {
        "TARGET_ROLES": "- UX Designer",
        "GEOGRAPHY": "**Not target locations:** US, Canada",
    }
    q = pt.resolve_linkedin_queries(sections)
    assert q == [{"keywords": "UX Designer", "location": "Remote"}]

    positive = dict(sections, GEOGRAPHY="Target locations: Lisbon\nNot target locations: US")
    locs = {item["location"] for item in pt.resolve_linkedin_queries(positive)}
    assert locs == {"Lisbon"}


def test_derivation_stops_at_not_a_target():
    q = pt.resolve_linkedin_queries(
        {"TARGET_ROLES": "- Nurse\n**Not a target:** Sales Rep, Recruiter"}
    )
    kw = {item["keywords"] for item in q}
    assert "Sales Rep" not in kw and "Recruiter" not in kw
    assert "Nurse" in kw


def test_no_queries_when_no_roles_and_no_explicit():
    assert pt.resolve_linkedin_queries({"USER_PROFILE": "Just a bio, no roles."}) == []


def test_commented_out_explicit_section_falls_through_to_derivation():
    sections = {
        "LINKEDIN_QUERIES": "<!-- Chef | Paris -->",  # all commented → not explicit
        "TARGET_ROLES": "- Baker",
    }
    assert pt.resolve_linkedin_queries(sections) == [{"keywords": "Baker", "location": "Remote"}]


def test_derived_queries_are_capped():
    roles = ", ".join(f"Role{i}" for i in range(20))
    q = pt.resolve_linkedin_queries({"TARGET_ROLES": f"- {roles}"})
    assert len(q) <= pt._MAX_DERIVED_QUERIES


# ---------------------------------------------------------------------------
# Scaffolding stripping — the two directions of the same misfilter
#
# 1. UNEDITED example scaffolding (a placeholder line + the shipped ``e.g. "…"``
#    sample lines) must be dropped so it cannot leak into targeting.
# 2. A user's OWN content that happens to sit on an ``e.g.`` line (a common
#    half-edit: real roles typed in, prefix not deleted) must NOT be dropped —
#    that would silently erase a real role, the inverse of the original bug.
# ---------------------------------------------------------------------------

# The verbatim sample lines the shipped example carries under TARGET_ROLES.
_UNEDITED_ROLE_SCAFFOLD = (
    "- [your target title], [a more senior version], [an adjacent title]\n"
    '- e.g. "Registered Nurse, Charge Nurse, Nurse Manager"\n'
    '- e.g. "Backend Engineer, Senior Software Engineer, Staff Engineer"\n'
    '- e.g. "Operations Manager, Head of Operations, Chief of Staff"'
)


def test_derive_role_keywords_drops_unedited_scaffolding():
    """Placeholder line + the shipped ``e.g.`` samples yield no keywords: none of
    them is the user's own choice, so none may become a LinkedIn search."""
    assert pt._derive_role_keywords(_UNEDITED_ROLE_SCAFFOLD) == []


def test_derive_role_keywords_keeps_only_the_users_real_role():
    """A real role typed above the untouched sample lines survives alone."""
    body = "- Platform Engineer, Staff Engineer\n" + _UNEDITED_ROLE_SCAFFOLD
    assert pt._derive_role_keywords(body) == ["Platform Engineer", "Staff Engineer"]


def test_derive_role_keywords_does_not_vanish_user_role_left_on_eg_line():
    """The inverse misfilter: a user edited their OWN roles into an ``e.g. "…"``
    line and left the prefix. That line is NOT a shipped sample, so it must NOT
    be dropped — the roles come through, and the stray ``e.g.``/quote decoration
    is scrubbed so the keyword is actually searchable."""
    body = '- e.g. "ICU Nurse, Charge Nurse, Public Health Nurse Manager"'
    assert pt._derive_role_keywords(body) == [
        "ICU Nurse",
        "Charge Nurse",
        "Public Health Nurse Manager",
    ]


def test_derive_locations_ignores_unedited_placeholder():
    """An untouched ``**Target locations:** [where you'd work — …]`` placeholder
    is scaffolding, not a location list; derivation falls back to Remote."""
    sections = {
        "USER_PROFILE": "**Target locations:** [where you'd work — cities, "
        'countries, or "remote-EU" style]'
    }
    assert pt._derive_locations(sections) == ["Remote"]


def test_derive_locations_still_reads_a_real_line():
    """A real, edited target-locations line is unaffected by the strip."""
    sections = {"USER_PROFILE": "**Target locations:** Berlin (DE), remote-EU"}
    assert pt._derive_locations(sections) == ["Berlin", "Remote"]


def test_markdown_link_label_is_not_stripped_as_a_placeholder():
    """``[label](url)`` is a user's own Markdown link, not template guidance —
    its words must survive in the board-matching haystack."""
    text = pt._strip_scaffolding("See my [Nonprofit consulting portfolio](https://x.test/p).")
    assert "nonprofit consulting portfolio" in text.lower()


def test_bracket_placeholder_without_link_is_stripped():
    """A bare ``[…]`` placeholder (no trailing link target) is still removed."""
    text = pt._strip_scaffolding("**Want to work in:** [healthcare, public policy].")
    assert "healthcare" not in text and "policy" not in text
