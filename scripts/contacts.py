"""Networking contacts — the data layer behind the dashboard's Networking tab.

The dashboard already tracks roles, the applications sent for them, and the
research behind both. The people were missing: who to write to, why they matter,
which channel actually reaches them, and whether anything has happened yet.
That lived in markdown sweeps and a spreadsheet, so it was never counted and
never followed up on a schedule — a list of twenty-seven people is only a list
until something tells you which six have gone quiet.

The identity of a contact is ``(name, group)``, not the name alone. The same
person can legitimately appear in two sweeps — a Forum list and a referee list
— and those are two different reasons to write, with different openers and
different states. Re-importing a corrected CSV therefore UPDATES the row rather
than forking a second one, the same rule ``vac add`` and ``vac report add`` use.

Pure helpers (channel extraction, CSV row -> contact, status transitions) are
separated from the writes on purpose: they are what the importer's behaviour
actually rests on, and they test without a database.
"""

import csv
import json
from pathlib import Path

from statuses import (
    VALID_CONTACT_CHANNELS,
    VALID_CONTACT_STATUSES,
)

#: What the sweep CSVs write when a field was looked for and not found. It is
#: not the same as an empty cell — "?" means "we searched and there is nothing",
#: blank means "not looked at" — but for storage both are absent, and storing
#: the literal "?" would put a question mark on screen as if it were a handle.
_UNKNOWN_MARKERS = frozenset({"?", "-", "—", "n/a", "na", "none", "unknown"})


def clean_value(value) -> str:
    """A CSV cell as a real value, or "" when the cell says "nothing here"."""
    text = str(value if value is not None else "").strip()
    if not text or text.lower() in _UNKNOWN_MARKERS:
        return ""
    return text


def extract_channels(row: dict) -> dict:
    """The reachable channels in a CSV row, as {channel: handle}.

    Only the known channel columns are read, so an unrelated column can never
    become a channel that nothing knows how to render. Empty and "?" cells are
    dropped rather than stored, so "has a LinkedIn" stays a question the data
    can answer.
    """
    channels = {}
    for key in VALID_CONTACT_CHANNELS:
        value = clean_value(row.get(key))
        if value:
            channels[key] = value
    return channels


def normalise_group(value: str) -> str:
    """A group name as stored: lowercase, spaces and underscores to hyphens.

    Free vocabulary (see migration 0024), so this only makes the SAME group
    written two ways land on one row — "EA Russian" and "ea-russian" are one
    list, and an identity keyed on the raw string would have made them two.
    """
    text = clean_value(value).lower().replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-") or "other"


def derive_group(row: dict, default: str = "other") -> str:
    """The group for a CSV row: its own column if it has one, else the default.

    The sweep CSVs do not carry a group column — one file is one list — so the
    importer passes the group in. A file that DOES carry one wins, because then
    a single CSV can hold several lists.
    """
    own = clean_value(row.get("group"))
    return normalise_group(own) if own else normalise_group(default)


#: Region groups that can be read off a person's city or organisation, most
#: specific token first. Deliberately a small, explicit table rather than a
#: general place lookup: these are the three regional sweeps that exist, and a
#: fuzzy match on country names would quietly refile people as new lists appear.
_REGION_TOKENS = (
    ("ea-georgia", ("georgia", "tbilisi", "batumi", "kutaisi", "sakartvelo")),
    ("ea-turkey", ("turkiye", "turkey", "istanbul", "ankara", "izmir")),
)


def derive_region_group(row: dict) -> str:
    """The regional list a person belongs to, read off their city and org.

    Returns "" when nothing matches, so the caller can fall back to the group
    the importer was given rather than inventing one. Matched on word-ish
    boundaries against the lowercased text: "Georgia (Republic of)" and
    "Tbilisi, Georgia" both answer ea-georgia, and a US state called Georgia
    would too — which is why the caller passes only files where that is the
    intended reading.
    """
    haystack = " ".join(
        clean_value(row.get(field)) for field in ("city", "org", "role", "why_matters")
    ).lower()
    if not haystack:
        return ""
    for group, tokens in _REGION_TOKENS:
        if any(token in haystack for token in tokens):
            return group
    return ""


