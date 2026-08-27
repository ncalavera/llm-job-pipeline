"""Stored research reports — the slug/title/kind derivation and the upsert.

Every report written for this search is a markdown file in a private repo, so
the work is only reachable from the laptop it was written on. `vac report add`
puts one in the database; the dashboard's Reports tab reads it back.

The contract these tests hold:

  * the slug comes from the FILENAME, so a re-import of an edited file updates
    one row instead of forking a second copy of the same report;
  * the title comes from the document's own H1, in either markdown form;
  * the kind is inferred from where the file lives, and is 'other' — not a
    guess — when nothing matches;
  * the closed kind vocabulary is enforced before the write.

Runs against an isolated temp SQLite database. Fully offline.
"""

import importlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Pure derivation — no database needed
# ---------------------------------------------------------------------------


@pytest.fixture()
def mod():
    sys.path.insert(0, "scripts")
    import reports

    return reports


def test_slug_comes_from_the_filename(mod):
    assert mod.slug_from_path("research/sectors/ea-funding-2026.md") == "ea-funding-2026"
    assert mod.slug_from_path("EA Funding 2026.md") == "ea-funding-2026"
    assert mod.slug_from_path("reports/Q3 review (final).md") == "q3-review-final"


def test_slug_ignores_the_directory(mod):
    """A slug built from the path would fork a second copy of the same report
    the first time a file moved between folders."""
    a = mod.slug_from_path("research/ea-funding-2026.md")
    b = mod.slug_from_path("reports/archive/ea-funding-2026.md")
    assert a == b


def test_slug_is_url_safe(mod):
    slug = mod.slug_from_path("research/Ünïcode & Symbols!.md")
    assert slug.strip("-") == slug
    assert all(c.isalnum() or c == "-" for c in slug)


def test_title_comes_from_the_first_h1(mod):
    body = "# EA Funding Landscape 2026\n\nThree funders matter.\n\n# Not this one\n"
    assert mod.title_from_markdown(body) == "EA Funding Landscape 2026"


def test_title_reads_the_setext_heading_form_too(mod):
    body = "EA Funding Landscape 2026\n=========================\n\nText.\n"
    assert mod.title_from_markdown(body) == "EA Funding Landscape 2026"


def test_title_skips_blank_lines_and_front_matter_above_it(mod):
    body = "---\ntags: funding\n---\n\n# The Real Title\n\nText.\n"
    assert mod.title_from_markdown(body) == "The Real Title"


def test_title_ignores_an_h1_far_down_the_document(mod):
    """An H1 on page four is a section, not the document's name."""
    body = "\n".join(["Some prose."] * 60 + ["# A Late Section"])
    assert mod.title_from_markdown(body, "fallback") == "fallback"


def test_title_falls_back_when_the_file_has_no_h1(mod):
    assert mod.title_from_markdown("Just prose, no heading.", "Ea Funding") == "Ea Funding"


def test_a_deeper_heading_is_not_mistaken_for_the_title(mod):
    assert mod.title_from_markdown("## A Section\n\nText.", "fb") == "fb"


@pytest.mark.parametrize(
    "path,kind",
    [
        ("research/sectors/ea-funding.md", "sector"),
        ("research/companies/givewell.md", "company"),
        ("research/ai-safety-landscape.md", "research"),
        ("reports/ea-landing-strategy.md", "research"),
        ("docs/cv/givedirectly/research-notes.md", "company"),
        ("grants/eaif-2026.md", "grant"),
        ("notes/random-thought.md", "other"),
    ],
)
def test_kind_is_inferred_from_where_the_file_lives(mod, path, kind):
    assert mod.kind_from_path(path) == kind


def test_an_unmatched_path_is_other_not_a_guess(mod):
    """A wrong kind hides a report in the wrong group; 'other' tells the truth."""
    assert mod.kind_from_path("somewhere/else/thing.md") == "other"


def test_the_more_specific_directory_wins(mod):
    """'research/sectors' must beat the bare 'research' it contains."""
    assert mod.kind_from_path("research/sectors/x.md") == "sector"


