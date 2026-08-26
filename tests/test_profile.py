"""Tests for the user profile: how it is found, what happens when it is missing
or half-edited, and what it drives (board recommendations, LinkedIn queries,
scoring factors).

Absorbed from tests/test_profile_targeting.py, tests/test_profile_fallback_warning.py,
tests/test_profile_worktree_bake_guard.py, tests/test_factors.py. Covers:
board recommendation and LinkedIn-query derivation from the profile (plus the
scaffolding-stripping guards against a half-edited example), the loud warning
when the loader falls back to the bundled EXAMPLE profile, the linked-worktree
resolution fallback and the Postgres prod-bake guard it feeds, and the factor
model's filter/penalty/note strengths with the guarantee that a NOTE never
reaches the scorer.

NOTE: the worktree/prod-bake-guard tests below carry an individual
``@pytest.mark.skipif(not _has_git(), ...)``. It is applied per-test, not as a
module-level ``pytestmark``, so a machine without git does not also skip the
profile-targeting, fallback-warning, or factor tests in this file.
"""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import factors  # noqa: E402
import profile_targeting as pt  # noqa: E402
import prompts  # noqa: E402
import settings  # noqa: E402


# --- from test_profile_targeting.py ---
#
# Tests for profile-derived targeting (scripts/profile_targeting.py).
#
# Covers the two things the fix must guarantee:
#
#   1. Board recommendations come from the PROFILE, not a maintainer default — a
#      synthetic engineer profile proposes engineering boards and NOT the impact
#      boards; a general board (LinkedIn) is always proposed.
#   2. LinkedIn queries resolve profile-first: an explicit ## LINKEDIN_QUERIES
#      wins; otherwise they DERIVE from ## TARGET_ROLES (+ geography); with neither
#      the set is empty (the shipped config carries no queries).
#
# Everything is person/sector-agnostic and offline: synthetic profiles are passed
# in as parsed-section dicts (no file, no DB), plus one slice against the real
# shipped boards to prove the engineer-vs-impact separation end to end.
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


# The shipped example's TARGET_ROLES GUIDANCE PROSE (not just its ``e.g.``
# samples) is scaffolding. Split on its commas/semicolons the
# sentence shatters into stray one-word "roles" (``field``, ``careers``) that, in
# a half-copied profile, became live LinkedIn search queries.
_EXAMPLE_TARGET_ROLES_PROSE = (
    "The exact job titles you want to see — one per line or comma-separated. Any\n"
    "field; pick your own. The lines below are only format examples from different\n"
    "careers, not a default set — replace them:"
)


def test_derive_role_keywords_drops_example_guidance_prose():
    """The intro paragraph alone must yield NO keywords — not "field"/"careers"."""
    roles = pt._derive_role_keywords(_EXAMPLE_TARGET_ROLES_PROSE)
    assert roles == []
    assert "field" not in roles and "careers" not in roles


def test_derive_role_keywords_full_unedited_target_roles_yields_nothing():
    """A verbatim, unedited TARGET_ROLES section (intro prose + bracket line +
    the three ``e.g.`` samples) derives no queries at all."""
    body = _EXAMPLE_TARGET_ROLES_PROSE + "\n\n" + _UNEDITED_ROLE_SCAFFOLD
    assert pt._derive_role_keywords(body) == []


def test_real_role_survives_alongside_example_guidance_prose():
    """The prose is stripped but a real role typed under it comes through — the
    guard keys off the example's own text, never off a topic word."""
    body = _EXAMPLE_TARGET_ROLES_PROSE + "\n- Platform Engineer, Staff Engineer"
    assert pt._derive_role_keywords(body) == ["Platform Engineer", "Staff Engineer"]


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


# --- from test_profile_fallback_warning.py ---
#
# Profile-safety: falling back to the EXAMPLE profile must warn loudly.
#
# Without config/user_profile.md the loader silently used the bundled EXAMPLE
# (a fictional person), so a user could score an entire run against fiction and
# never know. The loader must print a prominent stderr warning on that fallback —
# without hard-crashing, and without warning when a real profile is used.
def test_example_fallback_warns(monkeypatch, tmp_path, capsys):
    """No env override + no real profile + example present → stderr warning."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    # Neutralize the linked-worktree fallback: this test simulates "no real
    # profile anywhere", so the main-checkout source must also come up empty
    # (the test process itself runs inside a worktree with a real main profile).
    monkeypatch.setattr(prompts, "_worktree_main_profile", lambda: None)
    missing = tmp_path / "user_profile.md"  # does NOT exist
    example = tmp_path / "user_profile.example.md"
    example.write_text("## USER_PROFILE\n\nExample person.\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", missing)
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", example)

    sections = prompts._load_user_profile()  # must NOT raise
    assert "USER_PROFILE" in sections

    err = capsys.readouterr().err
    assert "EXAMPLE profile" in err
    assert "MEANINGLESS" in err


def test_real_profile_does_not_warn(monkeypatch, tmp_path, capsys):
    """A real config/user_profile.md → no warning."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    real = tmp_path / "user_profile.md"
    real.write_text("## USER_PROFILE\n\nReal person.\n", encoding="utf-8")
    example = tmp_path / "user_profile.example.md"
    example.write_text("## USER_PROFILE\n\nExample.\n", encoding="utf-8")
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", real)
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", example)

    prompts._load_user_profile()
    err = capsys.readouterr().err
    assert "EXAMPLE profile" not in err


