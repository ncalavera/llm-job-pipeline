"""Fair fetch rotation: engaged/overdue tracked orgs must not starve.

The old rotation sorted the due set by ``(last_fetched_epoch, name)`` with
never-fetched pinned to ``0.0`` — so every never-fetched candidate sorted AHEAD
of any previously-fetched org. As new candidate companies kept arriving, an
overdue tracked org with live liked/decided roles was permanently crowded out
below the per-run volume cap and never refreshed.

``_order_due_companies`` fixes the ordering with guaranteed cohorts:
overdue+engaged, then overdue tracked, then never-fetched — most-overdue first
inside each. These tests pin the acceptance behaviour on invented records
(pure function, no DB, no network).
"""

from __future__ import annotations

import fetch_vacancies as fv

DAY = 86400.0
NOW = 1_000_000_000.0  # arbitrary fixed epoch


def _rec(name, *, age_days=None, ttl_days=7, engaged=False):
    """Build a priority record. ``age_days=None`` means never fetched."""
    last = None if age_days is None else NOW - age_days * DAY
    return {"name": name, "last_fetched": last, "ttl_days": ttl_days, "engaged": engaged}


def test_engaged_overdue_beats_never_fetched_when_cap_tight():
    """The IRC case: an engaged, overdue tracked org outranks fresh newcomers.

    Cap of 1 would, under the old rule, go to a never-fetched candidate; now the
    engaged overdue org wins the single slot."""
    records = [
        _rec("NewCandidateA"),  # never fetched
        _rec("NewCandidateB"),  # never fetched
        _rec("IRC", age_days=18, ttl_days=7, engaged=True),  # overdue + engaged
    ]
    order = fv._order_due_companies(records, NOW)
    assert order[0] == "IRC"

    kept, deferred = fv._apply_company_cap(order, {n: {} for n in order}, cap=1)
    assert set(kept) == {"IRC"}
    assert deferred == 2


def test_cohort_precedence_engaged_then_tracked_then_never():
    """Full cohort order: overdue+engaged, then overdue tracked, then newcomers."""
    records = [
        _rec("Never1"),
        _rec("TrackedOverdue", age_days=10, ttl_days=7),
        _rec("EngagedOverdue", age_days=8, ttl_days=7, engaged=True),
        _rec("Never2"),
    ]
    order = fv._order_due_companies(records, NOW)
    assert order == ["EngagedOverdue", "TrackedOverdue", "Never1", "Never2"]


def test_most_overdue_wins_inside_cohort_by_ratio():
    """Within a cohort the overdue RATIO (age/ttl) ranks, not raw age.

    An S-tier (ttl=3) fetched 4 days ago (ratio 1.33) is more overdue than an
    A-tier (ttl=5) fetched 4 days ago (ratio 0.8), despite equal age."""
    records = [
        _rec("A_tier", age_days=4, ttl_days=5),  # ratio 0.8
        _rec("S_tier", age_days=4, ttl_days=3),  # ratio 1.33
    ]
    order = fv._order_due_companies(records, NOW)
    assert order == ["S_tier", "A_tier"]


def test_never_fetched_get_slots_when_nothing_overdue():
    """With no overdue tracked orgs in the due set, newcomers fill the run.

    (The stale-set builder only puts a tracked org in ``records`` once it is
    past its TTL, so "no overdue tracked" == only never-fetched records here.)"""
    records = [_rec("Beta"), _rec("Alpha"), _rec("Gamma")]
    order = fv._order_due_companies(records, NOW)
    assert order == ["Alpha", "Beta", "Gamma"]  # deterministic by name

    kept, deferred = fv._apply_company_cap(order, {n: {} for n in order}, cap=2)
    assert set(kept) == {"Alpha", "Beta"}
    assert deferred == 1


def test_ordering_is_deterministic_and_total():
    """Same input ⇒ same order; every input name appears exactly once."""
    records = [
        _rec("Zeta", age_days=9, ttl_days=7, engaged=True),
        _rec("Yield", age_days=9, ttl_days=7, engaged=True),  # tie on ratio → name
        _rec("Newbie"),
        _rec("Overdue", age_days=20, ttl_days=7),
    ]
    order1 = fv._order_due_companies(records, NOW)
    order2 = fv._order_due_companies(list(reversed(records)), NOW)
    assert order1 == order2
    assert sorted(order1) == sorted(r["name"] for r in records)
    # Tie between two engaged, equally-overdue orgs breaks by name.
    assert order1[:2] == ["Yield", "Zeta"]


def test_engaged_statuses_exclude_negative_decisions():
    """Engagement = positive interest only; passed/skipped are not engagement."""
    assert set(fv.ENGAGED_STATUSES) == {
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "applied",
    }
    assert "passed" not in fv.ENGAGED_STATUSES
    assert "skipped" not in fv.ENGAGED_STATUSES


def test_enrichment_tiers_use_lightweight_scores_and_recover_connection(monkeypatch):
    import database_supabase as db
    from unittest.mock import MagicMock

    conn = MagicMock()
    cur = conn.cursor.return_value
    scores = [
        ("Top", 65, "active"),
        ("Good", 50, "candidate"),
        ("Middle", 35, "active"),
        ("Low", 0, "active"),
        ("Unscored", None, "active"),
    ]

    def rows():
        if "mission_fit" in cur.execute.call_args.args[0]:
            return [
                {
                    "canonical_name": name,
                    "alignment_score": score,
                    "mission_fit": {"alignment_score": score},
                    "about": {},
                    "enriched_at": None,
                }
                for name, score, _status in scores
            ]
        return scores

    cur.fetchall.side_effect = rows
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    assert fv._load_enrichment_tiers() == {"Top": "S", "Good": "A", "Middle": "B", "Low": "C"}
    sql = cur.execute.call_args.args[0]
    assert "about" not in sql and "mission_fit" not in sql

    close = MagicMock()
    monkeypatch.setattr(db, "close_conn", close)
    cur.execute.side_effect = OSError("connection lost")
    assert fv._load_enrichment_tiers() == {}
    close.assert_called_once()
