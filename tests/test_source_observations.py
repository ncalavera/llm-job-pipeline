import json


def test_source_observations_keep_excluded_rows_and_lookup_url(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "jobs.sqlite"))
    from db_conn import close_conn

    close_conn()
    from source_observations import (
        lookup_source_url,
        record_source_observations,
        record_source_run,
    )

    record_source_observations(
        "run-1",
        "80k_hours",
        "https://jobs.80000hours.org",
        [
            {
                "external_id": "a",
                "title": "Researcher",
                "url": "https://x/a",
                "outcome": "accepted",
            },
            {
                "external_id": "b",
                "title": "Talent pool",
                "url": "https://x/b",
                "outcome": "excluded",
                "reason": "blacklist",
            },
        ],
    )

    assert lookup_source_url("80k_hours", "b") == "https://x/b"
    from db_conn import get_conn

    cur = get_conn().cursor()
    cur.execute(
        "SELECT outcome, reason FROM source_observation WHERE run_id = %s AND external_id = %s",
        ("run-1", "b"),
    )
    row = cur.fetchone()
    assert tuple(row) == ("excluded", "blacklist")
    record_source_run(
        "run-1",
        "80k_hours",
        "https://jobs.80000hours.org",
        raw_count=2,
        accepted_count=1,
        excluded_count=1,
        complete=True,
    )
    cur.execute(
        "SELECT raw_count, accepted_count, excluded_count, complete "
        "FROM source_fetch_run WHERE run_id = %s",
        ("run-1",),
    )
    assert tuple(cur.fetchone()) == (2, 1, 1, 1)


def test_algolia_later_page_failure_is_incomplete(monkeypatch):
    import fetchers
    from fetchers import fetch_algolia_board, get_fetch_errors

    class Response:
        def __init__(self, page):
            self.page = page

        def json(self):
            if self.page == 0:
                return {
                    "hits": [
                        {"objectID": "a", "title": "Researcher", "url_external": "https://x/a"}
                    ],
                    "nbPages": 2,
                }
            raise RuntimeError("page down")

        def raise_for_status(self):
            return None

    class HTTP:
        post = lambda self, *a, **kw: Response(json.loads(kw["data"])["page"])

    monkeypatch.setattr(fetchers, "requests", HTTP())
    # The adapter emits a useful partial result but marks the source incomplete.
    out = fetch_algolia_board(
        {
            "name": "80k",
            "url": "https://x",
            "algolia_app_id": "a",
            "algolia_api_key": "k",
            "algolia_index": "i",
        }
    )
    assert len(out) == 1
    assert "incomplete" in get_fetch_errors()["80k"]


def test_algolia_retains_blacklisted_listing_for_inbox(monkeypatch):
    import fetchers
    from fetchers import fetch_algolia_board

    class Response:
        def json(self):
            return {
                "hits": [{"objectID": "x", "title": "Talent pool", "url_external": "https://x/x"}],
                "nbPages": 1,
            }

        def raise_for_status(self):
            return None

    class HTTP:
        post = lambda self, *a, **kw: Response()

    monkeypatch.setattr(fetchers, "requests", HTTP())
    out = fetch_algolia_board(
        {
            "name": "80k",
            "url": "https://x",
            "algolia_app_id": "a",
            "algolia_api_key": "k",
            "algolia_index": "i",
        }
    )
    assert out and out[0]["external_id"] == "x"


def test_import_outcome_commits_with_the_vacancy(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(tmp_path / "intake.sqlite"))
    from db_conn import close_conn, get_conn

    close_conn()
    from source_observations import record_source_observations
    import database_supabase as db

    job = {
        "title": "Research Coordinator",
        "org_override": "Test Org",
        "external_id": "job1",
        "url": "https://example.org/job1",
        "preserve_listing": True,
        "_source_run": "r1",
        "_source_key": "board",
        "full_description": "Coordinate the research programme and manage the project timetable.",
    }
    assert record_source_observations(
        "r1",
        "board",
        "https://example.org",
        [{"external_id": "job1", "title": job["title"], "url": job["url"], "outcome": "observed"}],
    )
    db.save_board_vacancies({"name": "Board", "url": "https://example.org", "tier": "B"}, [job])
    cur = get_conn().cursor()
    cur.execute("SELECT outcome, canonical_id FROM source_observation WHERE external_id = 'job1'")
    outcome, canonical_id = cur.fetchone()
    assert outcome == "new" and canonical_id
    get_conn().rollback()
    cur.execute("SELECT outcome, canonical_id FROM source_observation WHERE external_id = 'job1'")
    assert tuple(cur.fetchone()) == ("observed", None)
    close_conn()