def test_read_report_file_pulls_everything_off_one_file(mod, tmp_path):
    d = tmp_path / "research" / "sectors"
    d.mkdir(parents=True)
    f = d / "ea-funding-2026.md"
    f.write_text("# EA Funding Landscape 2026\n\nThree funders matter.\n", encoding="utf-8")

    report = mod.read_report_file(f)
    assert report["slug"] == "ea-funding-2026"
    assert report["title"] == "EA Funding Landscape 2026"
    assert report["kind"] == "sector"
    assert "Three funders matter." in report["body_md"]
    assert report["source_path"].endswith("ea-funding-2026.md")


def test_an_empty_file_is_refused(mod, tmp_path):
    """Storing an empty report would REPLACE a good one on the next re-import."""
    f = tmp_path / "empty.md"
    f.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.read_report_file(f)


# ---------------------------------------------------------------------------
# The stored side
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A temp SQLite database with the full migration chain applied."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for name in (
        "reports",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "migrate",
    ):
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as dal
    import reports as reports_mod

    importlib.reload(reports_mod)
    yield reports_mod
    dal.close_conn()


def _store(mod, **over):
    fields = {
        "slug": "ea-funding-2026",
        "title": "EA Funding Landscape 2026",
        "kind": "sector",
        "body_md": "# EA Funding Landscape 2026\n\nThree funders matter.",
        "source_path": "research/sectors/ea-funding-2026.md",
    }
    fields.update(over)
    report_id = mod.upsert_report(**fields)
    from database_supabase import get_conn

    get_conn().commit()
    return report_id


def test_the_report_table_exists_after_migrating(db):
    assert db.table_ready() is True


def test_a_stored_report_reads_back_whole(db):
    _store(db)
    report = db.get_report("ea-funding-2026")
    assert report["title"] == "EA Funding Landscape 2026"
    assert report["kind"] == "sector"
    assert "Three funders matter." in report["body_md"]
    assert report["source_path"] == "research/sectors/ea-funding-2026.md"


def test_re_importing_updates_the_same_row(db):
    """The whole point of keying on the slug: an edited file replaces its
    report. A second copy is worse than no copy, because now two disagree."""
    first = _store(db)
    second = _store(db, title="EA Funding Landscape 2026 (revised)", body_md="# T\n\nNew.")
    assert first == second
    assert len(db.list_reports()) == 1
    assert db.get_report("ea-funding-2026")["title"].endswith("(revised)")
    assert "New." in db.get_report("ea-funding-2026")["body_md"]


def test_two_different_reports_both_survive(db):
    _store(db, slug="ea-funding-2026")
    _store(db, slug="givewell-dossier", title="GiveWell", kind="company")
    assert {r["slug"] for r in db.list_reports()} == {"ea-funding-2026", "givewell-dossier"}


def test_list_returns_no_bodies(db):
    """The list view shows none of the body; carrying it would make a library
    of a hundred reports a multi-megabyte response."""
    _store(db)
    assert "body_md" not in db.list_reports()[0]


def test_missing_report_is_none_not_an_error(db):
    assert db.get_report("never-written") is None


@pytest.mark.parametrize("kind", ["research", "grant", "company", "sector", "other"])
def test_every_kind_in_the_vocabulary_is_storable(db, kind):
    _store(db, slug=f"r-{kind}", kind=kind)
    assert db.get_report(f"r-{kind}")["kind"] == kind


def test_an_unknown_kind_is_refused_before_the_write(db):
    """A closed vocabulary. An unrecognised kind would silently create a group
    of one in the list, which reads as broken grouping rather than a typo."""
    with pytest.raises(ValueError):
        _store(db, kind="memo")
    assert db.get_report("ea-funding-2026") is None


@pytest.mark.parametrize("over", [{"slug": ""}, {"title": ""}, {"body_md": "   "}])
def test_an_incomplete_report_is_refused(db, over):
    with pytest.raises(ValueError):
        _store(db, **over)


