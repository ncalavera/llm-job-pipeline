"""Tests for the learning cycle (scripts/learning.py).

Two layers:
  * PURE — the backtest, proposal computation, tokenizer, culprit detection and
    the agreement adapter fallback decide on plain data (no DB, no LLM).
  * DB integration — one migrated temp SQLite DB exercises the rollover cursor,
    the append-only log and the profile-editing apply path end to end.

Everything is person- and place-agnostic: invented orgs and titles, a throwaway
DB and a throwaway profile copy. Zero LLM spend.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import learning  # noqa: E402


# ---------------------------------------------------------------------------
# PURE — backtest
# ---------------------------------------------------------------------------

LIKED = ["Head of Programmes", "Senior Programme Manager"]
HIGH = ["Director of Operations", "Climate Policy Lead"]


def test_word_matches_title_is_whole_word_with_plural():
    assert learning.word_matches_title("casino", "Online Casino Host")
    assert learning.word_matches_title("casino", "Two casinos opening")  # plural
    assert not learning.word_matches_title("cas", "Online Casino Host")  # not a substring hit


def test_word_matches_title_matches_the_es_plural_like_the_live_filter():
    # The exact regression scenario: liked title "Head of Coaches", candidate
    # word "coach" — the live filter (scripts/filters.py) uses (?:es|s)?, so
    # "coach" DOES match "Coaches". A backtest that missed this ("s"-only
    # plural) would say CLEAN and let the user approve a word that then kills
    # the liked role for real.
    assert learning.word_matches_title("coach", "Head of Coaches")
    assert learning.word_matches_title("coach", "Senior Coach")
    assert not learning.word_matches_title("coach", "Coaching Programme")  # not a boundary match


def test_backtest_catches_the_es_plural_collision():
    bt = learning.backtest_filter_word("coach", ["Head of Coaches"], [])
    assert bt["clean"] is False
    assert bt["collisions"] == ["Head of Coaches"]


def test_word_matches_title_escapes_regex_special_characters():
    # A user word containing regex metacharacters (".", "&", ...) must be
    # treated as a LITERAL string, not compiled as a pattern — "." should not
    # act as "any character", and the word must not crash the compile.
    assert learning.word_matches_title("a.b", "See a.b standalone")
    assert not learning.word_matches_title("a.b", "See axb standalone")  # "." != any char
    assert learning.word_matches_title("m&e", "Head of M&E")
    assert not learning.word_matches_title("m&e", "Head of Me")  # "&" is literal, not optional


def test_backtest_clean_word_has_no_collisions():
    bt = learning.backtest_filter_word("casino", LIKED, HIGH)
    assert bt["clean"] is True
    assert bt["collisions"] == []
    assert bt["checked_liked"] == 2 and bt["checked_high"] == 2


def test_backtest_dirty_word_lists_the_roles_it_would_kill():
    bt = learning.backtest_filter_word("programme", LIKED, HIGH)
    assert bt["clean"] is False
    assert set(bt["collisions"]) == {"Head of Programmes", "Senior Programme Manager"}


# ---------------------------------------------------------------------------
# PURE — proposals
# ---------------------------------------------------------------------------


def test_propose_filter_words_needs_recurrence_and_clean_backtest():
    garbage = ["Casino Dealer Trainee", "Online Casino Host", "Fundraising Gala Lead"]
    out = learning.propose_filter_words(garbage, LIKED, HIGH, existing=[])
    words = [p["word"] for p in out["proposals"]]
    assert "casino" in words  # recurs (2 titles) + clean
    assert "fundraising" not in words  # appears once → below MIN_GARBAGE_HITS
    for p in out["proposals"]:
        assert p["backtest"]["clean"] is True  # every proposal carries a clean backtest


def test_propose_filter_words_rejects_a_word_that_hits_liked_history():
    # "programme" recurs in garbage but collides with liked history → rejected,
    # and the rejection carries the collisions so the screen can show WHY.
    garbage = ["Programme Assistant", "Programme Intern"]
    out = learning.propose_filter_words(garbage, LIKED, HIGH, existing=[])
    assert all(p["word"] != "programme" for p in out["proposals"])
    rej = [r for r in out["rejected"] if r["word"] == "programme"]
    assert rej and rej[0]["backtest"]["collisions"]


def test_propose_filter_words_skips_already_filtered_words():
    garbage = ["Casino Host", "Casino Dealer"]
    out = learning.propose_filter_words(garbage, LIKED, HIGH, existing=["casino"])
    assert all(p["word"] != "casino" for p in out["proposals"])


def test_propose_board_disables_flags_zero_good_boards():
    known = {"linkedin", "idealist"}
    vacs = []
    for i in range(12):
        vacs.append({"status": "passed", "locations": [{"source": "linkedin"}]})
    vacs.append({"status": "liked", "locations": [{"source": "idealist"}]})
    out = learning.propose_board_disables(vacs, known, min_seen=10)
    boards = {p["board"] for p in out}
    assert boards == {"linkedin"}  # 12 seen, 0 good
    assert "idealist" not in boards  # has a liked role


def test_propose_board_disables_ignores_unknown_sources():
    vacs = [{"status": "passed", "source": "greenhouse"} for _ in range(20)]
    assert learning.propose_board_disables(vacs, {"linkedin"}, min_seen=10) == []


def test_propose_factor_moves_promotes_a_clean_recurring_penalty():
    penalties = ["Gambling, casino and betting roles.", "Pure M&E without ownership."]
    garbage = ["Casino Dealer", "Casino Host"]
    moves = learning.propose_factor_moves(penalties, garbage, LIKED, HIGH)
    assert len(moves) == 1
    assert moves[0]["keyword"] == "casino"
    assert moves[0]["from"] == "penalty" and moves[0]["to"] == "filter"
    assert moves[0]["backtest"]["clean"] is True


# ---------------------------------------------------------------------------
# PURE — culprit + revision sampling (reads archive files, no DB)
# ---------------------------------------------------------------------------


def test_culprit_rule_finds_the_personal_filter_that_killed_a_title():
    assert learning.culprit_rule("Fundraising Lead", ["fundraising", "sales"]) == "fundraising"
    assert learning.culprit_rule("Head of Programmes", ["fundraising"]) is None


def test_sample_filter_kills_reads_archives_and_attaches_culprit(tmp_path, monkeypatch):
    import json

    arch = tmp_path / "archive"
    arch.mkdir()
    (arch / "filter_20260101_0000.json").write_text(
        json.dumps({"vacancies": {"k1": {"title": "Fundraising Lead, Climate", "org": "Beta"}}})
    )
    monkeypatch.setenv("LEARNING_ARCHIVE_DIR", str(arch))
    sample = learning.sample_filter_kills(personal_filters=["fundraising"])
    assert len(sample) == 1
    assert sample[0]["title"] == "Fundraising Lead, Climate"
    assert sample[0]["culprit"] == "fundraising"


# ---------------------------------------------------------------------------
# PURE — agreement adapter (the golden-set eval harness integration seam)
# ---------------------------------------------------------------------------


def test_measure_agreement_reads_from_the_env_cmd_seam(monkeypatch):
    monkeypatch.setenv("LEARNING_AGREEMENT_CMD", "echo 87.5")
    val, src, measured_at = learning._measure_agreement()
    assert val == 87.5
    assert src and src.startswith("cmd:")
    assert measured_at is None  # the cmd escape hatch carries no timestamp


def test_measure_agreement_falls_back_when_no_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("LEARNING_AGREEMENT_CMD", raising=False)
    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path / "evals"))  # never measured here
    val, src, measured_at = learning._measure_agreement()
    assert val is None and src is None and measured_at is None


def test_measure_agreement_wires_the_golden_set_summary_end_to_end(monkeypatch, tmp_path):
    """golden_set.py's `measure` persists a summary; learning.py's probe
    (module 'golden_set') must find it and carry both the number and the
    measured-at date through to the review payload."""
    import json

    monkeypatch.delenv("LEARNING_AGREEMENT_CMD", raising=False)
    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path / "evals"))

    import golden_set

    summary_path = golden_set._summary_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "agreement_pct": 91.0,
                "set_size": 12,
                "set_version": 3,
                "threshold": 60,
                "measured_at": "2026-07-01T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    val, src, measured_at = learning._measure_agreement()
    assert val == 91.0
    assert src == "golden_set"
    assert measured_at == "2026-07-01T12:00:00+00:00"

    payload = learning.scoring_agreement()
    assert payload["value"] == 91.0
    assert payload["measured"] is True
    assert payload["measured_at"] == "2026-07-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# DB INTEGRATION — rollover cursor, ledger, apply path (migrated temp SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture()
def learn_db(tmp_path, monkeypatch):
    """A fully-migrated throwaway SQLite DB + a throwaway profile copy."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobsearch.db"))

    prof = tmp_path / "user_profile.md"
    src = (REPO / "config" / "user_profile.example.md").read_text(encoding="utf-8")
    src = src.replace("exclude_title_keywords: (none)", "exclude_title_keywords: fundraising")
    prof.write_text(src, encoding="utf-8")
    monkeypatch.setenv("USER_PROFILE_PATH", str(prof))
    monkeypatch.setenv("LEARNING_ARCHIVE_DIR", str(tmp_path / "archive"))
    # Isolate the golden-set dir too, so a real evals/golden_set_summary.json
    # left over from an actual /jobs-eval run on the developer's machine can
    # never leak an agreement number into these tests.
    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path / "evals"))

    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
        "hard_filters",
        "prompts",
        "factors",
        "learning",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import learning as lrn

    importlib.reload(lrn)
    yield lrn, prof
    try:
        from db_conn import close_conn

        close_conn()
    except Exception:
        pass