def test_explicit_env_path_does_not_warn(monkeypatch, tmp_path, capsys):
    """An explicit USER_PROFILE_PATH (how tests pass profiles) never warns."""
    profile = tmp_path / "p.md"
    profile.write_text("## USER_PROFILE\n\nExplicit.\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))

    prompts._load_user_profile()
    err = capsys.readouterr().err
    assert "EXAMPLE profile" not in err


def test_no_profile_at_all_raises(monkeypatch, tmp_path):
    """Neither real nor example present → explicit FileNotFoundError, not a
    silent empty profile."""
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    # No profile ANYWHERE — including the main checkout (the worktree fallback).
    monkeypatch.setattr(prompts, "_worktree_main_profile", lambda: None)
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", tmp_path / "nope.md")
    monkeypatch.setattr(prompts, "EXAMPLE_PROFILE_PATH", tmp_path / "nope.example.md")
    with pytest.raises(FileNotFoundError):
        prompts._load_user_profile()


def test_cache_invalidates_on_edit(monkeypatch, tmp_path):
    """Editing the profile (new mtime) must be picked up on the next load, not
    served stale from the parse cache (matters for long-lived processes)."""
    import os

    prompts.clear_profile_cache()
    profile = tmp_path / "p.md"
    profile.write_text("## TARGET_ROLES\n\nEngineer\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))

    first = prompts._load_user_profile()
    assert first["TARGET_ROLES"] == "Engineer"

    # Edit in place and force a newer mtime (fs resolution can be coarse).
    profile.write_text("## TARGET_ROLES\n\nDesigner\n", encoding="utf-8")
    st = profile.stat()
    os.utime(profile, (st.st_atime + 10, st.st_mtime + 10))

    second = prompts._load_user_profile()
    assert second["TARGET_ROLES"] == "Designer", "edit must invalidate the cache"


def test_clear_profile_cache(monkeypatch, tmp_path):
    """clear_profile_cache() empties the cache so the next load re-reads."""
    profile = tmp_path / "p.md"
    profile.write_text("## USER_PROFILE\n\nX\n", encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    prompts._load_user_profile()
    assert prompts._profile_cache  # populated
    prompts.clear_profile_cache()
    assert prompts._profile_cache == {}


# --- from test_profile_worktree_bake_guard.py ---
#
# The linked-worktree profile trap — resolution fallback + prod bake guard.
#
# ``config/user_profile.md`` is gitignored (personal data), so a ``git worktree``
# never carries it. A pipeline run launched from a worktree would fall through to
# the bundled EXAMPLE profile and bake DEFAULT settings (language, thresholds…)
# into the SINGLE shared ``dashboard_snapshot`` row in prod Supabase — silently
# overwriting the user profile's real values. Two layers close it:
#
#   1. resolution recovers the REAL profile from the MAIN checkout's ``config/``
#      when a linked worktree lacks its own copy (``prompts``);
#   2. a fail-safe: on the Postgres/Supabase backend, if NO real profile resolves,
#      ``generate_dashboard`` REFUSES to write rather than bake the example into
#      shared state. SQLite/local stays permissive (fresh installs, tests).
#
# Offline, invented data, real throwaway git repos under tmp_path — never the
# maintainer's files or a live DB.
def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    prompts.clear_profile_cache()
    yield
    prompts.clear_profile_cache()


# ---------------------------------------------------------------------------
# 1. Worktree-fallback resolution — the real profile is recovered from main
# ---------------------------------------------------------------------------


def _make_main_with_worktree(tmp_path: Path, profile_body: str) -> tuple[Path, Path]:
    """Build a main checkout whose gitignored profile lives only in its working
    tree, plus a linked worktree that (correctly) lacks that file. Returns
    ``(main, worktree)``."""
    main = tmp_path / "main"
    (main / "config").mkdir(parents=True)
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "Test")
    # user_profile.md is gitignored — so it never enters the worktree checkout.
    (main / ".gitignore").write_text("config/user_profile.md\n", encoding="utf-8")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    (main / "config" / "user_profile.md").write_text(profile_body, encoding="utf-8")

    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))
    return main, worktree