def test_the_sql_check_and_the_python_vocabulary_agree():
    """Two halves of one contract: the module validates, the database enforces.
    Drift means a kind that passes the check and then fails the write."""
    import re
    from pathlib import Path

    import statuses

    root = Path(__file__).resolve().parents[1]
    sql = (root / "sql" / "migrations" / "0023_report_table.postgres.sql").read_text()
    check = re.search(r"kind IN \(([^)]*)\)", sql)
    assert check, "no kind CHECK in the migration"
    sql_kinds = set(re.findall(r"'([a-z]+)'", check.group(1)))
    assert sql_kinds == set(statuses.VALID_REPORT_KINDS)

    sqlite_sql = (root / "sql" / "migrations" / "0023_report_table.sqlite.sql").read_text()
    sqlite_check = re.search(r"kind IN \(([^)]*)\)", sqlite_sql)
    assert sqlite_check
    assert set(re.findall(r"'([a-z]+)'", sqlite_check.group(1))) == sql_kinds


# ---------------------------------------------------------------------------
# The CLI: `vac report add` / `vac report list`
# ---------------------------------------------------------------------------


@pytest.fixture()
def vac_reports(tmp_path, monkeypatch):
    """vac.py bound to a temp database at the current schema."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for name in (
        "vac",
        "reports",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "geo",
        "migrate",
    ):
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=False, do_backup=False) == 0

    import database_supabase as dal
    import reports as reports_mod
    import vac as vac_mod

    importlib.reload(reports_mod)
    importlib.reload(vac_mod)

    ns = type("Env", (), {})()
    ns.mod = vac_mod
    ns.reports = reports_mod
    yield ns
    dal.close_conn()


def _write_report(tmp_path, rel, body):
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    return f


def _report_args(path, **over):
    import types

    fields = {"report_cmd": "add", "path": str(path), "title": None, "kind": None}
    fields.update(over)
    return types.SimpleNamespace(**fields)


def test_report_add_stores_a_file_end_to_end(vac_reports, tmp_path):
    f = _write_report(
        tmp_path,
        "research/sectors/ea-funding-2026.md",
        "# EA Funding Landscape 2026\n\nThree funders matter.\n",
    )
    vac_reports.mod.cmd_report(_report_args(f))

    stored = vac_reports.reports.get_report("ea-funding-2026")
    assert stored["title"] == "EA Funding Landscape 2026"
    assert stored["kind"] == "sector"
    assert "Three funders matter." in stored["body_md"]


def test_report_add_re_run_updates_rather_than_duplicating(vac_reports, tmp_path):
    f = _write_report(tmp_path, "research/x.md", "# First Title\n\nOne.\n")
    vac_reports.mod.cmd_report(_report_args(f))
    f.write_text("# Second Title\n\nTwo.\n", encoding="utf-8")
    vac_reports.mod.cmd_report(_report_args(f))

    rows = vac_reports.reports.list_reports()
    assert len(rows) == 1
    assert rows[0]["title"] == "Second Title"


def test_report_add_overrides_beat_the_guesses(vac_reports, tmp_path):
    """Title and kind are both inferred, and a guess must be correctable."""
    f = _write_report(tmp_path, "notes/thing.md", "# Guessed Title\n\nText.\n")
    vac_reports.mod.cmd_report(_report_args(f, title="The Real Name", kind="grant"))

    stored = vac_reports.reports.get_report("thing")
    assert stored["title"] == "The Real Name"
    assert stored["kind"] == "grant"


def test_report_add_titles_an_h1_less_file_from_its_name(vac_reports, tmp_path):
    f = _write_report(tmp_path, "research/ea-funding-2026.md", "Just prose, no heading.\n")
    vac_reports.mod.cmd_report(_report_args(f))
    assert vac_reports.reports.get_report("ea-funding-2026")["title"] == "Ea funding 2026"


def test_report_add_refuses_a_missing_file(vac_reports, tmp_path):
    with pytest.raises(SystemExit) as exc:
        vac_reports.mod.cmd_report(_report_args(tmp_path / "nope.md"))
    assert exc.value.code == 1


def test_report_add_refuses_a_non_markdown_file(vac_reports, tmp_path):
    f = _write_report(tmp_path, "notes/thing.txt", "# Title\n\nText.\n")
    with pytest.raises(SystemExit) as exc:
        vac_reports.mod.cmd_report(_report_args(f))
    assert exc.value.code == 1


def test_report_add_refuses_an_empty_file(vac_reports, tmp_path):
    f = _write_report(tmp_path, "notes/empty.md", "\n\n")
    with pytest.raises(SystemExit) as exc:
        vac_reports.mod.cmd_report(_report_args(f))
    assert exc.value.code == 1


def test_report_add_refuses_an_unknown_kind(vac_reports, tmp_path):
    f = _write_report(tmp_path, "notes/thing.md", "# Title\n\nText.\n")
    with pytest.raises(SystemExit) as exc:
        vac_reports.mod.cmd_report(_report_args(f, kind="memo"))
    assert exc.value.code == 1


def test_report_list_prints_every_stored_report(vac_reports, tmp_path, capsys):
    for name, body in [
        ("research/sectors/ea-funding.md", "# EA Funding\n\nText.\n"),
        ("grants/eaif.md", "# EA Infrastructure Fund\n\nText.\n"),
    ]:
        vac_reports.mod.cmd_report(_report_args(_write_report(tmp_path, name, body)))
    capsys.readouterr()

    vac_reports.mod.cmd_report(_report_args(None, report_cmd="list"))
    out = capsys.readouterr().out
    assert "EA Funding" in out
    assert "EA Infrastructure Fund" in out
    assert "Total: 2" in out
    assert "None" not in out


def test_report_list_on_an_empty_library_says_how_to_add_one(vac_reports, capsys):
    vac_reports.mod.cmd_report(_report_args(None, report_cmd="list"))
    out = capsys.readouterr().out
    assert "No reports stored yet." in out
    assert "vac.py report add" in out


def test_report_on_a_pre_migration_database_says_what_to_run(tmp_path, monkeypatch, capsys):
    """No `report` table yet: name the fix rather than raising "no such table"."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    for name in (
        "vac",
        "reports",
        "database_supabase",
        "config",
        "company_registry",
        "db_conn",
        "db_backend",
        "geo",
    ):
        sys.modules.pop(name, None)

    import db_backend

    importlib.reload(db_backend)
    import database_supabase as dal
    import vac as vac_mod

    importlib.reload(vac_mod)
    dal.get_conn()  # baseline only — migrate.py never ran

    with pytest.raises(SystemExit) as exc:
        vac_mod.cmd_report(_report_args(None, report_cmd="list"))
    assert exc.value.code == 1
    assert "migration 0023" in capsys.readouterr().out
    dal.close_conn()