def _seed(lrn):
    import database_supabase as db
    from db_conn import get_conn

    db.ensure_company("Acme Foundation", status="active")
    get_conn().commit()
    cur = get_conn().cursor()
    cur.execute("SELECT id FROM company WHERE canonical_name = %s", ("Acme Foundation",))
    cid = cur.fetchone()[0]

    def add(dh, title, status="unseen", score=None):
        cur.execute(
            "INSERT INTO vacancy (dedup_hash, company_id, title, first_seen, last_seen, "
            "status, status_updated_at, llm_score) VALUES (%s,%s,%s,now(),now(),%s,now(),%s)",
            (dh, cid, title, status, score),
        )

    add("h1", "Head of Programmes", status="liked", score=82)
    add("h2", "Senior Programme Manager", status="applied", score=78)
    add("h3", "Director of Operations", status="unseen", score=71)
    add("p1", "Casino Floor Supervisor", status="passed", score=8)
    get_conn().commit()


def test_table_ready_after_migration(learn_db):
    lrn, _ = learn_db
    assert lrn.table_ready() is True


def test_garbage_rolls_over_until_review_completes(learn_db):
    lrn, _ = learn_db
    _seed(lrn)
    lrn.record_garbage("g1", "Casino Dealer Trainee", source="linkedin", score=5)
    lrn.record_garbage("g2", "Online Casino Host", source="linkedin", score=7)

    # 3 decision-status verdicts (liked, applied, passed); skipped/unseen excluded.
    assert lrn.decided_since_cursor() == 3
    assert len(lrn.undiscussed_garbage()) == 2

    # A skip writes no 'reviewed' row → still undiscussed on the next look.
    assert len(lrn.undiscussed_garbage()) == 2

    # Completing the review advances the cursor → garbage no longer undiscussed.
    lrn.mark_reviewed(agreement="skip", applied_count=0)
    assert lrn.undiscussed_garbage() == []


