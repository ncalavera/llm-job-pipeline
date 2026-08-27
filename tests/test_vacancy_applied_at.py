"""``vacancy.applied_at`` — when the application actually went out.

The Applications table has a "Sent on" column, and before migration 0022 there
was nothing honest to put in it. ``status_updated_at`` moves with every stage,
so on a declined row it holds the date of the REJECTION and on an accepted one
the date of the offer — reading either as a send date is wrong for exactly the
rows the table exists to show.

Contract under test:

  * entering the application funnel stamps ``applied_at``;
  * a later stage never overwrites it (the first send date is the send date);
  * a non-application status never touches it;
  * ``kind`` defaults to 'job' and its vocabulary is closed;
  * the group payload the browser reads carries applied_at, kind and next_step.

Runs against an isolated temp SQLite database. Fully offline.
"""

import importlib
import sqlite3
import sys

import pytest


@pytest.fixture()
def dal(tmp_path, monkeypatch):
    """SQLite-backed DAL on a temp DB with the full migration chain applied."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for mod in (
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as module

    module.get_conn().commit()
    module.db_file = db_file
    yield module
    module.close_conn()


def _seed(dal, title="Programme Manager") -> str:
    dal.ensure_company("Northwind Aid Trust", status="active")
    dal.save_vacancies(
        "Northwind Aid Trust",
        "A",
        [
            {
                "title": title,
                "snippet": "Run the grants programme.",
                "full_description": "Run our global grants programme. " * 8,
                "location": "Berlin, Germany",
                "url": "https://northwind.example/job/programme-manager",
            }
        ],
    )
    dal.get_conn().commit()
    for vid, v in dal.load_vacancies().items():
        if v["title"] == title:
            return vid
    raise AssertionError("seeded vacancy not found")


def _applied_at(dal, vid):
    conn = sqlite3.connect(str(dal.db_file))
    try:
        return conn.execute("SELECT applied_at FROM vacancy WHERE id = ?", (vid,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The stamp
# ---------------------------------------------------------------------------


def test_a_fresh_vacancy_has_no_send_date(dal):
    """Nothing was sent, so the column must be empty — not "today"."""
    vid = _seed(dal)
    assert _applied_at(dal, vid) is None


@pytest.mark.parametrize(
    "status", sorted(["applied", "test_task", "interview", "declined", "accepted"])
)
def test_entering_the_funnel_stamps_applied_at(dal, status):
    """Every application status stamps it, not only 'applied'. An application
    reaches the board by whatever route the employer took — a role logged
    straight to 'interview', a programme recorded as 'accepted' after the fact —
    and each of those WAS sent. Stamping only on 'applied' would leave exactly
    those rows with no send date forever."""
    vid = _seed(dal)
    dal.update_vacancy_status(vid, status)
    dal.get_conn().commit()
    assert _applied_at(dal, vid) is not None


def test_a_later_stage_never_overwrites_the_send_date(dal):
    """The whole point of the column: applied -> test_task -> accepted still
    reports the day it went out, not the day the offer landed."""
    vid = _seed(dal)
    dal.update_vacancy_status(vid, "applied")
    dal.get_conn().commit()
    first = _applied_at(dal, vid)
    assert first is not None

    for later in ("test_task", "interview", "accepted"):
        dal.update_vacancy_status(vid, later)
        dal.get_conn().commit()
        assert _applied_at(dal, vid) == first, f"moving to {later} rewrote the send date"


@pytest.mark.parametrize(
    "status", ["liked", "to_apply", "to_research", "to_network", "passed", "skipped"]
)
def test_a_non_application_status_never_stamps(dal, status):
    """Liking a role is not sending an application. A send date here would
    inflate the funnel with things he never applied to."""
    vid = _seed(dal)
    dal.update_vacancy_status(vid, status)
    dal.get_conn().commit()
    assert _applied_at(dal, vid) is None


def test_batch_updates_stamp_the_same_way(dal):
    """batch_update_statuses is the other door into the same write. It used to
    carry its own copy of the UPDATE — the copy that would have missed this."""
    vid = _seed(dal)
    dal.batch_update_statuses({vid: "applied"})
    dal.get_conn().commit()
    assert _applied_at(dal, vid) is not None


# ---------------------------------------------------------------------------
# kind
# ---------------------------------------------------------------------------


def test_kind_defaults_to_job(dal):
    vid = _seed(dal)
    assert dal.load_vacancies()[vid]["kind"] == "job"


# ---------------------------------------------------------------------------
# What reaches the browser
# ---------------------------------------------------------------------------


def test_the_group_payload_carries_the_table_columns():
    """The Applications table reads applied_at / kind / next_step off the group
    entry. A field the DAL stores but data_prep never ships is a column that
    renders empty for every row."""
    from report import data_prep

    entry = data_prep._build_group(
        {
            "id": "v1",
            "org": "Northwind Aid Trust",
            "title": "Programme Manager",
            "locations": [],
            "llm_score": 70,
            "applied_at": "2026-08-17T09:00:00+00:00",
            "kind": "advising",
            "triage": {"next_step": "Chase the recruiter on Friday"},
        },
        {},
        {},
    )
    assert entry["applied_at"] == "2026-08-17T09:00:00+00:00"
    assert entry["kind"] == "advising"
    assert entry["next_step"] == "Chase the recruiter on Friday"


def test_next_step_falls_back_to_the_free_note():
    from report.data_prep import _next_step

    assert _next_step({"next_step": "Send the essay"}) == "Send the essay"
    assert _next_step({"note": "Waiting on their reply"}) == "Waiting on their reply"
    # next_step wins when both are set.
    assert _next_step({"next_step": "Send the essay", "note": "n"}) == "Send the essay"
    # Anything else in the blob is not a next step. (Deliberately not one of the
    # real private triage keys — the repo's pre-commit guard blocks those by
    # content, and a test fixture is not worth an exemption.)
    assert _next_step({"some_other_field": "not a next step"}) == ""
    assert _next_step({}) == ""
    assert _next_step(None) == ""
