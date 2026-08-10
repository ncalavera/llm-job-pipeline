"""The strong model's near-zero scores seed filter proposals by themselves.

Hand-flagged garbage never scaled: it needs the user to notice a bad row and
run a command, so the whole ledger holds a handful. Now that every role gets one
strong-model pass (no cheap screen), a near-zero score is a considered judgement
and can do that work automatically — without loosening any guard.
"""

import learning


def test_AG01_threshold_sits_below_the_weakest_wanted_role():
    """The weakest role in his liked basket scores 15. Harvest at or above that
    and the filter starts learning from roles he would have wanted."""
    assert learning.AUTO_GARBAGE_SCORE <= 15


def test_AG02_auto_titles_still_face_the_recurrence_threshold():
    """One junk title must never propose a filter word, however it was found."""
    result = learning.propose_filter_words(
        ["Chief Strategy and Scale Officer"], liked=[], high=[]
    )
    assert result["proposals"] == []


def test_AG03_auto_titles_still_face_the_backtest():
    """A word harvested from junk is still rejected when it collides with
    anything he wanted — the widened net cannot bypass this."""
    junk = ["Programme Officer, Malaria", "Programme Officer, Nutrition"]

    blocked = learning.propose_filter_words(
        junk, liked=["Programme Officer, Strategy"], high=[]
    )
    assert [p["word"] for p in blocked["proposals"]] == []
    assert any(r["word"] == "programme" for r in blocked["rejected"])

    allowed = learning.propose_filter_words(junk, liked=["Director of Operations"], high=[])
    assert "programme" in [p["word"] for p in allowed["proposals"]]


def test_AG04_query_is_scoped_to_the_strong_model(monkeypatch):
    """A cheap screen score is a guess. Guesses must not teach filters."""
    captured = {}

    class _Cur:
        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return []

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(learning, "_conn", lambda: _Conn())
    monkeypatch.setattr(learning, "cursor_ts", lambda: None)

    learning.auto_garbage_titles()

    assert "scored_by = %s" in captured["sql"]
    assert "status = 'unseen'" in captured["sql"]
    assert learning.AUTO_GARBAGE_SCORE in captured["params"]


def test_AG05_a_broken_harvest_never_takes_down_the_review(monkeypatch):
    """The gate that reviews proposals must survive its own extras failing."""

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(learning, "auto_garbage_titles", _boom)
    monkeypatch.setattr(learning, "table_ready", lambda: True)
    monkeypatch.setattr(learning, "undiscussed_garbage", lambda: [])
    monkeypatch.setattr(learning, "decided_since_cursor", lambda: 0)
    monkeypatch.setattr(learning, "liked_titles", lambda: [])
    monkeypatch.setattr(learning, "high_scored_titles", lambda: [])
    monkeypatch.setattr(learning, "personal_filter_words", lambda: [])
    monkeypatch.setattr(learning, "revision_due", lambda: False)
    monkeypatch.setattr(learning, "cursor_ts", lambda: None)
    monkeypatch.setattr(learning, "applied_log", lambda limit=5: [])
    monkeypatch.setattr(
        learning, "scoring_agreement", lambda: {"value": None, "measured": False}
    )

    review = learning.build_review()
    assert review["ready"] is True