def contact_from_csv_row(
    row: dict, *, group: str = "other", source_path: str = "", derive_region: bool = False
) -> dict:
    """One CSV row as a contact, or None when the row has no name.

    A nameless row is a trailing blank line or a note someone left in the file;
    importing it would create a contact called "" that can never be matched
    again, so it is skipped rather than stored.
    """
    name = clean_value(row.get("name"))
    if not name:
        return None

    status = clean_value(row.get("status")).lower()
    if status not in VALID_CONTACT_STATUSES:
        # An unrecognised or absent status means the sweep did not record one.
        # 'planned' is the honest default: the person is on the list and
        # nothing has been sent.
        status = "planned"

    return {
        "name": name,
        "name_local": clean_value(row.get("name_local")),
        "city": clean_value(row.get("city")),
        "org": clean_value(row.get("org")),
        "role": clean_value(row.get("role")),
        "why_matters": clean_value(row.get("why_matters")),
        "channels": extract_channels(row),
        "group": _group_for(row, group, derive_region),
        "status": status,
        "last_active": clean_value(row.get("last_active")),
        "opener": clean_value(row.get("opener")),
        # The sweeps put the reasoning for a channel choice in its own column,
        # and the sources behind the row in another. Both are notes about how
        # to use the contact, so they are kept together rather than dropped.
        "notes": _notes_from(row),
        "source_path": source_path,
    }


def _group_for(row: dict, default: str, derive_region: bool) -> str:
    """Which list a row belongs to.

    Order of authority: the row's own group column, then the region read off
    its city/org when the importer asked for that, then the group the importer
    was given. The explicit always beats the inferred.
    """
    own = clean_value(row.get("group"))
    if own:
        return normalise_group(own)
    if derive_region:
        region = derive_region_group(row)
        if region:
            return region
    return normalise_group(default)


def _notes_from(row: dict) -> str:
    parts = []
    suggested = clean_value(row.get("suggested_channel"))
    if suggested:
        parts.append(f"Suggested channel: {suggested}")
    sources = clean_value(row.get("source_urls"))
    if sources:
        parts.append(f"Sources: {sources}")
    existing = clean_value(row.get("notes"))
    if existing:
        parts.insert(0, existing)
    return "\n".join(parts)


def read_csv(path) -> list[dict]:
    """Rows of a sweep CSV, as dicts. Raises if the file has no name column."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"No such file: {file_path}")
    with file_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "name" not in reader.fieldnames:
            raise ValueError(
                f"{file_path} has no 'name' column — found: {reader.fieldnames}"
            )
        return [dict(r) for r in reader]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _conn():
    from database_supabase import get_conn

    return get_conn()


def _is_sqlite() -> bool:
    import db_backend

    return db_backend.IS_SQLITE


def _encode_channels(channels: dict) -> str:
    """Channels on their way into the database, as a JSON string.

    SQLite has no JSON type and stores it as TEXT; Postgres casts the same
    string into JSONB on the way in. One representation for both, and
    decode_channels turns it back into a dict on the way out, so callers never
    see the difference.
    """
    return json.dumps(channels or {}, ensure_ascii=False)


def decode_channels(value) -> dict:
    """Whatever the backend returned, as a dict. Never raises on bad JSON."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def upsert_contact(contact: dict) -> str:
    """Store one contact, keyed on (name, group). Returns its id.

    ``created_at`` survives an update — the person was first found when they
    were first found — while ``updated_at`` moves, because it is what a re-import
    is claiming to have changed.

    A re-import must not silently undo work done in the UI, so ``status`` is
    only overwritten when the incoming row actually carries one. A sweep CSV
    with an empty status column re-imported after a reply would otherwise reset
    everyone to 'planned'.
    """
    name = clean_value(contact.get("name"))
    group = normalise_group(contact.get("group") or "other")
    if not name:
        raise ValueError("a contact needs a name")

    status = clean_value(contact.get("status")).lower() or "planned"
    if status not in VALID_CONTACT_STATUSES:
        raise ValueError(
            f"Unknown contact status {status!r}. "
            f"Allowed: {', '.join(sorted(VALID_CONTACT_STATUSES))}"
        )

    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, status FROM contact WHERE name = %s AND "group" = %s',
        (name, group),
    )
    row = cur.fetchone()
    channels = _encode_channels(contact.get("channels") or {})

    if row:
        contact_id, current_status = row[0], row[1]
        # Keep the status the UI last set unless the import states one.
        keep_status = current_status if not contact.get("status") else status
        cur.execute(
            'UPDATE contact SET name_local = %s, city = %s, org = %s, role = %s, '
            "why_matters = %s, channels = %s, status = %s, last_active = %s, "
            "opener = %s, notes = %s, source_path = %s, updated_at = now() "
            "WHERE id = %s",
            (
                clean_value(contact.get("name_local")),
                clean_value(contact.get("city")),
                clean_value(contact.get("org")),
                clean_value(contact.get("role")),
                clean_value(contact.get("why_matters")),
                channels,
                keep_status,
                clean_value(contact.get("last_active")),
                clean_value(contact.get("opener")),
                clean_value(contact.get("notes")),
                clean_value(contact.get("source_path")),
                contact_id,
            ),
        )
    else:
        cur.execute(
            'INSERT INTO contact (name, name_local, city, org, role, why_matters, '
            'channels, "group", status, last_active, opener, notes, source_path) '
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                clean_value(contact.get("name_local")),
                clean_value(contact.get("city")),
                clean_value(contact.get("org")),
                clean_value(contact.get("role")),
                clean_value(contact.get("why_matters")),
                channels,
                group,
                status,
                clean_value(contact.get("last_active")),
                clean_value(contact.get("opener")),
                clean_value(contact.get("notes")),
                clean_value(contact.get("source_path")),
            ),
        )
        contact_id = cur.fetchone()[0]
    cur.close()
    return str(contact_id)


