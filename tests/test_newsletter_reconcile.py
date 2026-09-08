import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import newsletter_reconcile as nr  # noqa: E402


def test_extracts_direct_and_encoded_mailchimp_links_and_drops_actions():
    body = """<a href="https://jobs.80000hours.org/?jobPk=123">Role</a>
    <a href="https://mailchi.mp/x?u=https%3A%2F%2Fjobs.80000hours.org%2F%3FjobPk%3D124">Role 2</a>
    <a href="https://jobs.80000hours.org/unsubscribe">unsubscribe</a>
    <a href="https://evil.example/?jobPk=999">bad</a>"""
    assert [x["external_id"] for x in nr.extract_links(body)] == ["123", "124"]


def test_follows_one_known_80k_mailchimp_tracking_redirect(monkeypatch):
    class Response:
        headers = {"Location": "https://jobs.80000hours.org/?jobPk=777"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith(
                (
                    "https://80000hours.us2.list-manage.com/track/click",
                    "https://us.list-manage.com/LOXN2jxMbBF",
                )
            )
            return Response()

    monkeypatch.setattr(nr, "build_opener", lambda handler: Opener())
    for url in (
        "https://80000hours.us2.list-manage.com/track/click?u=x&id=y&e=z",
        "https://us.list-manage.com/LOXN2jxMbBF?e=x&c2id=y&m=z",
    ):
        assert nr.extract_links(f'<a href="{url}">Tracked role</a>')[0]["external_id"] == "777"


def test_reconcile_matches_stable_ids_and_caches(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(nr, "lookup_source_urls", lambda sources, ids: {"123": "x"})
    monkeypatch.setattr(
        nr, "record_source_observations", lambda *args: calls.append(("obs", args)) or True
    )
    monkeypatch.setattr(
        nr, "record_source_run", lambda *args, **kwargs: calls.append(("run", args, kwargs)) or True
    )

    class Exec:
        def execute(self):
            import base64

            data = (
                base64.urlsafe_b64encode(
                    b'<a href="https://jobs.80000hours.org/?jobPk=123">A</a><a href="https://jobs.80000hours.org/?jobPk=999">B</a>'
                )
                .decode()
                .rstrip("=")
            )
            return {"payload": {"mimeType": "text/html", "body": {"data": data}}}

    class Messages:
        def get(self, **kwargs):
            return Exec()

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    meta = {
        "id": "abc123",
        "from": "80,000 Hours <hello@80000hours.org>",
        "subject": "10 new roles",
    }
    result = nr.reconcile_message(meta, Service(), tmp_path / "state.json")
    assert result["matched"] == 1 and result["unverified"] == 1
    assert (
        json.loads((tmp_path / "state.json").read_text())["messages"]["abc123"]["status"]
        == "complete"
    )
    assert len(calls) == 2
    monkeypatch.setattr(nr, "lookup_source_urls", lambda sources, ids: {"123": "x", "999": "y"})
    monkeypatch.setattr(
        nr, "_body", lambda *a: (_ for _ in ()).throw(AssertionError("must reuse links"))
    )
    assert nr.reconcile_message(meta, Service(), tmp_path / "state.json")["unverified"] == 0
    assert len(calls) == 4


def test_unknown_sender_is_ignored():
    assert not nr.is_newsletter({"from": "hello@evil.example", "subject": "10 new roles"})


def test_zero_links_are_partial_and_cache_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(nr, "record_source_observations", lambda *args: True)
    runs = []
    monkeypatch.setattr(
        nr, "record_source_run", lambda *args, **kwargs: runs.append(kwargs) or True
    )
    monkeypatch.setattr(nr, "_body", lambda service, message_id: "<p>No jobs here</p>")
    state = tmp_path / "state.json"
    result = nr.reconcile_message(
        {"id": "empty", "from": "hello@80000hours.org", "subject": "10 new roles"}, object(), state
    )
    assert result["status"] == "partial" and runs[-1]["complete"] is False
    assert os.stat(state).st_mode & 0o777 == 0o600
