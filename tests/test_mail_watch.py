"""scripts/mail_watch.py — rules matcher (U1) and run loop (U2)."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import mail_watch as mw  # noqa: E402

RULES = {
    "own_addresses": ["me@example.com"],
    "platform_domains": ["ashbyhq.com", "pageuppeople.com"],
    "org_domains": ["acme.example"],
    "subject_phrases": ["thanks for applying", "next steps", "candidate"],
    "exclude_domains": ["github.com", "team@board.example"],
}


# --- U1: rules and matcher ---------------------------------------------------


def test_org_domain_matches_without_phrase():
    assert mw.classify("Ann <recruiter@acme.example>", "Re: Ops Lead role", RULES) == "org_domain"


def test_excluded_sender_with_phrase_is_none():
    assert mw.classify("GitHub <noreply@github.com>", "A third-party OAuth application has been added", RULES) is None


def test_own_address_is_none():
    assert mw.classify("Me <me@example.com>", "Re: ... Next steps", RULES) is None


def test_subdomain_matches_platform():
    assert mw.classify("HRTeam-671@mail.pageuppeople.com", "Application outcome", RULES) == "platform_domain"


def test_phrase_only_matches():
    assert mw.classify("hr@unknown-startup.io", "Thanks for applying to Unknown", RULES) == "subject:thanks for applying"


def test_exclusion_beats_phrase():
    assert mw.classify("team@board.example", "88 new roles for candidates", RULES) is None


def test_excluded_address_only_excludes_that_sender():
    assert mw.classify("team@board.example", "Here are your next steps", RULES) is None
    assert mw.classify("advising@board.example", "Here are your next steps", RULES) == "subject:next steps"


def test_display_name_spoof_is_none():
    assert mw.classify('"Acme Recruiting acme.example" <spam@evil.example>', "hello", RULES) is None


def test_similar_domain_does_not_match():
    assert mw.classify("hr@notpageuppeople.com", "hello", RULES) is None


def test_load_rules_lowercases_and_mixed_case_subject(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text(
        'own_addresses=[]\nplatform_domains=["AshbyHQ.com"]\norg_domains=[]\n'
        'subject_phrases=["Next Steps"]\nexclude_domains=[]\n'
    )
    rules = mw.load_rules(p)
    assert mw.classify("x@ashbyhq.com", "NEXT STEPS", rules) == "platform_domain"
    assert mw.classify("x@other.com", "Your NEXT STEPS", rules) == "subject:next steps"


def test_load_rules_missing_file_names_path(tmp_path):
    with pytest.raises(FileNotFoundError, match="nope.toml"):
        mw.load_rules(tmp_path / "nope.toml")


def test_load_rules_missing_key_names_key(tmp_path):
    p = tmp_path / "r.toml"
    p.write_text('own_addresses=[]\nplatform_domains=[]\norg_domains=[]\nsubject_phrases=[]\n')
    with pytest.raises(KeyError, match="exclude_domains"):
        mw.load_rules(p)


def test_example_rules_file_loads():
    rules = mw.load_rules(Path(__file__).resolve().parent.parent / "config" / "mail_watch_rules.example.toml")
    assert "greenhouse.io" in rules["platform_domains"]
    assert rules["org_domains"] == []


# --- U2: run loop ------------------------------------------------------------


class FakeService:
    """Enough of the Gmail client surface for fetch_new: list pages + batch get."""

    def __init__(self, messages, page_size=100):
        self.by_id = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]
        self.page_size = page_size
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        start = int(pageToken or 0)
        chunk = self.order[start : start + self.page_size]
        resp = {"messages": [{"id": i} for i in chunk]}
        if start + self.page_size < len(self.order):
            resp["nextPageToken"] = str(start + self.page_size)
        return _Exec(resp)

    def get(self, userId, id, format, metadataHeaders):
        m = self.by_id[id]
        return _Exec(
            {
                "threadId": m.get("threadId", "t-" + id),
                "internalDate": str(m.get("internalDate", 1_700_000_000_000)),
                "snippet": m.get("snippet", ""),
                "payload": {"headers": [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]}]},
            }
        )

    def new_batch_http_request(self, callback):
        return _Batch(callback)


class _Exec:
    def __init__(self, resp):
        self.resp = resp

    def execute(self):
        return self.resp


class _Batch:
    def __init__(self, cb):
        self.cb, self.items = cb, []

    def add(self, req, request_id):
        self.items.append((request_id, req))

    def execute(self):
        for rid, req in self.items:
            self.cb(rid, req.execute(), None)


def msg(i, frm, subject, **kw):
    return {"id": i, "from": frm, "subject": subject, **kw}


MATCH = msg("m1", "Ann <recruiter@acme.example>", "Re: Ops Lead | Acme - Next steps", snippet="Hi there, <b>test</b> & more")
NOISE = msg("n1", "team@board.example", "88 new roles")
PLAIN = msg("p1", "friend@example.org", "lunch?")


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state.json"


def seeded(state, ids=()):
    state.write_text(json.dumps({"seen": {i: int(time.time() * 1000) for i in ids}, "seeded_at": 1}))
    return state


def test_seed_run_records_and_sends_nothing(state):
    sends = []
    r = mw.run_once(RULES, state, FakeService([MATCH, NOISE, PLAIN]), sends.append)
    assert r["seed"] and sends == []
    assert set(json.loads(state.read_text())["seen"]) == {"m1", "n1", "p1"}


def test_match_sends_once_with_content(state):
    seeded(state)
    sends = []
    mw.run_once(RULES, state, FakeService([MATCH]), sends.append)
    assert len(sends) == 1
    text = sends[0]
    assert "recruiter@acme.example" in text and "Next steps" in text
    assert "https://mail.google.com/mail/u/0/#inbox/t-m1" in text
    assert "&lt;b&gt;test&lt;/b&gt; &amp; more" in text  # snippet escaped
    assert "m1" in json.loads(state.read_text())["seen"]


def test_seen_id_does_not_resend(state):
    seeded(state, ["m1"])
    sends = []
    mw.run_once(RULES, state, FakeService([MATCH]), sends.append)
    assert sends == []


def test_failed_send_retries_next_run(state):
    seeded(state)

    def boom(text):
        raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError):
        mw.run_once(RULES, state, FakeService([MATCH]), boom)
    assert "m1" not in json.loads(state.read_text()).get("seen", {})
    sends = []
    mw.run_once(RULES, state, FakeService([MATCH]), sends.append)
    assert len(sends) == 1 and "m1" in json.loads(state.read_text())["seen"]


def test_non_matching_recorded_not_sent(state):
    seeded(state)
    sends = []
    mw.run_once(RULES, state, FakeService([NOISE, PLAIN]), sends.append)
    assert sends == [] and set(json.loads(state.read_text())["seen"]) == {"n1", "p1"}


def test_two_matches_two_sends(state):
    seeded(state)
    sends = []
    m2 = msg("m2", "no-reply@ashbyhq.com", "Thanks for applying")
    mw.run_once(RULES, state, FakeService([MATCH, m2]), sends.append)
    assert len(sends) == 2


def test_prune_old_seen(state):
    now = 1_800_000_000
    state.write_text(json.dumps({"seen": {"old": (now - 8 * 86400) * 1000, "new": (now - 86400) * 1000}, "seeded_at": 1}))
    mw.run_once(RULES, state, FakeService([]), lambda t: None, now=now)
    assert set(json.loads(state.read_text())["seen"]) == {"new"}


def test_send_cap_leaves_rest_unseen(state):
    seeded(state)
    sends = []
    many = [msg(f"a{i}", "no-reply@ashbyhq.com", "hi", internalDate=1_700_000_000_000 + i) for i in range(25)]
    r = mw.run_once(RULES, state, FakeService(many), sends.append)
    assert len(sends) == 20 and r["matched"] == 25
    assert len(json.loads(state.read_text())["seen"]) == 20


def test_corrupt_state_fails_and_sends_nothing(state):
    state.write_text("{")
    sends = []
    with pytest.raises(json.JSONDecodeError):
        mw.run_once(RULES, state, FakeService([MATCH]), sends.append)
    assert sends == []


def test_replay_lists_pages_and_ignores_seen(state, capsys):
    seeded(state, ["m1", "n1"])
    svc = FakeService([MATCH, NOISE, PLAIN], page_size=2)
    assert mw.replay(RULES, svc, 90) == 3
    assert state.read_text()  # untouched
    assert svc.queries[0] == "newer_than:90d -in:spam -in:trash"
    out = capsys.readouterr().out
    assert "org_domain" in out and "no match" in out


def test_default_query_is_all_incoming_mail(state):
    seeded(state)
    svc = FakeService([])
    mw.run_once(RULES, state, svc, lambda t: None)
    assert svc.queries == ["newer_than:2d -in:sent -in:spam -in:trash -in:draft"]
    mw.run_once(RULES, state, svc, lambda t: None, query="newer_than:7d")
    assert svc.queries[-1] == "newer_than:7d"


def test_dry_run_prints_and_writes_nothing(state, capsys):
    seeded(state)
    before = state.read_text()
    sends = []
    mw.run_once(RULES, state, FakeService([MATCH]), sends.append, dry_run=True)
    assert sends == [] and state.read_text() == before
    assert "would send: id=m1 reason=org_domain" in capsys.readouterr().out


# --- KTD6: failure counter and escalation ----------------------------------


def test_escalates_on_third_failure_and_escapes(state):
    sends = []
    for _ in range(3):
        mw.record_failure(state, "<HttpError 401 when requesting>", sends.append, now=1000)
    assert len(sends) == 1
    assert "failing" in sends[0] and "&lt;HttpError 401" in sends[0]


def test_escalation_repeats_after_six_hours_only(state):
    sends = []
    for i in range(3):
        mw.record_failure(state, "e", sends.append, now=1000 + i)
    mw.record_failure(state, "e", sends.append, now=1000 + 3600)
    assert len(sends) == 1
    mw.record_failure(state, "e", sends.append, now=1000 + 7 * 3600)
    assert len(sends) == 2


def test_success_resets_counter(state):
    for _ in range(2):
        mw.record_failure(state, "e", lambda t: None, now=1000)
    mw.update_state_file(state, consecutive_failures=0)
    assert mw.read_state_file(state)["consecutive_failures"] == 0


def test_mask_hides_registered_token_values(monkeypatch):
    monkeypatch.setattr(mw, "_extra_secrets", ["1//refresh-token-value"])
    assert "refresh-token-value" not in mw.mask("invalid_grant for 1//refresh-token-value")


def test_main_requires_dry_run_for_since_days(state, monkeypatch):
    monkeypatch.setenv("MAIL_WATCH_STATE_FILE", str(state))
    with pytest.raises(SystemExit):
        mw.main(["--since-days", "3"])


def test_main_counts_failure_on_missing_rules(state, tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_WATCH_STATE_FILE", str(state))
    monkeypatch.setenv("MAIL_WATCH_RULES", str(tmp_path / "missing.toml"))
    monkeypatch.setattr(mw, "telegram_send", lambda t: None)
    assert mw.main([]) == 1
    assert mw.read_state_file(state)["consecutive_failures"] == 1
    assert "missing.toml" in mw.read_state_file(state)["last_error"]


def test_batch_retries_429_then_succeeds(monkeypatch):
    """Gmail 429 on a batch: the failed ids are retried after a backoff."""

    class Err(Exception):
        status_code = 429

    calls = {"n": 0}

    class Flaky(FakeService):
        def new_batch_http_request(self, callback):
            calls["n"] += 1
            fail_first = calls["n"] == 1
            outer = self

            class B(_Batch):
                def execute(self):
                    for rid, req in self.items:
                        if fail_first and rid == "m1":
                            self.cb(rid, None, Err("429"))
                        else:
                            self.cb(rid, req.execute(), None)

            return B(callback)

    monkeypatch.setattr(mw.time, "sleep", lambda s: None)
    metas = mw.fetch_new(Flaky([MATCH, PLAIN]), {})
    assert calls["n"] == 2 and {m["id"] for m in metas} == {"m1", "p1"}
    assert next(m for m in metas if m["id"] == "m1")["subject"].startswith("Re: Ops Lead")


def test_failed_first_run_still_seeds_next_success(state):
    """A failure before any seed leaves a state file with only a counter; the
    next successful run must still seed, not alert on old mail."""
    mw.record_failure(state, "boom", lambda t: None, now=1000)
    sends = []
    r = mw.run_once(RULES, state, FakeService([MATCH]), sends.append)
    assert r["seed"] and sends == []


def test_batch_non_retryable_error_raises(monkeypatch):
    class Err(Exception):
        status_code = 404

    class Broken(FakeService):
        def new_batch_http_request(self, callback):
            class B(_Batch):
                def execute(self):
                    for rid, req in self.items:
                        self.cb(rid, None, Err("404"))

            return B(callback)

    monkeypatch.setattr(mw.time, "sleep", lambda s: None)
    with pytest.raises(Err):
        mw.fetch_new(Broken([MATCH]), {})


def test_escalation_send_failure_does_not_raise(state):
    def boom(text):
        raise RuntimeError("telegram down")

    for i in range(3):
        mw.record_failure(state, "e", boom, now=1000 + i)
    st = mw.read_state_file(state)
    assert st["consecutive_failures"] == 3 and "last_escalation_at" not in st


def test_error_text_decodes_bytes_content():
    class E(Exception):
        content = b'{"error":"invalid_grant"}' + b"x" * 1000

    out = mw._error_text(E("<HttpError 401>"))
    assert out.startswith("E: <HttpError 401>") and '{"error":"invalid_grant"}' in out and "b'" not in out
    assert len(out) < 700
