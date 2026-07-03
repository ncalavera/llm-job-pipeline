"""Tests for scripts/dashboard_local.py — the local easy-mode API server.

The QA report exercised these endpoints by hand; this locks them down. We run
the real stdlib HTTPServer on an ephemeral port (port 0) in a background thread,
seed an isolated temp SQLite DB with a couple of companies/vacancies, and drive
the four JSON endpoints over real HTTP with urllib:

    GET  /api/statuses          — non-unseen/non-archived statuses
    GET  /api/company-statuses  — company status → review label map
    POST /api/save              — persist a vacancy status (like/pass)
    POST /api/company-review    — approve/reject a candidate company

Fully offline: SQLite backend, no Vercel, no Supabase, no real network beyond
localhost loopback.
"""

import importlib
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "sql" / "migrations"


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Boot dashboard_local.Handler on 127.0.0.1:0 against a temp SQLite DB.

    Yields ``(base_url, dal)`` and tears the server down cleanly after.
    """

    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    # Rebuild the backend chain so the DAL/db_conn singletons bind to the temp DB.
    for mod in (
        "dashboard_local",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "dashboard tests must run on SQLite"

    # TEST-ONLY: the production server is single-threaded, so its SQLite
    # connection (check_same_thread=True) always lives on the one serving
    # thread. This test seeds the DB from the main thread but drives the
    # endpoints from the server thread, so the singleton connection is touched
    # from two threads. Allow that here by forcing check_same_thread=False on
    # the connection db_backend opens. This does NOT change production behavior;
    # it only relaxes a same-thread assertion the test deliberately crosses.
    _orig_connect = db_backend.sqlite3.connect

    def _connect_shared(*args, **kwargs):
        kwargs.setdefault("check_same_thread", False)
        return _orig_connect(*args, **kwargs)

    monkeypatch.setattr(db_backend.sqlite3, "connect", _connect_shared)

    import database_supabase as dal
    import dashboard_local

    importlib.reload(dashboard_local)

    from http.server import HTTPServer

    httpd = HTTPServer(("127.0.0.1", 0), dashboard_local.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    try:
        yield base, dal
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        dal.close_conn()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(base, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _seed_vacancy(dal, org="Acme Robotics", title="Head of Community"):
    dal.ensure_company(org, status="active")
    dal.save_vacancies(
        org,
        "A",
        [
            {
                "title": title,
                "snippet": "Lead community efforts.",
                "full_description": "Lead our global community programme. " * 8,
                "location": "Berlin, Germany",
                "url": f"https://acme.example/job/{title.lower().replace(' ', '-')}",
            }
        ],
    )
    dal.get_conn().commit()
    # Return the id of the row we just seeded (matched by title), not merely the
    # first row — callers seed several vacancies and need each one's own id.
    for vid, v in dal.load_vacancies().items():
        if v["title"] == title:
            return vid
    raise AssertionError(f"seeded vacancy {title!r} not found")


# ---------------------------------------------------------------------------
# /api/health + /api/statuses
# ---------------------------------------------------------------------------


def test_health_ok(server):
    base, _ = server
    status, body = _get(base, "/api/health")
    assert status == 200
    assert body == {"ok": True}


def test_statuses_empty_db(server):
    base, _ = server
    status, body = _get(base, "/api/statuses")
    assert status == 200
    assert body == {"statuses": {}, "timestamps": {}}


def test_statuses_returns_decided_only(server):
    """/api/statuses returns non-unseen, non-archived statuses keyed by vid."""
    base, dal = server
    liked = _seed_vacancy(dal, title="Liked Role")
    unseen = _seed_vacancy(dal, title="Unseen Role")
    dal.update_vacancy_status(liked, "liked")
    dal.get_conn().commit()

    status, body = _get(base, "/api/statuses")
    assert status == 200
    assert body["statuses"].get(liked) == "liked"
    # Unseen rows are omitted from the map.
    assert unseen not in body["statuses"]


# ---------------------------------------------------------------------------
# /api/save — like/pass persistence
# ---------------------------------------------------------------------------


def test_save_persists_status(server):
    base, dal = server
    vid = _seed_vacancy(dal)

    status, body = _post(base, "/api/save", {"id": vid, "status": "liked"})
    assert status == 200
    assert body == {"ok": True}

    # Persisted to the DB (read on a fresh cursor through the DAL).
    assert dal.get_vacancy_statuses()[vid] == "liked"

    # And surfaced by /api/statuses.
    _, sbody = _get(base, "/api/statuses")
    assert sbody["statuses"][vid] == "liked"

    # A second save (pass) round-trips too.
    status, _ = _post(base, "/api/save", {"id": vid, "status": "passed"})
    assert status == 200
    assert dal.get_vacancy_statuses()[vid] == "passed"


def test_save_rejects_missing_fields(server):
    base, dal = server
    _seed_vacancy(dal)
    status, body = _post(base, "/api/save", {"id": "whatever"})
    assert status == 400
    assert "error" in body


def test_save_rejects_invalid_status(server):
    base, dal = server
    vid = _seed_vacancy(dal)
    status, body = _post(base, "/api/save", {"id": vid, "status": "bogus"})
    assert status == 400
    assert "Invalid status" in body["error"]


def test_save_unknown_vacancy_is_404(server):
    base, dal = server
    _seed_vacancy(dal)
    status, body = _post(
        base,
        "/api/save",
        {"id": "00000000-0000-0000-0000-000000000000", "status": "liked"},
    )
    assert status == 404
    assert "not found" in body["error"].lower()


# ---------------------------------------------------------------------------
# /api/company-statuses + /api/company-review
# ---------------------------------------------------------------------------


def test_company_statuses_maps_review_labels(server):
    """active→approved, candidate→pending, inactive→rejected."""
    base, dal = server
    active = dal.ensure_company("Active Co", status="active")
    candidate = dal.ensure_company("Candidate Co", status="candidate")
    inactive = dal.ensure_company("Inactive Co", status="inactive")
    dal.get_conn().commit()

    status, body = _get(base, "/api/company-statuses")
    assert status == 200
    statuses = body["statuses"]
    assert statuses[str(active)] == "approved"
    assert statuses[str(candidate)] == "pending"
    assert statuses[str(inactive)] == "rejected"


def test_company_review_approve_and_reject(server):
    base, dal = server
    cid = dal.ensure_company("Pending Co", status="candidate")
    dal.get_conn().commit()

    # Approve → company becomes active.
    status, body = _post(base, "/api/company-review", {"company_id": cid, "action": "approve"})
    assert status == 200
    assert body["ok"] is True
    assert dal.get_company_fitness_map()["Pending Co"]["status"] == "active"

    # Reject → company becomes inactive.
    status, body = _post(base, "/api/company-review", {"company_id": cid, "action": "reject"})
    assert status == 200
    assert dal.get_company_fitness_map()["Pending Co"]["status"] == "inactive"

    # And the review-label map reflects the new state.
    _, cs = _get(base, "/api/company-statuses")
    assert cs["statuses"][str(cid)] == "rejected"


def test_company_review_rejects_invalid_action(server):
    base, dal = server
    cid = dal.ensure_company("Pending Co", status="candidate")
    dal.get_conn().commit()
    status, body = _post(base, "/api/company-review", {"company_id": cid, "action": "explode"})
    assert status == 400
    assert "Invalid action" in body["error"]


def test_company_review_unknown_company_is_404(server):
    base, dal = server
    status, body = _post(
        base,
        "/api/company-review",
        {"company_id": "999999", "action": "approve"},
    )
    assert status == 404
    assert "not found" in body["error"].lower()


def test_unknown_post_route_is_404(server):
    base, _ = server
    status, body = _post(base, "/api/nope", {})
    assert status == 404


# ---------------------------------------------------------------------------
# /api/board-toggle — enable/disable a job board (writes board.enabled only)
# ---------------------------------------------------------------------------


def _ensure_board_schema(dal):
    """Bring the board table (0002) + enabled flag (0011) onto the temp DB —
    the shape a migrated install has. The frozen baseline predates both, and the
    server fixture doesn't apply migrations, so board tests set it up here."""
    conn = dal.get_conn()
    cur = conn.cursor()
    for m in ("0002_board_table", "0011_board_enabled"):
        cur.execute((MIGRATIONS / f"{m}.sqlite.sql").read_text(encoding="utf-8"))
    cur.close()
    conn.commit()


