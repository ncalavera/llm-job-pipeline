"""Tests for the vac.py CLI status transitions — the /jobs-review contract.

The apply / triage modes of the review hub (`/jobs-review apply`,
`/jobs-review vac`) lean on ``vac.py mark <id> <status>`` to move a vacancy
between states. These tests lock that contract: seed a vacancy, run mark
through liked → passed → unseen, and assert each status persists and
round-trips through the DAL on a fresh read.

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


@pytest.mark.parametrize("status", ["to_apply", "applied"])
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


# ---------------------------------------------------------------------------
# add — applications that never came from a job board
# ---------------------------------------------------------------------------
#
# Not every application is a job. A course, an incubation programme, a
# career-advising session — he sent those too, and until now there was no way
# to record one, so the count of "what I sent" was wrong by however many of
# them there were. They land as ordinary vacancy rows so every existing surface
# (the board, the Applications table, the company page) shows them with no
# special case.


@pytest.fixture()
def vm(tmp_path, monkeypatch):
    """The `vac` fixture above builds the frozen baseline only. `add` writes
    columns that arrive in migration 0022, so its tests need a database at the
    schema a real install actually runs."""
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
        "migrate",
    ):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as dal
    import vac as vac_mod

    importlib.reload(vac_mod)

    ns = type("VacEnv", (), {})()
    ns.dal = dal
    ns.mod = vac_mod
    yield ns
    dal.close_conn()


def _add_args(**over):
    fields = {
        "company": "80,000 Hours",
        "title": "1:1 career advising",
        "kind": "advising",
        "status": "declined",
        "applied_at": "2026-07-03",
        "status_at": "2026-07-06",
        "url": "https://80000hours.org/speak-with-us/",
        "note": "",
    }
    fields.update(over)
    return types.SimpleNamespace(**fields)


def _find(env, title):
    rows = env.dal.load_vacancies(include_candidate_companies=True, include_inactive_companies=True)
    for v in rows.values():
        if v["title"] == title:
            return v
    raise AssertionError(f"no vacancy titled {title!r}")


def test_add_records_a_non_job_application(vm):
    vm.mod.cmd_add(_add_args())
    v = _find(vm, "1:1 career advising")
    assert v["org"] == "80,000 Hours"
    assert v["kind"] == "advising"
    assert v["status"] == "declined"
    # 'manual', so the boards report never counts a hand-entered application
    # as some job board's yield.
    assert v["source_board"] == "manual"
    # Never scored, and never will be — nothing here came from the scorer.
    assert v["llm_score"] is None
    assert str(v["applied_at"]).startswith("2026-07-03")
    assert str(v["status_updated_at"]).startswith("2026-07-06")


def test_add_creates_the_company_when_it_is_new(vm):
    """A company he applied to through a side channel is not in the registry.
    Refusing to create it would make the CLI useless for its own use case."""
    assert vm.dal.resolve_company_id("Northwind Aid Trust") is None
    vm.mod.cmd_add(_add_args(company="Northwind Aid Trust", title="Fellowship", kind="programme"))
    assert vm.dal.resolve_company_id("Northwind Aid Trust") is not None


def test_add_puts_the_company_in_the_approved_set(vm):
    """Not 'candidate': he applied there, so it is not awaiting a review — and
    a candidate company's roles are hidden from the board."""
    vm.mod.cmd_add(_add_args(company="Rethink Priorities", title="Operations Generalist"))
    cid = vm.dal.resolve_company_id("Rethink Priorities")
    cur = vm.dal.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE id = %s", (cid,))
    status = cur.fetchone()[0]
    cur.close()
    assert status == "active"


def test_add_is_idempotent_on_company_and_title(vm):
    """The dedup hash comes from company + title, so fixing a typo is a re-run,
    not a cleanup. A second row would double-count the funnel."""
    vm.mod.cmd_add(_add_args())
    vm.mod.cmd_add(_add_args(status="accepted", note="Start date to confirm"))
    rows = [
        v
        for v in vm.dal.load_vacancies(include_candidate_companies=True).values()
        if v["title"] == "1:1 career advising"
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"


def test_add_stores_the_link_and_the_note(vm):
    vm.mod.cmd_add(_add_args(note="Chase them on Friday"))
    v = _find(vm, "1:1 career advising")
    assert v["locations"][0]["url"] == "https://80000hours.org/speak-with-us/"
    assert v["triage"]["note"] == "Chase them on Friday"


def test_add_defaults_to_a_job_and_invents_no_date(vm):
    """Without --applied-at the send date stays empty rather than being guessed
    as "today". The table then falls back to the stage date and MARKS the cell
    as an estimate — a guess that looks like a record is worse than a blank."""
    vm.mod.cmd_add(
        _add_args(kind="job", status="applied", applied_at=None, status_at=None, title="Analyst")
    )
    v = _find(vm, "Analyst")
    assert v["kind"] == "job"
    assert v["status"] == "applied"
    assert v["applied_at"] is None


@pytest.mark.parametrize("bad", ["internship", "JOB", ""])
def test_add_rejects_an_unknown_kind(vm, bad):
    """A closed vocabulary, enforced before the write. A typo that reached the
    database would show as a blank Kind column with nothing to explain it."""
    with pytest.raises(SystemExit) as exc:
        vm.mod.cmd_add(_add_args(kind=bad))
    assert exc.value.code == 1


def test_add_rejects_an_unknown_status(vm):
    with pytest.raises(SystemExit) as exc:
        vm.mod.cmd_add(_add_args(status="ghosted"))
    assert exc.value.code == 1


@pytest.mark.parametrize("bad", ["3 July 2026", "2026-13-01", "yesterday"])
def test_add_rejects_an_unparseable_date(vm, bad):
    """Refusing is the point: a silently-dropped date leaves the Applications
    table showing the wrong send date with no way to notice."""
    with pytest.raises(SystemExit) as exc:
        vm.mod.cmd_add(_add_args(applied_at=bad))
    assert exc.value.code == 1


@pytest.mark.parametrize("over", [{"company": "   "}, {"title": ""}])
def test_add_rejects_a_blank_company_or_title(vm, over):
    with pytest.raises(SystemExit) as exc:
        vm.mod.cmd_add(_add_args(**over))
    assert exc.value.code == 1


def test_added_application_reaches_the_dashboard_payload(vm):
    """The whole point: an unscored, hand-entered application must survive the
    dashboard's score floor. Before this it did not — keep_on_dashboard dropped
    every row with no score, so the Applications table would have been missing
    exactly the rows only this command can create."""
    from report.data_prep import keep_on_dashboard

    vm.mod.cmd_add(_add_args())
    assert keep_on_dashboard(_find(vm, "1:1 career advising")) is True


def test_add_on_a_pre_migration_database_says_what_to_run(vac):
    """A row written without `kind` and `applied_at` is half an application.
    The command stops and names the fix instead of writing that row."""
    with pytest.raises(SystemExit) as exc:
        vac.mod.cmd_add(_add_args())
    assert exc.value.code == 1


def test_parser_exposes_add_subcommand(vac):
    parser = vac.mod.build_parser()
    ns = parser.parse_args(
        [
            "add",
            "--company",
            "80,000 Hours",
            "--title",
            "1:1 career advising",
            "--kind",
            "advising",
            "--status",
            "declined",
            "--applied-at",
            "2026-07-03",
            "--status-at",
            "2026-07-06",
        ]
    )
    assert ns.cmd == "add"
    assert ns.company == "80,000 Hours"
    assert ns.kind == "advising"
    assert ns.applied_at == "2026-07-03"
    assert ns.status_at == "2026-07-06"
