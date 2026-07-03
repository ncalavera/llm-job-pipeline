"""Trial T6 — "overflow".

Reproduces the first user-test failure where the volume of fetched content
overwhelmed the tester with no obvious levers to shrink it. A wide profile with
many boards must still leave the volume levers visible, keep the "Today" cockpit
bounded to the loudest tier, and — once the review backlog builds — surface a
learning screen that proposes cuts (never applies them).

The banner volumes and the overload lever copy are unit-tested in
``test_volume_settings.py``; board recommendation in ``test_profile_targeting.py``.
This trial is the persona integration: a wide impact profile fans out to many
boards, the run banner shows the levers AND the cut advice together under an
overflow backlog, and "Today" stays gated strictly above the catalog floor.
"""

from __future__ import annotations

import os
import re

import trial_harness as h

# The pure-impact boards an engineer never gets (see trial T1) — an ops/impact
# profile SHOULD get them. Same machinery, opposite persona: proof it is
# profile-driven, not a fixed default in either direction.
IMPACT_BOARDS = {"idealist", "impactpool", "reliefweb"}

TODAY_JS = os.path.join(h.REPO_ROOT, "public", "modules", "today.js")


def test_wide_impact_profile_fans_out_to_impact_boards(monkeypatch):
    """A broad ops/impact profile proposes many boards, including the impact ones."""
    h.swap_profile(monkeypatch, "profile_ops_impact.md")

    import profile_targeting as pt
    import prompts

    recs = pt.recommend_boards(prompts._load_user_profile())
    ids = {r["id"] for r in recs}

    assert "linkedin" in ids
    assert IMPACT_BOARDS.issubset(ids), f"impact boards missing for an impact profile: {ids}"
    assert len(recs) >= 6, "a wide profile should fan out to many boards"


def test_run_banner_shows_levers_and_cut_advice_under_overflow(monkeypatch):
    """Under an overflow backlog, the banner shows the volume dials AND the cuts."""
    h.swap_profile(monkeypatch, "profile_engineer.md")

    import run_daily as rd

    # Overflow day: many companies tracked, backlog well past the overload proxy.
    monkeypatch.setattr(rd, "_scalar", lambda *a, **k: 250)
    monkeypatch.setattr(rd, "_scored_unseen", lambda: rd.OVERLOAD_BACKLOG + 70)

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rd._print_run_banner(rd.Opts(job_boards="all"))
    out = buf.getvalue()

    # The volume dials are on screen.
    assert "today's volume" in out
    assert "150" in out and "Digest size: 5" in out
    assert "all defined boards" in out

    # The cut advice fires and names the three real levers — suggestion only.
    assert "Review backlog" in out
    assert "disable-board" in out
    assert "daily_scoring_limit" in out
    assert "HARD_FILTERS" in out
    assert "nothing changes unless you do it" in out


def test_today_cockpit_is_bounded_above_the_catalog_floor(monkeypatch, tmp_path):
    """ "Today" surfaces only the loudest tier, so an overflow day can't dump into it."""
    h.use_persona(
        monkeypatch, profile="profile_engineer.md", db_path=tmp_path / "db", migrate=False
    )

    with open(TODAY_JS, encoding="utf-8") as fh:
        today_src = fh.read()
    m = re.search(r"NEW_HIGH_FIT\s*=\s*(\d+)", today_src)
    assert m, "today.js must define a NEW_HIGH_FIT gate for the cockpit"
    new_high_fit = int(m.group(1))

    import config

    # Today's "new" tier sits above the catalog floor and at/above the protect
    # line — the cockpit is a high-signal subset, never the full flood.
    assert new_high_fit > config.CATALOG_MIN_SCORE
    assert new_high_fit >= config.PROTECT_SCORE