def test_board_toggle_enable_persists_and_commits(server):
    base, dal = server
    _ensure_board_schema(dal)
    dal.set_board_enabled("test_board", False)  # seed a known, disabled board
    assert dal.get_enabled_boards() == []

    status, body = _post(base, "/api/board-toggle", {"board_id": "test_board", "enabled": True})
    assert status == 200
    assert body["ok"] is True
    assert body["enabled"] is True
    assert dal.get_enabled_boards() == ["test_board"]

    # Committed — survives dropping the connection the endpoint wrote through.
    dal.close_conn()
    assert "test_board" in dal.get_enabled_boards()


def test_board_toggle_disable_clears_the_flag(server):
    base, dal = server
    _ensure_board_schema(dal)
    dal.set_board_enabled("test_board", True)
    assert dal.get_enabled_boards() == ["test_board"]

    status, body = _post(base, "/api/board-toggle", {"board_id": "test_board", "enabled": False})
    assert status == 200
    assert dal.get_enabled_boards() == []


def test_board_toggle_unknown_board_is_404(server):
    base, dal = server
    _ensure_board_schema(dal)
    status, body = _post(base, "/api/board-toggle", {"board_id": "no_such_board", "enabled": True})
    assert status == 404
    assert "not found" in body["error"].lower()
    # Failed closed — nothing was written.
    assert dal.get_enabled_boards() == []


def test_board_toggle_rejects_missing_board_id(server):
    base, dal = server
    _ensure_board_schema(dal)
    status, body = _post(base, "/api/board-toggle", {"enabled": True})
    assert status == 400
    assert "board_id" in body["error"]


def test_board_toggle_rejects_non_boolean_enabled(server):
    base, dal = server
    _ensure_board_schema(dal)
    dal.set_board_enabled("test_board", False)
    # A non-boolean must be rejected BEFORE any write — even for a real board,
    # and the type-check runs before the existence check so it's 400 not 404.
    status, body = _post(base, "/api/board-toggle", {"board_id": "test_board", "enabled": "yes"})
    assert status == 400
    assert "enabled" in body["error"]
    assert dal.get_enabled_boards() == []