def list_contacts(status: str = None, group: str = None) -> list[dict]:
    """Stored contacts, newest activity first.

    Ordered by status_at rather than created_at: the question the list answers
    is "what moved, and what has not", so a contact written to yesterday belongs
    above one added last month and never touched. Rows with no status_at (still
    'planned') sort last, which is where a queue belongs.
    """
    from db_backend import RealDictCursor

    conditions = []
    params = []
    if status:
        conditions.append("status = %s")
        params.append(status)
    if group:
        conditions.append('"group" = %s')
        params.append(normalise_group(group))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    columns = (
        'id, name, name_local, city, org, role, why_matters, channels, '
        '"group", status, status_at, last_active, opener, notes, source_path, '
        "created_at, updated_at"
    )
    # SQLite has no NULLS LAST; "status_at IS NULL" sorts false(0) before
    # true(1) and puts the untouched rows at the bottom, same as Postgres.
    order = (
        " ORDER BY status_at IS NULL, status_at DESC, name ASC"
        if _is_sqlite()
        else " ORDER BY status_at DESC NULLS LAST, name ASC"
    )

    cur = _conn().cursor(cursor_factory=RealDictCursor)
    cur.execute(f"SELECT {columns} FROM contact{where}{order}", params)
    rows = []
    for record in cur.fetchall():
        item = dict(record)
        item["channels"] = decode_channels(item.get("channels"))
        item["id"] = str(item["id"])
        rows.append(item)
    cur.close()
    return rows


def set_contact_status(contact_id: str, status: str) -> bool:
    """Move one contact to a new status, stamping when it moved.

    Returns False when no such contact exists, so a caller can answer 404
    rather than reporting a successful write that changed nothing.
    """
    status = clean_value(status).lower()
    if status not in VALID_CONTACT_STATUSES:
        raise ValueError(
            f"Unknown contact status {status!r}. "
            f"Allowed: {', '.join(sorted(VALID_CONTACT_STATUSES))}"
        )
    cur = _conn().cursor()
    cur.execute(
        "UPDATE contact SET status = %s, status_at = now(), updated_at = now() "
        "WHERE id = %s",
        (status, contact_id),
    )
    changed = cur.rowcount > 0
    cur.close()
    return changed


def count_by_status(contacts: list[dict]) -> dict:
    """How many contacts sit in each status, including the zeroes.

    Zeroes are kept, unlike the Applications count strip: this list is a queue,
    and "0 replied" is the number that says the sweep has not paid off yet.
    """
    from statuses import CONTACT_STATUSES

    counts = {s: 0 for s in CONTACT_STATUSES}
    for contact in contacts:
        status = (contact or {}).get("status")
        if status in counts:
            counts[status] += 1
    return counts


def table_ready() -> bool:
    """True once migration 0024 has run.

    Lets the CLI say "run the migration" instead of failing with "no such
    table" — the same courtesy reports.table_ready() gives its own table.
    """
    from database_supabase import _table_has_column

    return _table_has_column("contact", "name")


def import_csv(
    path, *, group: str = "other", source_path: str = None, derive_region: bool = False
) -> dict:
    """Import a sweep CSV. Returns {'added': n, 'updated': n, 'skipped': n}.

    Counts added and updated separately because a re-import is the normal case
    — the sweeps get corrected — and "27 updated" is the line that says the file
    landed on the rows it was meant to, rather than forking 27 new ones.
    """
    rows = read_csv(path)
    where_from = source_path if source_path is not None else str(path)

    seen = {}
    for row in rows:
        contact = contact_from_csv_row(
            row, group=group, source_path=where_from, derive_region=derive_region
        )
        if contact is None:
            continue
        # A CSV that lists the same person twice is a mistake in the file, not
        # two contacts: the second row wins and the pair counts once.
        seen[(contact["name"], contact["group"])] = contact

    existing = {(c["name"], c["group"]) for c in list_contacts()}
    added = updated = 0
    for key, contact in seen.items():
        upsert_contact(contact)
        if key in existing:
            updated += 1
        else:
            added += 1

    return {
        "added": added,
        "updated": updated,
        "skipped": len(rows) - len(seen),
    }
