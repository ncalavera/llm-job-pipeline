"""Tests for the golden-set scoring-quality check (scripts/golden_set.py).

Pure logic only — no DB, no LLM. The DB-reading helpers (_load_verdict_vacancies
etc.) are thin wrappers over the DAL and are exercised by the acceptance run, not
here; these tests pin the labelling, append-only versioning, and metric maths.
"""

import json

import pytest

import golden_set as g


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------


def test_verdict_label_maps_liked_basket_to_fit():
    for s in ("liked", "to_apply", "to_research", "to_network", "applied"):
        assert g.verdict_label(s) == "fit"


def test_verdict_label_maps_passed_basket_to_nofit():
    assert g.verdict_label("passed") == "nofit"
    assert g.verdict_label("skipped") == "nofit"


def test_verdict_label_none_for_non_verdict_statuses():
    for s in ("unseen", "expiring", "archived", None, "weird"):
        assert g.verdict_label(s) is None


def test_auto_expiry_only_passed_with_past_deadline():
    assert g.is_probable_auto_expiry(
        {"status": "passed", "deadline": "2000-01-01"}, today="2026-07-02"
    )
    # future deadline → a real pass
    assert not g.is_probable_auto_expiry(
        {"status": "passed", "deadline": "2999-01-01"}, today="2026-07-02"
    )
    # no deadline → a real pass
    assert not g.is_probable_auto_expiry({"status": "passed"}, today="2026-07-02")
    # skipped is always a genuine user verdict, never auto-expiry
    assert not g.is_probable_auto_expiry(
        {"status": "skipped", "deadline": "2000-01-01"}, today="2026-07-02"
    )


# ---------------------------------------------------------------------------
# Seed selection
# ---------------------------------------------------------------------------


def _vac(
    h, status, score=50, desc="a description long enough", updated="2026-01-01", deadline=None
):
    v = {
        "dedup_hash": h,
        "status": status,
        "llm_score": score,
        "full_description": desc,
        "org": "Placeholder Org",
        "title": "Some Role",
        "status_updated_at": updated,
    }
    if deadline:
        v["deadline"] = deadline
    return v


def test_seed_balance_guarantees_scarce_fit_examples():
    vacs = [_vac(f"f{i}", "liked", score=80, updated=f"2026-01-0{i + 1}") for i in range(2)]
    vacs += [_vac(f"n{i}", "passed", score=10, updated=f"2026-02-{i + 1:02d}") for i in range(20)]
    recs = g.select_seed_records(vacs, limit=10, balance=True)
    assert len(recs) == 10
    assert sum(1 for r in recs if r["label"] == "fit") == 2  # both scarce positives kept


def test_seed_excludes_unscored_blind_and_autoexpiry():
    vacs = [
        _vac("scored", "passed", score=10),
        _vac("unscored", "passed", score=None),
        _vac("blind", "passed", score=10, desc="  "),
        _vac("expired", "passed", score=90, deadline="2000-01-01"),
        _vac("unseen", "unseen", score=10),
    ]
    recs = g.select_seed_records(vacs, limit=50, balance=False, today="2026-07-02")
    ids = {r["id"] for r in recs}
    assert ids == {"scored"}


def test_seed_record_shape_freezes_input():
    rec = g.make_record(_vac("h1", "applied", score=77), "fit", "great", "seed:applied")
    assert rec["id"] == "h1"
    assert rec["label"] == "fit"
    assert rec["source"] == "seed:applied"
    # frozen scoring input, no live status/score fields leaking in
    assert set(rec["vacancy"]) == {"org", "title", "tier", "locations", "full_description"}


# ---------------------------------------------------------------------------
# Append-only store + versioning
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "golden_set.jsonl"


def test_append_stamps_version_and_timestamp(store):
    recs = [g.make_record(_vac("a", "liked"), "fit", "", "seed:liked")]
    summary = g.append_records(store, recs)
    assert summary["added"] == 1 and summary["version"] == 1
    row = json.loads(store.read_text().splitlines()[0])
    assert row["version"] == 1 and "labeled_at" in row


def test_append_is_idempotent_by_id(store):
    recs = [g.make_record(_vac("a", "liked"), "fit", "", "seed:liked")]
    g.append_records(store, recs)
    again = g.append_records(store, recs)
    assert again["added"] == 0 and again["skipped"] == 1
    assert again["version"] == 1  # no empty version bump
    assert len(store.read_text().splitlines()) == 1  # old line untouched


