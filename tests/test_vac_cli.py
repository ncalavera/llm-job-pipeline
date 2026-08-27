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

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from statuses import APPLICATION_STATUSES  # noqa: E402


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


def _company_status(env, name):
    cid = env.dal.resolve_company_id(name)
    cur = env.dal.get_conn().cursor()
    cur.execute("SELECT status FROM company WHERE id = %s", (cid,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def _visible_titles(env):
    """What the board and `vac list` show — the DEFAULT company filter."""
    rows = env.dal.load_vacancies()
    return {v["title"] for v in rows.values()}


# --- The Successif bug -----------------------------------------------------
# Found in production: he applied to a company already on file as 'inactive'.
# ensure_company only sets a status when it CREATES the row, so the company
# stayed inactive, the company filter hid the vacancy, and the application was
# invisible on the dashboard and in `vac list` — the funnel undercounting
# itself with nothing on screen to say a row was missing.


def test_add_reactivates_a_company_that_was_set_aside(vm):
    """Applying is a decision about the company, and it overrides an older one."""
    cid = vm.dal.ensure_company("Successif", status="active")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()
    assert _company_status(vm, "Successif") == "inactive"

    vm.mod.cmd_add(_add_args(company="Successif", title="Career advising"))

    assert _company_status(vm, "Successif") == "active"


def test_add_to_an_inactive_company_is_visible_on_the_board(vm):
    """The bug as the user met it: the row existed but nothing showed it."""
    cid = vm.dal.ensure_company("Successif", status="active")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()

    vm.mod.cmd_add(_add_args(company="Successif", title="Career advising"))

    assert "Career advising" in _visible_titles(vm)


def test_an_application_stays_visible_even_if_its_company_goes_inactive(vm):
    """The second half of the fix, and the one that does not depend on `vac add`.

    A company can be set aside AFTER the application was sent — by a later
    review, or by the auto-reject pass. The application must not vanish with
    it, whichever route made the company inactive.
    """
    vm.mod.cmd_add(_add_args(company="Successif", title="Career advising"))
    cid = vm.dal.resolve_company_id("Successif")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()

    assert "Career advising" in _visible_titles(vm)


@pytest.mark.parametrize("status", sorted(APPLICATION_STATUSES))
def test_every_application_status_survives_an_inactive_company(vm, status):
    """Not just 'applied': every stage of the funnel is a sent application."""
    vm.mod.cmd_add(_add_args(company="Successif", title="Career advising", status=status))
    cid = vm.dal.resolve_company_id("Successif")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()

    assert "Career advising" in _visible_titles(vm), f"{status} was hidden"


def test_a_non_application_row_is_still_hidden_by_an_inactive_company(vm):
    """The widening must not become "show everything".

    An unreviewed role at a company he set aside is exactly what the company
    filter is FOR. Only sent applications are exempt.
    """
    cid = vm.dal.ensure_company("Successif", status="active")
    vm.dal.upsert_vacancy(
        vm.dal.make_vacancy_id("Successif", "Unrelated open role"),
        {
            "company_id": cid,
            "title": "Unrelated open role",
            "status": "unseen",
            "first_seen": "2026-07-01",
            "last_seen": "2026-07-01",
        },
    )
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()

    assert "Unrelated open role" not in _visible_titles(vm)


def test_reactivating_records_why_the_status_changed(vm):
    """A silent status flip is discovered months later; this one leaves a reason."""
    cid = vm.dal.ensure_company("Successif", status="active")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status = 'inactive' WHERE id = %s", (cid,))
    cur.close()

    vm.mod.cmd_add(_add_args(company="Successif", title="Career advising"))

    cur = vm.dal.get_conn().cursor()
    cur.execute("SELECT status_reason FROM company WHERE id = %s", (cid,))
    reason = cur.fetchone()[0]
    cur.close()
    assert reason and "vac add" in reason


def test_add_leaves_an_already_active_company_alone(vm):
    """No pointless write, and no misleading status_reason on a company that
    never changed."""
    cid = vm.dal.ensure_company("Rethink Priorities", status="active")
    cur = vm.dal.get_conn().cursor()
    cur.execute("UPDATE company SET status_reason = 'approved by review' WHERE id = %s", (cid,))
    cur.close()

    vm.mod.cmd_add(_add_args(company="Rethink Priorities", title="Operations Generalist"))

    cur = vm.dal.get_conn().cursor()
    cur.execute("SELECT status, status_reason FROM company WHERE id = %s", (cid,))
    status, reason = cur.fetchone()
    cur.close()
    assert status == "active"
    assert reason == "approved by review", "an untouched company kept its own reason"


# --- vac publish ----------------------------------------------------------
# The dashboard reads a baked snapshot, not the tables, so anything that changes
# what the snapshot CONTAINS is invisible until it is rewritten. Before this,
# only a fetch or a scoring run did that — both of which cost time and LLM calls
# to produce a result neither of them needed.


def test_publish_rewrites_the_snapshot_without_fetching_or_scoring(vm, monkeypatch, tmp_path):
    """The whole point: the last step alone, no network and no scorer."""
    import report as report_mod

    monkeypatch.setattr(report_mod, "PUBLIC_DIR", tmp_path, raising=False)
    called = {"n": 0}
    real = report_mod.generate_dashboard

    def counting():
        called["n"] += 1
        return real()

    monkeypatch.setattr(report_mod, "generate_dashboard", counting)

    vm.mod.cmd_add(_add_args())
    vm.mod.cmd_publish(types.SimpleNamespace())

    assert called["n"] == 1


def test_publish_reports_what_the_snapshot_covers(vm, monkeypatch, tmp_path, capsys):
    """The counts are the confirmation that it read the tables it was meant to."""
    import report as report_mod

    monkeypatch.setattr(report_mod, "PUBLIC_DIR", tmp_path, raising=False)
    vm.mod.cmd_add(_add_args())
    vm.mod.cmd_publish(types.SimpleNamespace())

    out = capsys.readouterr().out
    assert "snapshot rewritten" in out
    assert "vacancy" in out


def test_publish_survives_a_database_without_the_optional_tables(vm, monkeypatch, tmp_path, capsys):
    """A database that has not migrated to `contact` yet still publishes.

    The count line is a convenience; failing the publish because one optional
    table is absent would make the command useless on exactly the databases
    most likely to need it.
    """
    import report as report_mod

    monkeypatch.setattr(report_mod, "PUBLIC_DIR", tmp_path, raising=False)
    cur = vm.dal.get_conn().cursor()
    cur.execute("DROP TABLE IF EXISTS contact")
    cur.close()
    vm.dal.get_conn().commit()

    vm.mod.cmd_publish(types.SimpleNamespace())
    assert "snapshot rewritten" in capsys.readouterr().out


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


# ---------------------------------------------------------------------------
# A row with no score is still fully operable
# ---------------------------------------------------------------------------
#
# Three rows already live in the production database with source_board='manual'
# and llm_score NULL (HIP Impact Accelerator, and the Coefficient Giving and
# EA Infrastructure Fund grants). Nothing in the status path may require a
# score: an application he already sent must be movable between stages whether
# or not a model ever looked at it.


def test_mark_moves_an_unscored_row_through_the_whole_funnel(vm):
    """No stage of `vac mark` reads llm_score. The regression this guards would
    be silent — a row that simply refuses to move, on the only rows that can
    never be scored."""
    vm.mod.cmd_add(_add_args(title="HIP Impact Accelerator", kind="programme", status="applied"))
    v = _find(vm, "HIP Impact Accelerator")
    assert v["llm_score"] is None

    vid = None
    for uid, row in vm.dal.load_vacancies(include_candidate_companies=True).items():
        if row["title"] == "HIP Impact Accelerator":
            vid = uid
    assert vid

    for status in ("test_task", "interview", "accepted", "declined"):
        vm.mod.cmd_mark(types.SimpleNamespace(id=vid, status=status))
        assert vm.dal.get_vacancy_statuses()[vid] == status
    # Still unscored after all that — marking must never invent a number.
    assert _find(vm, "HIP Impact Accelerator")["llm_score"] is None


def test_listing_an_unscored_row_prints_a_dash_not_none(vm, capsys):
    """`vac list` renders the score column itself. 'None' or a traceback there
    is the whole command broken for these rows."""
    vm.mod.cmd_add(_add_args(title="EA Infrastructure Fund grant", kind="grant"))
    capsys.readouterr()  # drop cmd_add's own confirmation line
    vm.mod.cmd_list(
        types.SimpleNamespace(
            status=None,
            min_score=None,
            tier=None,
            org=None,
            limit=None,
            sort="score",
            include_candidates=True,
            geo=None,
        )
    )
    out = capsys.readouterr().out
    assert "EA Infrastructure Fund grant" in out
    assert "None" not in out
    assert "nan" not in out.lower()


def test_min_score_filter_excludes_rather_than_crashes_on_an_unscored_row(vm, capsys):
    """`--min-score` compares against a number that may not exist. Excluding is
    correct (an unscored row cannot clear a floor); raising is not."""
    vm.mod.cmd_add(_add_args(title="Coefficient Giving grant", kind="grant"))
    capsys.readouterr()  # drop cmd_add's own confirmation line
    vm.mod.cmd_list(
        types.SimpleNamespace(
            status=None,
            min_score=40,
            tier=None,
            org=None,
            limit=None,
            sort="score",
            include_candidates=True,
            geo=None,
        )
    )
    out = capsys.readouterr().out
    assert "Coefficient Giving grant" not in out