def _point_prompts_at(monkeypatch, root: Path) -> None:
    monkeypatch.delenv("USER_PROFILE_PATH", raising=False)
    monkeypatch.setattr(prompts, "_REPO_ROOT", root)
    monkeypatch.setattr(prompts, "DEFAULT_PROFILE_PATH", root / "config" / "user_profile.md")
    monkeypatch.setattr(
        prompts, "EXAMPLE_PROFILE_PATH", root / "config" / "user_profile.example.md"
    )
    prompts.clear_profile_cache()


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_worktree_without_profile_reads_main_checkout(tmp_path, monkeypatch, capsys):
    body = "## USER_PROFILE\n\nReal person.\n\n## OUTPUT_LANGUAGE\n\nRussian\n"
    main, worktree = _make_main_with_worktree(tmp_path, body)
    assert not (worktree / "config" / "user_profile.md").exists()

    _point_prompts_at(monkeypatch, worktree)

    path, warn_example, from_worktree = prompts._resolve_profile_path()
    assert path.resolve() == (main / "config" / "user_profile.md").resolve()
    assert warn_example is False
    assert from_worktree is True

    # The real profile's content is what gets parsed — not the example.
    sections = prompts._load_user_profile()
    assert sections.get("OUTPUT_LANGUAGE") == "Russian"
    assert prompts.has_real_profile() is True

    # One clear line explains the recovery, once.
    err = capsys.readouterr().err
    assert "git worktree" in err
    assert "main checkout" in err


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_main_checkout_is_not_treated_as_a_worktree(tmp_path, monkeypatch):
    """A plain checkout (git-dir == common-dir) must NOT trigger the fallback."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    _git(repo, "init", "-q")
    monkeypatch.setattr(prompts, "_REPO_ROOT", repo)
    prompts.clear_profile_cache()
    assert prompts._worktree_main_profile() is None


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_worktree_fallback_absent_when_main_has_no_profile(tmp_path, monkeypatch):
    """Worktree + a main checkout that itself lacks the profile → no recovery,
    so resolution degrades to the EXAMPLE (has_real_profile False)."""
    main = tmp_path / "main"
    (main / "config").mkdir(parents=True)
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "Test")
    (main / ".gitignore").write_text("config/user_profile.md\n", encoding="utf-8")
    _git(main, "add", ".gitignore")
    _git(main, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))

    (worktree / "config").mkdir(parents=True, exist_ok=True)
    (worktree / "config" / "user_profile.example.md").write_text(
        "## USER_PROFILE\n\nExample.\n", encoding="utf-8"
    )
    _point_prompts_at(monkeypatch, worktree)

    assert prompts._worktree_main_profile() is None
    path, warn_example, from_worktree = prompts._resolve_profile_path()
    assert warn_example is True  # degraded to the bundled example
    assert from_worktree is False
    assert prompts.has_real_profile() is False


# ---------------------------------------------------------------------------
# 2 & 3. The prod bake guard — Postgres refuses, SQLite stays permissive
# ---------------------------------------------------------------------------


def _load_report(monkeypatch):
    """Reload report (and its db_backend dependency) with a clean SQLite chain."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    for mod in ("database_supabase", "config", "db_conn", "db_backend", "report"):
        sys.modules.pop(mod, None)
    import db_backend

    importlib.reload(db_backend)
    import report

    return report, db_backend


def _current_prompts():
    """The ``prompts`` module the guard's ``import prompts`` will resolve.

    Bind it from ``sys.modules`` at call time: other tests reload the module via
    ``sys.modules`` surgery, so the file-level ``prompts`` import can go stale and
    monkeypatching it would miss the object the guard actually sees.
    """
    import prompts as _p

    return _p


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_postgres_refuses_to_bake_example_profile(monkeypatch):
    """Postgres backend + no real profile → guard raises, naming the file/trap."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    with pytest.raises(RuntimeError) as exc:
        report._guard_shared_snapshot_profile()
    msg = str(exc.value)
    assert "user_profile.md" in msg
    assert "worktree" in msg.lower()


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_generate_dashboard_refuses_before_any_db_read(monkeypatch):
    """The guard fires at the TOP of generate_dashboard — nothing is read or
    written when the profile is missing on Postgres."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("generate_dashboard read data before the profile guard")

    monkeypatch.setattr(report, "prepare_report_data", _boom)
    with pytest.raises(RuntimeError):
        report.generate_dashboard()


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_postgres_allows_write_with_real_profile(monkeypatch):
    """A real profile (default file, USER_PROFILE_PATH, or worktree fallback) →
    the guard is a no-op even on Postgres."""
    report, db_backend = _load_report(monkeypatch)
    monkeypatch.setattr(db_backend, "IS_SQLITE", False)
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: True)
    report._guard_shared_snapshot_profile()  # must not raise


