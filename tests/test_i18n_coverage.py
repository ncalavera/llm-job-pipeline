"""Every UI string the frontend asks for has a translation to give it.

The Applications table and the Reports tab shipped calling ``T(key, fallback)``
for their labels, but the keys were never added to ``scripts/i18n.py``. Because
a missing key falls back to its English default, nothing broke and nothing
looked broken in tests — the dashboard just quietly rendered "SENT ON" and
"7 sent · 3 waiting" inside an otherwise Russian shell. A fallback that works
is exactly why this needs a test: the failure is invisible to every other one.

So this reads the keys OUT of the frontend rather than listing them here. A new
`T("apps_col_owner", "Owner")` with no entry in i18n.py fails immediately,
whoever adds it.

Offline and file-based: no DB, no network, no maintainer data.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = str(REPO_ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import i18n  # noqa: E402

MODULES_DIR = REPO_ROOT / "public" / "modules"
INDEX_HTML = REPO_ROOT / "public" / "index.html"

# A literal key inside T("…") or translate("…"). Keys built by concatenation —
# T("apps_kind_" + row.kind, …) — end at the quote and are filtered out below;
# their concrete variants are covered by the enumerated tests further down.
_CALL_RE = re.compile(r'(?:\bT|translate)\(\s*"([a-z0-9_]+)"')
_ATTR_RE = re.compile(r'data-i18n(?:-ph)?="([a-z0-9_]+)"')


def _english_keys() -> set[str]:
    return set(i18n.STRINGS["en"])


def _frontend_keys() -> dict[str, set[str]]:
    """Literal keys per source file, prefixes dropped."""
    found: dict[str, set[str]] = {}
    for path in sorted(MODULES_DIR.glob("*.js")):
        if path.name.endswith(".test.js"):
            continue
        keys = {k for k in _CALL_RE.findall(path.read_text(encoding="utf-8"))}
        # A trailing underscore means the call concatenated a variant onto it.
        keys = {k for k in keys if not k.endswith("_")}
        if keys:
            found[path.name] = keys
    attrs = set(_ATTR_RE.findall(INDEX_HTML.read_text(encoding="utf-8")))
    if attrs:
        found["index.html"] = attrs
    return found


# The tabs this test covers. Kept as an explicit list so the guard cannot be
# satisfied by deleting a module from the scan. The Health tab is deliberately
# absent: it has ~26 keys that exist in neither language, predating this test,
# and adding it here would fail the suite for a gap it did not create. Add it
# to this tuple in the same change that translates it.
NEW_TAB_SOURCES = ("applications.js", "reports.js", "contacts.js")


@pytest.mark.parametrize("source", NEW_TAB_SOURCES)
def test_new_tab_keys_all_have_translations(source):
    """Applications and Reports ask for nothing i18n.py cannot answer."""
    keys = _frontend_keys().get(source, set())
    assert keys, f"{source} asked for no i18n keys at all — did the scan break?"
    missing = sorted(keys - _english_keys())
    assert not missing, f"{source} uses keys absent from i18n.py: {missing}"


def test_every_language_covers_the_english_key_set():
    """A translation may not silently omit a key.

    ``strings()`` falls back to English per key, so a partial translation
    renders as mixed language rather than as a missing label — readable, and
    invisible. This makes the omission loud instead.
    """
    english = _english_keys()
    for lang, table in i18n.STRINGS.items():
        if lang == "en":
            continue
        missing = sorted(english - set(table))
        assert not missing, f"{lang!r} is missing {len(missing)} keys: {missing[:10]}"


def test_no_language_invents_keys_english_does_not_have():
    """English is the canonical key set; a key only there is a typo or dead."""
    english = _english_keys()
    for lang, table in i18n.STRINGS.items():
        if lang == "en":
            continue
        extra = sorted(set(table) - english)
        assert not extra, f"{lang!r} has keys English does not: {extra}"


# --- The concatenated key families ----------------------------------------
# These are built at runtime, so the scan above cannot see them. Enumerate the
# vocabulary each one closes over and prove every member resolves.

CONTACT_STATUSES = ("planned", "contacted", "replied", "met", "declined", "stale")
CONTACT_CHANNELS = (
    "ea_forum",
    "linkedin",
    "telegram",
    "x",
    "github",
    "site",
    "email",
    "calendly",
)

VACANCY_KINDS = ("job", "programme", "advising", "consulting", "grant", "course")
REPORT_KINDS = ("research", "sector", "company", "grant", "other")
FUNNEL_STATUSES = ("applied", "test_task", "interview", "declined", "accepted")
PLURAL_FORMS = ("one", "few", "many")


@pytest.mark.parametrize("kind", VACANCY_KINDS)
def test_every_application_kind_has_a_label(kind):
    assert f"apps_kind_{kind}" in _english_keys()


@pytest.mark.parametrize("kind", REPORT_KINDS)
def test_every_report_kind_has_a_label(kind):
    assert f"reports_kind_{kind}" in _english_keys()


@pytest.mark.parametrize("status", FUNNEL_STATUSES)
def test_every_funnel_stage_has_a_name(status):
    """The Applications table's Stage column reads these same keys."""
    assert f"vac_status_{status}" in _english_keys()


