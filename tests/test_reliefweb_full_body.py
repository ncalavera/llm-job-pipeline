"""ReliefWeb RSS carries the FULL posting body, not a
teaser — the fetcher used to keep only a 400-char snippet built from HTML
that still had the metadata divs glued to the front. This checks the fix:
full_description holds the real body (metadata stripped), description_source
is stamped 'feed', and org/location/deadline extraction is unaffected.
"""

from fetchers.boards import reliefweb

_RSS = """<?xml version="1.0"?>
<rss><channel>
<item>
<title>Test Role</title>
<link>https://reliefweb.int/job/4199305/test-role</link>
<description>
    &lt;div class="tag country"&gt;Country: Spain&lt;/div&gt;
    &lt;div class="tag source"&gt;Organization: Acme NGO&lt;/div&gt;
    &lt;div class="date closing"&gt;Closing date: 30 Oct 2026&lt;/div&gt;
    &lt;p&gt;&lt;strong&gt;Acme NGO&lt;/strong&gt; helps people.&lt;/p&gt;
    &lt;h2&gt;Main tasks&lt;/h2&gt;
    &lt;ul&gt;&lt;li&gt;Do the thing.&lt;/li&gt;&lt;/ul&gt;
</description>
</item>
</channel></rss>"""


class _FakeResp:
    content = _RSS.encode()


def test_full_body_kept_metadata_stripped_source_is_feed(monkeypatch):
    monkeypatch.setattr(reliefweb.http, "get", lambda *a, **k: _FakeResp())
    jobs = reliefweb.fetch_reliefweb_board({"name": "ReliefWeb", "url": "https://reliefweb.int"})

    assert len(jobs) == 1
    job = jobs[0]
    assert job["description_source"] == "feed"
    assert "Main tasks" in job["full_description"]
    assert "Do the thing" in job["full_description"]
    # Metadata already extracted into its own fields — not duplicated in the body.
    assert "Closing date" not in job["full_description"]
    assert job["org_override"] == "Acme NGO"
    assert job["location"] == "Spain"
    assert job["deadline"] == "30 Oct 2026"