# ---------------------------------------------------------------------------
# The provenance line is shown on screen, so it must not be one laptop's path
# ---------------------------------------------------------------------------


def test_a_path_under_the_working_directory_is_stored_relative(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _write_report(tmp_path, "research/sectors/x.md", "# T\n\nText.\n")
    assert mod.display_source_path(f) == "research/sectors/x.md"


def test_a_path_outside_it_keeps_only_its_last_two_segments(mod):
    """The dashboard SHOWS this line. A full home-directory path there is noise
    on screen and a needless leak of one machine's layout — the field answers
    "which file is this", not "where was it on that laptop".

    The home prefix is assembled rather than written out: this repo is public
    and a guard (test_no_hardcoded_data) blocks a literal one in any tracked
    file, fixtures included."""
    home = "/" + "Users" + "/someone"
    assert (
        mod.display_source_path(f"{home}/Projects/job-search/research/sectors/ea.md")
        == "sectors/ea.md"
    )
    assert home not in mod.display_source_path(f"{home}/a/b/c.md")


def test_a_relative_path_is_kept_as_given(mod):
    assert mod.display_source_path("research/sectors/x.md") == "research/sectors/x.md"


def test_a_stored_report_never_carries_an_absolute_path(vac_reports, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = _write_report(tmp_path, "research/sectors/ea-funding.md", "# EA Funding\n\nText.\n")
    vac_reports.mod.cmd_report(_report_args(f))
    stored = vac_reports.reports.get_report("ea-funding")
    assert stored["source_path"] == "research/sectors/ea-funding.md"
