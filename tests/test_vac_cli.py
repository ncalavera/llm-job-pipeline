"""Tests for the vac.py CLI status transitions — the /jobs-apply contract.

The apply / triage runbooks (`/jobs-apply`, `/jobs-vac`) lean on
``vac.py mark <id> <status>`` to move a vacancy between states. These tests lock
that contract: seed a vacancy, run mark through liked → passed → unseen, and
assert each status persists and round-trips through the DAL on a fresh read.

Drives vac.py's command handlers in-process (cmd_mark / cmd_list) against an
isolated temp SQLite DB. Fully offline.
"""

import importlib
import sys
import types

import pytest


@pytest.fixture()
def vac(tmp_path, monkeypatch):
    """SQLite-backed DAL + a fresh vac module bound to a temp DB."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))

    for mod in (
        "vac",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "geo",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import database_supabase as dal
    import vac as vac_mod

    importlib.reload(vac_mod)

    ns = type("VacEnv", (), {})()
    ns.dal = dal
    ns.mod = vac_mod
    yield ns
    dal.close_conn()


def _seed(dal, title="Head of Community"):
    dal.ensure_company("Acme Robotics", status="active")
    dal.save_vacancies(
        "Acme Robotics",
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
    for vid, v in dal.load_vacancies().items():
        if v["title"] == title:
            return vid
    raise AssertionError("seeded vacancy not found")


# ---------------------------------------------------------------------------
# mark — status transitions persist and round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["liked", "passed", "unseen", "to_apply", "applied"])
def test_mark_persists_each_status(vac, status):
    vid = _seed(vac.dal)
    vac.mod.cmd_mark(types.SimpleNamespace(id=vid, status=status))
    # Read back on a fresh status query — proves it persisted, not just cached.
    assert vac.dal.get_vacancy_statuses().get(vid, "unseen") == status


def test_mark_full_apply_flow_round_trip(vac):
    """The full liked → passed → unseen round-trip the apply runbook relies on."""
    vid = _seed(vac.dal)

    vac.mod.cmd_mark(types.SimpleNamespace(id=vid, status="liked"))
    assert vac.dal.get_vacancy_statuses()[vid] == "liked"

    vac.mod.cmd_mark(types.SimpleNamespace(id=vid, status="passed"))
    assert vac.dal.get_vacancy_statuses()[vid] == "passed"

    vac.mod.cmd_mark(types.SimpleNamespace(id=vid, status="unseen"))
    # unseen rows drop out of the status map entirely.
    assert vac.dal.get_vacancy_statuses().get(vid, "unseen") == "unseen"


def test_mark_accepts_uuid_prefix(vac):
    """mark resolves a short UUID prefix, like the CLI usage `vac mark abcd ...`."""
    vid = _seed(vac.dal)
    prefix = vid[:8]
    vac.mod.cmd_mark(types.SimpleNamespace(id=prefix, status="liked"))
    assert vac.dal.get_vacancy_statuses()[vid] == "liked"


def test_mark_invalid_status_exits_nonzero(vac):
    vid = _seed(vac.dal)
    with pytest.raises(SystemExit) as exc:
        vac.mod.cmd_mark(types.SimpleNamespace(id=vid, status="bogus"))
    assert exc.value.code == 1
    # Status unchanged.
    assert vac.dal.get_vacancy_statuses().get(vid, "unseen") == "unseen"


def test_mark_unknown_id_exits_nonzero(vac):
    _seed(vac.dal)
    with pytest.raises(SystemExit) as exc:
        vac.mod.cmd_mark(types.SimpleNamespace(id="ffffffff", status="liked"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Parser contract — the subcommands the runbooks call must exist.
# ---------------------------------------------------------------------------


def test_parser_exposes_mark_subcommand(vac):
    parser = vac.mod.build_parser()
    ns = parser.parse_args(["mark", "abcd1234", "liked"])
    assert ns.cmd == "mark"
    assert ns.id == "abcd1234"
    assert ns.status == "liked"


def test_valid_statuses_contract(vac):
    """The CLI's accepted statuses cover the apply workflow vocabulary."""
    assert {"unseen", "liked", "passed", "to_apply", "applied"} <= vac.mod.VALID_STATUSES