def test_append_dedups_within_one_batch(store):
    dup = g.make_record(_vac("a", "liked"), "fit", "", "seed:liked")
    summary = g.append_records(store, [dup, dup])
    assert summary["added"] == 1 and summary["skipped"] == 1


def test_new_batch_increments_version_and_old_rows_frozen(store):
    g.append_records(store, [g.make_record(_vac("a", "liked"), "fit", "", "seed:liked")])
    g.append_records(store, [g.make_record(_vac("b", "passed"), "nofit", "", "seed:passed")])
    v1 = g.load_records(store, version=1)
    assert [r["id"] for r in v1] == ["a"]  # freeze excludes the v2 row
    assert len(g.load_records(store)) == 2
    assert g.current_version(store) == 2


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _records(labels):
    return [
        g.make_record(_vac(str(i), "liked" if lbl == "fit" else "passed"), lbl, f"r{i}", "x")
        for i, lbl in enumerate(labels)
    ]


def test_metrics_perfect_agreement():
    recs = _records(["fit", "fit", "nofit", "nofit"])
    scores = {r["id"]: (80 if r["label"] == "fit" else 10) for r in recs}
    m = g.compute_metrics(recs, scores, threshold=60)
    assert m["agreement"] == 1.0
    assert m["precision"] == 1.0 and m["recall"] == 1.0 and m["f1"] == 1.0
    assert m["disagreements"] == []


def test_metrics_confusion_and_disagreements():
    recs = _records(["fit", "fit", "nofit", "nofit"])
    # one FN (fit scored low) + one FP (nofit scored high)
    scores = {
        recs[0]["id"]: 80,  # TP
        recs[1]["id"]: 30,  # FN
        recs[2]["id"]: 90,  # FP
        recs[3]["id"]: 10,  # TN
    }
    m = g.compute_metrics(recs, scores, threshold=60)
    assert m["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert m["agreement"] == 0.5
    assert m["precision"] == 0.5 and m["recall"] == 0.5
    kinds = {d["kind"] for d in m["disagreements"]}
    assert kinds == {"false_negative", "false_positive"}


def test_metrics_reports_missing_scores():
    recs = _records(["fit", "nofit"])
    scores = {recs[0]["id"]: 80}  # second has no score
    m = g.compute_metrics(recs, scores, threshold=60)
    assert m["n"] == 1
    assert m["missing_scores"] == [recs[1]["id"]]


def test_metrics_precision_none_when_no_positive_predictions():
    recs = _records(["fit", "nofit"])
    scores = {recs[0]["id"]: 10, recs[1]["id"]: 5}  # model predicts nofit for all
    m = g.compute_metrics(recs, scores, threshold=60)
    assert m["precision"] is None  # no positive predictions → undefined, not a crash
    # format_report must survive None metrics
    assert "AGREEMENT" in g.format_report(m)


def test_disagreements_sorted_nearest_boundary_first():
    recs = _records(["nofit", "nofit"])
    scores = {recs[0]["id"]: 95, recs[1]["id"]: 62}  # both FP; 62 is nearer the 60 boundary
    m = g.compute_metrics(recs, scores, threshold=60)
    assert [d["model_score"] for d in m["disagreements"]] == [62, 95]


# ---------------------------------------------------------------------------
# Learning-cycle seam — `measure` persists a summary; scripts/learning.py's
# agreement adapter reads it back via scoring_agreement_pct/_summary.
# ---------------------------------------------------------------------------


def test_write_summary_persists_the_headline_fields(tmp_path):
    recs = _records(["fit", "fit", "nofit", "nofit"])
    scores = {r["id"]: (80 if r["label"] == "fit" else 10) for r in recs}
    m = g.compute_metrics(recs, scores, threshold=60)
    path = tmp_path / "golden_set_summary.json"

    summary = g.write_summary(path, m, version=3)

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == summary
    assert summary["agreement_pct"] == 100.0
    assert summary["set_size"] == 4
    assert summary["set_version"] == 3
    assert summary["threshold"] == 60
    assert "measured_at" in summary and summary["measured_at"]


def test_scoring_agreement_summary_and_pct_absent_when_never_measured(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path))
    assert g.scoring_agreement_summary() is None
    assert g.scoring_agreement_pct() is None