@pytest.mark.parametrize("form", PLURAL_FORMS)
def test_plural_families_are_complete(form):
    """Russian needs three forms; a missing one shows the English word."""
    for base in ("apps_waiting_day", "reports_count"):
        assert f"{base}_{form}" in _english_keys(), f"{base}_{form} missing"


def test_russian_uses_the_three_plural_forms_distinctly():
    """1 день / 2 дня / 5 дней — three different words, or the rule is pointless."""
    ru = i18n.STRINGS["ru"]
    days = {ru["apps_waiting_day_one"], ru["apps_waiting_day_few"], ru["apps_waiting_day_many"]}
    assert len(days) == 3, f"expected three distinct day forms, got {days}"
    reports = {ru["reports_count_one"], ru["reports_count_few"], ru["reports_count_many"]}
    assert len(reports) == 3, f"expected three distinct report forms, got {reports}"


def test_the_two_new_tabs_are_actually_translated_into_russian():
    """Not just present, but different from English — a copied English string
    would satisfy every parity check above while still rendering as English."""
    ru = i18n.STRINGS["ru"]
    en = i18n.STRINGS["en"]
    sample = [
        "tab_reports",
        "apps_col_sent_on",
        "apps_col_organisation",
        "apps_col_stage",
        "apps_count_sent",
        "triage_view_table",
        "reports_back",
    ]
    untranslated = [k for k in sample if ru[k] == en[k]]
    assert not untranslated, f"still English in the Russian table: {untranslated}"


@pytest.mark.parametrize("status", CONTACT_STATUSES)
def test_every_contact_status_has_a_label(status):
    assert f"contact_status_{status}" in _english_keys()


@pytest.mark.parametrize("channel", CONTACT_CHANNELS)
def test_every_contact_channel_has_a_word(channel):
    """A word, never a bare glyph — the house style bans category codes."""
    assert f"contact_channel_{channel}" in _english_keys()


@pytest.mark.parametrize("form", PLURAL_FORMS)
def test_the_contact_count_has_all_three_plural_forms(form):
    assert f"contacts_count_{form}" in _english_keys()


def test_the_networking_tab_is_translated_into_russian():
    ru = i18n.STRINGS["ru"]
    en = i18n.STRINGS["en"]
    sample = [
        "tab_contacts",
        "contacts_col_name",
        "contacts_col_status",
        "contacts_opener",
        "contact_status_planned",
        "contact_status_replied",
    ]
    untranslated = [k for k in sample if ru[k] == en[k]]
    assert not untranslated, f"still English in the Russian table: {untranslated}"


def test_the_contact_status_vocabulary_matches_the_python_one():
    """The i18n keys, statuses.py and the SQL CHECK are three copies of one
    list. A status with no label would render as its raw key."""
    from statuses import CONTACT_STATUSES as PY_STATUSES

    assert set(PY_STATUSES) == set(CONTACT_STATUSES)
