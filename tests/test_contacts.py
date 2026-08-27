"""Networking contacts — the importer, the identity rule, and the status funnel.

The sweep CSVs are written by hand and by agents, so the interesting cases are
all about messy input: a "?" where a handle was looked for and not found, the
same person listed twice, a status column that is empty because nobody recorded
one. The rule that matters most is identity: a contact is (name, group), so
re-importing a corrected file must land on the same rows rather than forking a
second copy of the list.

Offline, invented people, a temp SQLite database — never the maintainer's files.
"""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO_ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from statuses import CONTACT_STATUSES, VALID_CONTACT_CHANNELS  # noqa: E402

CSV_HEADER = (
    "name,name_local,city,role,org,why_matters,ea_forum,linkedin,telegram,x,"
    "github,site,email,last_active,suggested_channel,opener,source_urls,status"
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A contacts module bound to a fresh migrated SQLite database."""
    db_file = tmp_path / "jobsearch.db"
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DIRECT_URL", raising=False)
    monkeypatch.setenv("JOBSEARCH_DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_PIPELINE_DISABLE_DOTENV", "1")

    for mod in ("database_supabase", "config", "db_conn", "db_backend", "migrate", "contacts"):
        sys.modules.pop(mod, None)

    import db_backend

    importlib.reload(db_backend)
    assert db_backend.IS_SQLITE, "contacts tests must run on SQLite"

    import migrate

    importlib.reload(migrate)
    assert migrate.cmd_migrate(allow_destructive=True, do_backup=False) == 0

    import contacts

    importlib.reload(contacts)
    assert contacts.table_ready(), "migration 0024 did not create the contact table"
    return contacts


def write_csv(tmp_path, rows, name="sweep.csv"):
    path = tmp_path / name
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


# --- Pure helpers ---------------------------------------------------------


@pytest.mark.parametrize("marker", ["?", "-", "n/a", "none", "unknown", "  ", ""])
def test_a_cell_that_says_nothing_reads_as_empty(db, marker):
    """The sweeps write "?" when a field was searched for and not found.

    Storing the literal "?" would put a question mark on screen where a handle
    belongs, as if it were the handle.
    """
    assert db.clean_value(marker) == ""


def test_a_real_value_survives_cleaning(db):
    assert db.clean_value("  Tbilisi, Georgia  ") == "Tbilisi, Georgia"


def test_only_known_channels_are_extracted(db):
    """An unrelated column can never become a channel nothing can render."""
    row = {"linkedin": "https://x", "telegram": "@y", "favourite_colour": "blue"}
    assert db.extract_channels(row) == {"linkedin": "https://x", "telegram": "@y"}


def test_unknown_channel_values_are_dropped_not_stored(db):
    row = {"linkedin": "https://x", "telegram": "?", "email": ""}
    assert db.extract_channels(row) == {"linkedin": "https://x"}


def test_every_extracted_channel_is_in_the_shared_vocabulary(db):
    row = {c: f"value-{c}" for c in VALID_CONTACT_CHANNELS}
    assert set(db.extract_channels(row)) == set(VALID_CONTACT_CHANNELS)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("EA Russian", "ea-russian"),
        ("ea_russian", "ea-russian"),
        ("  EA   Russian  ", "ea-russian"),
        ("", "other"),
        ("?", "other"),
    ],
)
def test_group_names_normalise_to_one_form(db, raw, expected):
    """"EA Russian" and "ea-russian" are one list, not two — and identity is
    keyed on the group, so two spellings would silently split it."""
    assert db.normalise_group(raw) == expected


@pytest.mark.parametrize(
    "city,org,expected",
    [
        ("Tbilisi, Georgia", "", "ea-georgia"),
        ("", "EA Georgia (Republic of)", "ea-georgia"),
        ("Istanbul", "", "ea-turkey"),
        ("", "AI Safety Turkiye", "ea-turkey"),
        ("Berkeley, CA, USA", "Constellation", ""),
        ("", "", ""),
    ],
)
def test_the_region_is_read_off_the_city_and_org(db, city, org, expected):
    assert db.derive_region_group({"city": city, "org": org}) == expected


def test_an_explicit_group_column_beats_the_derived_region(db):
    """The explicit always wins over the inferred."""
    row = {"name": "X", "city": "Tbilisi", "group": "yandex-referees"}
    contact = db.contact_from_csv_row(row, group="ea-russian", derive_region=True)
    assert contact["group"] == "yandex-referees"


def test_the_derived_region_beats_the_importer_default(db):
    row = {"name": "X", "city": "Istanbul"}
    contact = db.contact_from_csv_row(row, group="ea-russian", derive_region=True)
    assert contact["group"] == "ea-turkey"


def test_without_derivation_the_importer_default_is_used(db):
    row = {"name": "X", "city": "Istanbul"}
    contact = db.contact_from_csv_row(row, group="ea-russian", derive_region=False)
    assert contact["group"] == "ea-russian"


def test_a_row_with_no_name_is_skipped_not_stored(db):
    """A blank trailing line would otherwise become a contact called "" that can
    never be matched again."""
    assert db.contact_from_csv_row({"name": "  "}) is None


def test_an_unrecognised_status_falls_back_to_planned(db):
    """'planned' is the honest default: they are on the list, nothing was sent."""
    contact = db.contact_from_csv_row({"name": "X", "status": "thinking about it"})
    assert contact["status"] == "planned"


def test_the_suggested_channel_and_sources_are_kept_as_notes(db):
    contact = db.contact_from_csv_row(
        {"name": "X", "suggested_channel": "DM on the Forum", "source_urls": "https://a"}
    )
    assert "DM on the Forum" in contact["notes"]
    assert "https://a" in contact["notes"]


# --- Storage --------------------------------------------------------------


def test_a_contact_round_trips_with_its_channels(db):
    db.upsert_contact(
        {
            "name": "Ada Lovelace",
            "group": "ea-georgia",
            "channels": {"linkedin": "https://x", "email": "a@b.c"},
            "status": "planned",
        }
    )
    rows = db.list_contacts()
    assert len(rows) == 1
    assert rows[0]["channels"] == {"linkedin": "https://x", "email": "a@b.c"}


def test_identity_is_name_plus_group_not_name_alone(db):
    """The same person can be on two lists for two different reasons, with two
    different openers and two different states."""
    db.upsert_contact({"name": "Ada", "group": "ea-forum-open", "opener": "About ops"})
    db.upsert_contact({"name": "Ada", "group": "yandex-referees", "opener": "About the reference"})
    rows = db.list_contacts()
    assert len(rows) == 2
    assert {r["group"] for r in rows} == {"ea-forum-open", "yandex-referees"}


def test_re_importing_the_same_person_updates_rather_than_forks(db):
    db.upsert_contact({"name": "Ada", "group": "ea-georgia", "org": "Old Org"})
    db.upsert_contact({"name": "Ada", "group": "ea-georgia", "org": "New Org"})
    rows = db.list_contacts()
    assert len(rows) == 1
    assert rows[0]["org"] == "New Org"


def test_a_re_import_does_not_undo_a_status_set_in_the_ui(db):
    """The sweep CSVs carry no status. Re-importing one after a reply must not
    reset everybody to 'planned' — that would erase the only record of work."""
    contact_id = db.upsert_contact({"name": "Ada", "group": "ea-georgia"})
    db.set_contact_status(contact_id, "replied")

    # A row from the file, with an empty status column.
    db.upsert_contact({"name": "Ada", "group": "ea-georgia", "org": "Updated", "status": ""})

    rows = db.list_contacts()
    assert rows[0]["status"] == "replied", "the re-import reset a status it did not know"
    assert rows[0]["org"] == "Updated", "the re-import should still update the other fields"


def test_an_import_that_states_a_status_does_set_it(db):
    db.upsert_contact({"name": "Ada", "group": "network-2026-07"})
    db.upsert_contact({"name": "Ada", "group": "network-2026-07", "status": "contacted"})
    assert db.list_contacts()[0]["status"] == "contacted"


def test_a_contact_needs_a_name(db):
    with pytest.raises(ValueError):
        db.upsert_contact({"name": "", "group": "other"})


def test_an_unknown_status_is_refused_at_the_write(db):
    with pytest.raises(ValueError):
        db.upsert_contact({"name": "Ada", "group": "other", "status": "ghosted"})


@pytest.mark.parametrize("status", CONTACT_STATUSES)
def test_every_status_in_the_vocabulary_can_be_stored(db, status):
    """The Python tuple, the SQL CHECK and the dashboard's copy must agree; a
    status the CHECK rejects would fail only in production."""
    db.upsert_contact({"name": f"Person {status}", "group": "other", "status": status})
    stored = {r["name"]: r["status"] for r in db.list_contacts()}
    assert stored[f"Person {status}"] == status


def test_setting_a_status_stamps_when_it_moved(db):
    contact_id = db.upsert_contact({"name": "Ada", "group": "other"})
    assert db.list_contacts()[0]["status_at"] is None
    assert db.set_contact_status(contact_id, "contacted") is True
    assert db.list_contacts()[0]["status_at"] is not None


def test_setting_the_status_of_a_missing_contact_reports_it(db):
    """So a caller can answer 404 rather than report a write that changed
    nothing."""
    assert db.set_contact_status("00000000-0000-0000-0000-000000000000", "met") is False


def test_untouched_contacts_sort_last(db):
    """The list answers "what moved, and what has not", so a queue belongs at
    the bottom and the most recent movement at the top."""
    db.upsert_contact({"name": "Never touched", "group": "other"})
    moved = db.upsert_contact({"name": "Moved", "group": "other"})
    db.set_contact_status(moved, "replied")

    names = [r["name"] for r in db.list_contacts()]
    assert names[0] == "Moved"
    assert names[-1] == "Never touched"


def test_filters_narrow_by_status_and_by_group(db):
    db.upsert_contact({"name": "A", "group": "ea-georgia", "status": "planned"})
    db.upsert_contact({"name": "B", "group": "ea-turkey", "status": "contacted"})
    assert len(db.list_contacts(group="ea-georgia")) == 1
    assert len(db.list_contacts(status="contacted")) == 1
    assert len(db.list_contacts()) == 2


def test_counts_keep_their_zeroes(db):
    counts = db.count_by_status([{"status": "planned"}])
    assert counts["planned"] == 1
    assert counts["replied"] == 0
    assert set(counts) == set(CONTACT_STATUSES)


# --- The CSV importer -----------------------------------------------------


def test_import_reads_a_sweep_csv(db, tmp_path):
    path = write_csv(
        tmp_path,
        [
            "Ada Lovelace,,Tbilisi,Ops lead,Engines,Ran the only one,?,https://li/ada,?,?,?,?,?,2026-08-05 post,DM,Hello Ada,https://src,",
            "Bilge Kaan,,Istanbul,Organiser,EA Turkiye,Runs the local group,?,?,@bilge,?,?,?,?,,DM,Hello Bilge,,",
        ],
    )
    result = db.import_csv(path, group="ea-russian", derive_region=True)
    assert result == {"added": 2, "updated": 0, "skipped": 0}

    rows = {r["name"]: r for r in db.list_contacts()}
    assert rows["Ada Lovelace"]["group"] == "ea-georgia"
    assert rows["Bilge Kaan"]["group"] == "ea-turkey"
    assert rows["Ada Lovelace"]["channels"] == {"linkedin": "https://li/ada"}
    assert rows["Bilge Kaan"]["channels"] == {"telegram": "@bilge"}


def test_re_importing_a_corrected_file_updates_the_same_rows(db, tmp_path):
    rows = ["Ada,,Tbilisi,Ops,Engines,Reason,?,?,?,?,?,?,?,,,Hello,,"]
    path = write_csv(tmp_path, rows)
    assert db.import_csv(path)["added"] == 1

    fixed = ["Ada,,Tbilisi,Head of Ops,Engines,Reason,?,?,?,?,?,?,?,,,Hello,,"]
    path2 = write_csv(tmp_path, fixed, name="sweep2.csv")
    result = db.import_csv(path2)

    assert result == {"added": 0, "updated": 1, "skipped": 0}
    assert db.list_contacts()[0]["role"] == "Head of Ops"


def test_a_person_listed_twice_in_one_file_counts_once(db, tmp_path):
    """A duplicate is a mistake in the file, not two people. The later row wins,
    because that is the one the author edited last."""
    path = write_csv(
        tmp_path,
        [
            "Ada,,Tbilisi,Ops,Engines,First,?,?,?,?,?,?,?,,,Hello,,",
            "Ada,,Tbilisi,Ops,Engines,Second,?,?,?,?,?,?,?,,,Hello,,",
        ],
    )
    result = db.import_csv(path)
    assert result["added"] == 1
    assert db.list_contacts()[0]["why_matters"] == "Second"


def test_nameless_rows_are_counted_as_skipped(db, tmp_path):
    path = write_csv(
        tmp_path,
        [
            "Ada,,Tbilisi,Ops,Engines,Reason,?,?,?,?,?,?,?,,,Hello,,",
            ",,,,,,,,,,,,,,,,,",
        ],
    )
    result = db.import_csv(path)
    assert result["added"] == 1
    assert result["skipped"] == 1


def test_import_records_where_the_rows_came_from(db, tmp_path):
    path = write_csv(tmp_path, ["Ada,,Tbilisi,Ops,Engines,Reason,?,?,?,?,?,?,?,,,Hello,,"])
    db.import_csv(path, source_path="profiles/sweep.csv")
    assert db.list_contacts()[0]["source_path"] == "profiles/sweep.csv"


def test_a_csv_without_a_name_column_is_refused(db, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("org,city\nEngines,Tbilisi\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no 'name' column"):
        db.import_csv(path)


def test_a_missing_file_is_refused(db, tmp_path):
    with pytest.raises(FileNotFoundError):
        db.import_csv(tmp_path / "nope.csv")