def test_scoring_agreement_pct_reads_the_persisted_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path))
    recs = _records(["fit", "nofit"])
    scores = {recs[0]["id"]: 80, recs[1]["id"]: 10}
    m = g.compute_metrics(recs, scores, threshold=60)
    g.write_summary(g._summary_path(), m, version=1)

    assert g.scoring_agreement_pct() == 100.0
    summary = g.scoring_agreement_summary()
    assert summary["set_size"] == 2 and summary["set_version"] == 1


def test_cmd_measure_persists_the_summary_end_to_end(tmp_path, monkeypatch, capsys):
    import argparse
    import io

    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path))
    store = g._store_path()
    recs = [g.make_record(_vac("a", "liked", score=80), "fit", "", "seed:liked")]
    g.append_records(store, recs)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"id": "a", "score": 80}])))
    args = argparse.Namespace(version=None, threshold=60)
    rc = g.cmd_measure(args)
    assert rc == 0

    summary = g.scoring_agreement_summary()
    assert summary is not None
    assert summary["agreement_pct"] == 100.0
    assert summary["set_size"] == 1
    assert summary["set_version"] == 1
    assert summary["threshold"] == 60


# ---------------------------------------------------------------------------
# Payload builder — reuses the pipeline prompt, stays blind to the label
# ---------------------------------------------------------------------------


def test_build_payloads_are_blind_and_use_the_real_prompt():
    recs = [
        g.make_record(
            _vac("a", "liked", desc="Lead operations for a mission-driven org."),
            "fit",
            "secret reason",
            "seed:liked",
        )
    ]
    payloads = g.build_payloads(recs)
    assert len(payloads) == 1
    p = payloads[0]
    assert p["id"] == "a"
    assert p["payload_kind"] == "vacancy"
    # scorer must not see the ground truth
    assert "label" not in p and "reason" not in p
    assert "secret reason" not in json.dumps(p)
    # real scoring prompt + user message are attached
    assert p["system_prompt"].strip()
    assert "Lead operations" in p["user_msg"]


def test_threshold_default_reads_env(monkeypatch):
    monkeypatch.setenv("APPLYABLE_SCORE", "55")
    assert g._threshold_default() == 55
    monkeypatch.delenv("APPLYABLE_SCORE", raising=False)
    assert g._threshold_default() == g.DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Company mode — labelling from status/status_reason, freezing, payloads.
# Fake companies only (neutral names): the real set is personal + gitignored.
# ---------------------------------------------------------------------------


def _co(cid, status, reason, content="x" * 300, score=None, **extra):
    c = {
        "id": cid,
        "canonical_name": f"Fake Org {cid}",
        "website": "https://example.test",
        "category": "Test category",
        "status": status,
        "status_reason": reason,
        "content": content,
    }
    if score is not None:
        c["alignment_score"] = score
    c.update(extra)
    return c


def test_company_label_maps_hand_verdicts():
    assert g.company_label("active", "approved via dashboard") == "fit"
    assert g.company_label("inactive", "rejected via dashboard") == "nofit"
    # a free-text hand reason on an inactive company is still the user's reject
    assert g.company_label("inactive", "strictly commercial, not interested") == "nofit"


def test_company_label_excludes_machine_status_changes():
    # auto-approve / auto-reject are the model's own call, not a hand verdict
    assert g.company_label("active", "auto-approved: alignment=72.0") is None
    for reason in (
        "auto-rejected: alignment=12.0",
        "triage drop: drop_commercial",
        "pre-filter: university",
        "board noise — filtered",
        "audit-242: small philanthropy",
        "merged_into:abc123",
        "merged into abc123",
        "archived",
        "cadence reset 2026-06",
    ):
        assert g.company_label("inactive", reason) is None, reason


def test_company_label_screen_prefix_is_not_a_hand_label():
    # mirrors the vacancy rule: a screen drop is a cheap pre-filter, not a verdict
    assert g.company_label("inactive", "screen: off-thesis") is None
    assert not g._is_hand_reject_reason("screen: whatever")


def test_company_label_none_for_candidate_and_empty():
    assert g.company_label("candidate", None) is None
    assert g.company_label("inactive", None) is None
    assert g.company_label("inactive", "   ") is None