def test_build_review_gates_with_a_clean_backtested_proposal(learn_db):
    lrn, _ = learn_db
    _seed(lrn)
    lrn.record_garbage("g1", "Casino Dealer Trainee", source="linkedin", score=5)
    lrn.record_garbage("g2", "Online Casino Host", source="linkedin", score=7)

    review = lrn.build_review()
    assert review["ready"] and review["has_content"]
    assert review["verdicts_since_last_review"] == 3
    props = review["proposals"]["filter_words"]
    casino = [p for p in props if p["word"] == "casino"]
    assert casino and casino[0]["backtest"]["clean"] is True
    # agreement degrades gracefully (golden-set harness absent in the test env).
    assert review["agreement"]["measured"] is False
    assert review["agreement"]["note"]


def test_apply_add_filter_word_edits_profile_and_logs(learn_db):
    lrn, prof = learn_db
    _seed(lrn)
    res = lrn.apply_add_filter_word("casino")
    assert "casino" in res["filters_now"]
    import hard_filters

    assert "casino" in hard_filters.load_hard_filters()["exclude_title_keywords"]
    log = lrn.applied_log()
    assert any(e["kind"] == "add_filter_word" and e["detail"]["word"] == "casino" for e in log)


def test_apply_add_filter_word_snapshots_a_backup(learn_db):
    lrn, prof = learn_db
    _seed(lrn)
    original = prof.read_text(encoding="utf-8")
    lrn.apply_add_filter_word("casino")
    backup = prof.with_name(prof.name + ".bak")
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original


def test_apply_add_filter_word_crash_before_replace_leaves_original_intact(learn_db, monkeypatch):
    """Simulated crash between the tmp write and the atomic rename: os.replace
    raises, so the live profile file must be untouched (only the tmp path was
    written to)."""
    lrn, prof = learn_db
    _seed(lrn)
    original = prof.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(lrn.os, "replace", boom)

    with pytest.raises(OSError):
        lrn.apply_add_filter_word("casino")

    assert prof.read_text(encoding="utf-8") == original
    assert not any(e["kind"] == "add_filter_word" for e in lrn.applied_log())


def test_weaken_filter_word_removes_culprit_and_logs(learn_db):
    lrn, _ = learn_db
    _seed(lrn)
    lrn.apply_remove_filter_word("fundraising")
    import hard_filters

    assert "fundraising" not in hard_filters.load_hard_filters()["exclude_title_keywords"]
    assert any(e["kind"] == "weaken_filter_word" for e in lrn.applied_log())


def test_move_factor_promotes_penalty_to_filter_and_logs(learn_db):
    lrn, _ = learn_db
    _seed(lrn)
    res = lrn.apply_factor_move("Gambling, casino and betting.", "casino")
    assert "casino" in res["filters_now"]
    import hard_filters

    assert "casino" in hard_filters.load_hard_filters()["exclude_title_keywords"]
    assert any(e["kind"] == "move_factor" for e in lrn.applied_log())