@pytest.mark.skipif(not _has_git(), reason="git is required for worktree tests")
def test_sqlite_stays_permissive_with_missing_profile(monkeypatch, tmp_path):
    """SQLite/local keeps the permissive fallback: a missing profile never blocks
    the local data.js bake (fresh installs, tests)."""
    report, db_backend = _load_report(monkeypatch)
    assert db_backend.IS_SQLITE, "this test must run on the SQLite backend"
    monkeypatch.setattr(_current_prompts(), "has_real_profile", lambda: False)

    report._guard_shared_snapshot_profile()  # no raise on SQLite

    out_dir = tmp_path / "public_out"
    out_dir.mkdir()
    monkeypatch.setattr(report, "PUBLIC_DIR", out_dir, raising=False)
    report._persist_dashboard({"groups": [{"id": "x"}]})
    assert [p.name for p in out_dir.iterdir()] == ["data.js"]


# --- from test_factors.py ---
#
# Tests for the factor model (scripts/factors.py).
#
# Locks the ticket's core invariant: every factor is declared with a strength
# (filter / penalty / note), and a NOTE never reaches the scorer — otherwise a
# display-only reminder would act as an implicit penalty.
_SECTIONS = {
    "HARD_FILTERS": "exclude_title_keywords: engineer, nurse\nban_regions: (none)\n",
    "EXCLUDE_PATTERNS": "- Gambling and tobacco.\n- Weekend on-call rotations.",
    "NOTES": "- Flag frequent travel.\n- SENTINEL_NOTE_XYZ prefer a written culture.",
}


def test_three_strengths_parse_from_their_sections():
    fs = factors.load_factors(_SECTIONS)
    filt = [f.text for f in factors.by_strength(fs, factors.FILTER)]
    pen = [f.text for f in factors.by_strength(fs, factors.PENALTY)]
    note = [f.text for f in factors.by_strength(fs, factors.NOTE)]
    assert filt == ["engineer", "nurse"]
    assert pen == ["Gambling and tobacco.", "Weekend on-call rotations."]
    assert note == ["Flag frequent travel.", "SENTINEL_NOTE_XYZ prefer a written culture."]


def test_summary_counts_per_strength():
    assert factors.summary(_SECTIONS) == {"filter": 2, "penalty": 2, "note": 2}


def test_notes_are_not_scorer_visible():
    fs = factors.load_factors(_SECTIONS)
    for f in fs:
        assert f.scorer_visible == (f.strength != factors.NOTE)
        assert f.is_note == (f.strength == factors.NOTE)


#: The one profile section that must never reach a scorer template — see
#: factors.py's module docstring. Hardcoded here rather than read off a
#: runtime constant: the guarantee IS this test, checked against every real
#: template file so a newly added one is covered automatically.
_SCORER_HIDDEN_SECTION = "NOTES"


def test_scoring_prompt_never_carries_a_note():
    """The load-bearing guarantee: rendering EVERY real scorer template (not a
    hardcoded pair — the whole prompts dir, so a future template is covered
    too) with a NOTES section present must NOT surface any note text, because
    none of them reference a {{NOTES}} placeholder. That is what keeps a note
    from ever becoming an implicit penalty."""
    from prompts import _PROMPTS_DIR, _render

    templates = sorted(_PROMPTS_DIR.glob("*.md"))
    assert templates, "expected at least one scorer template in scripts/prompts/"
    for tpl_path in templates:
        template = tpl_path.read_text(encoding="utf-8").strip()
        placeholder = "{{" + _SCORER_HIDDEN_SECTION + "}}"
        assert placeholder not in template.replace(" ", ""), tpl_path.name
        rendered = _render(template, _SECTIONS)
        assert "SENTINEL_NOTE_XYZ" not in rendered, tpl_path.name


def test_empty_profile_declares_nothing():
    assert factors.load_factors({}) == []


def test_bundled_example_profile_declares_notes():
    """The shipped example must declare the new NOTE strength so onboarding sees
    all three strengths (neutral, invented factors only)."""
    from prompts import EXAMPLE_PROFILE_PATH, _render  # noqa: F401

    text = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
    assert "## NOTES" in text