def test_freeze_company_keeps_only_scoring_input():
    frozen = g._freeze_company(_co("a", "active", "approved via dashboard"), "some evidence text")
    assert set(frozen) == {
        "canonical_name",
        "website",
        "category",
        "tier",
        "product",
        "experience_match",
        "personal_interest",
        "user_comments",
        "content",
    }
    assert frozen["content"] == "some evidence text"
    # no live status / status_reason / alignment_score leaks into the frozen input
    assert "status" not in frozen and "alignment_score" not in frozen


def test_make_company_record_shape_and_optional_flag():
    rec = g.make_company_record(
        _co("a", "active", "approved via dashboard"), "fit", "why", "seed:active", "content"
    )
    assert rec["id"] == "a" and rec["label"] == "fit" and rec["source"] == "seed:active"
    assert "company" in rec and "flag" not in rec
    flagged = g.make_company_record(
        _co("b", "inactive", "rejected via dashboard"),
        "nofit",
        "",
        "seed:inactive",
        "c",
        flag="check",
    )
    assert flagged["flag"] == "check"


def test_select_company_seed_balances_and_flags_high_score_nofit():
    fits = [_co(f"f{i}", "active", "approved via dashboard", score=80) for i in range(10)]
    # one nofit the model liked (>=60 → flag), one it disliked (<60 → no flag)
    nofits = [
        _co("n_high", "inactive", "rejected via dashboard", score=75),
        _co("n_low", "inactive", "rejected via dashboard", score=20),
    ]
    for c in fits + nofits:
        c["label"] = g.company_label(c["status"], c["status_reason"])
    recs = g.select_company_seed_records(fits + nofits, limit=4, balance=True)
    labels = [r["label"] for r in recs]
    assert labels.count("fit") == 2 and labels.count("nofit") == 2  # even split
    by_id = {r["id"]: r for r in recs}
    assert by_id["n_high"].get("flag")  # high stored WANT + hand reject → flagged
    assert not by_id["n_low"].get("flag")


def test_build_company_payloads_are_blind_and_use_the_real_prompt():
    rec = g.make_company_record(
        _co("a", "active", "approved via dashboard"),
        "fit",
        "secret reason",
        "seed:active",
        "Mission-driven work on global development outcomes.",
    )
    payloads = g.build_company_payloads([rec])
    assert len(payloads) == 1
    p = payloads[0]
    assert p["id"] == "a"
    assert p["payload_kind"] == "company"
    # scorer must not see the ground truth
    assert "label" not in p and "reason" not in p and "flag" not in p
    assert "secret reason" not in json.dumps(p)
    # real company scoring prompt + user message with the frozen content
    assert p["system_prompt"].strip()
    assert "Mission-driven work" in p["user_msg"]


def test_company_metrics_round_trip_via_display():
    recs = [
        g.make_company_record(_co("a", "active", "approved via dashboard"), "fit", "", "s", "c"),
        g.make_company_record(
            _co("b", "inactive", "rejected via dashboard"), "nofit", "", "s", "c"
        ),
    ]
    scores = {"a": 80, "b": 30}
    m = g.compute_metrics(recs, scores, threshold=60)
    assert m["agreement"] == 1.0
    # display resolves company records to (canonical_name, category), not vacancy fields
    org, title = g._display(recs[0])
    assert org == "Fake Org a" and title == "Test category"


def test_company_threshold_default_reads_env(monkeypatch):
    monkeypatch.setenv("AUTO_REVIEW_APPROVE", "70")
    assert g._company_threshold_default() == 70
    monkeypatch.delenv("AUTO_REVIEW_APPROVE", raising=False)
    assert g._company_threshold_default() == g.DEFAULT_THRESHOLD


def test_cmd_measure_company_persists_to_company_summary(tmp_path, monkeypatch):
    import argparse
    import io

    monkeypatch.setenv("GOLDEN_SET_DIR", str(tmp_path))
    store = g._company_store_path()
    recs = [
        g.make_company_record(_co("a", "active", "approved via dashboard"), "fit", "", "s", "c")
    ]
    g.append_records(store, recs)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([{"id": "a", "score": 80}])))
    args = argparse.Namespace(version=None, threshold=None, kind="company")
    assert g.cmd_measure(args) == 0

    # written to the COMPANY summary path, and the vacancy summary stays absent
    assert g._company_summary_path().exists()
    assert not g._summary_path().exists()
    summary = json.loads(g._company_summary_path().read_text(encoding="utf-8"))
    assert summary["agreement_pct"] == 100.0 and summary["set_size"] == 1
