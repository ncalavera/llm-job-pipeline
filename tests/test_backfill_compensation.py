"""backfill_compensation_from_text — mirrors test_backfill_deadline.py.

Real bug it fixes: GiveWell's "Senior Program Officer" full_description
names a location-tiered salary ("Our pay for this role: NYC or the San
Francisco Bay Area: $308,000. All other U.S. locations: $280,000...") while
`compensation` stayed NULL, so the screener card showed "not stated". Every
write site that saves fresh posting text now also calls this, the same way
backfill_deadline_from_text already does for `deadline`.
"""

from unittest.mock import MagicMock

import database_supabase as db

GIVEWELL_TEXT = (
    "GiveWell sets salaries using a location-based tier system. Our pay for "
    "this role: NYC or the San Francisco Bay Area: $308,000. All other U.S. "
    "locations: $280,000. International: Similar to the “all other U.S. "
    "locations” salary, based on historical exchange rates and delivered "
    "in locally-denominated currency."
)

POSITIVE_CASES = [
    ("givewell_location_tiered", GIVEWELL_TEXT, "$280,000–$308,000 (by location)"),
    (
        "dollar_range",
        "The salary range for this role is $270,000 - $330,000.",
        "$270,000 - $330,000",
    ),
    (
        "usd_code_range_per_year",
        "Compensation: USD 200,000–230,000/year, commensurate with experience.",
        "USD 200,000–230,000/year",
    ),
    ("gbp_k_range", "Base salary: £55k–£65k depending on seniority.", "£55k–£65k"),
    ("euro_dot_thousands", "Annual salary of €70.000 gross, paid monthly.", "€70.000"),
    (
        "euro_monthly_range",
        "Compensation The compensation range for this role is €3,300 - €4,300 gross per month.",
        "€3,300 - €4,300",
    ),
    (
        "dollar_stipend",
        "This is a paid fellowship; the stipend is $9,000 stipend for the term.",
        "$9,000 stipend",
    ),
    (
        "dollar_per_month",
        "Compensation: $6,000 per month stipend, plus benefits.",
        "$6,000 per month",
    ),
    (
        "wide_dollar_range",
        "The salary range for this role is $238,400 to $369,400 USD, based on experience.",
        "$238,400 to $369,400",
    ),
    (
        "inr_range_per_year",
        "Salary: INR 360,000-450,000/yr depending on location.",
        "INR 360,000-450,000/yr",
    ),
    ("dollar_per_hour", "Pay: $28/hr for this hourly, part-time role.", "$28/hr"),
    (
        "ote_dollar_range",
        "OTE for this role is $120,000 - $230,000 including commission.",
        "$120,000 - $230,000",
    ),
    (
        "usd_mo_single",
        "Compensation: this role pays USD 5000/mo, paid twice monthly.",
        "USD 5000/mo",
    ),
]

NEGATIVE_CASES = [
    ("grant_budget", "The total grant budget for this program is $2,500,000 over three years."),
    ("funds_raised", "The organization has raised $5 million from donors since founding."),
    ("team_budget", "Our team budget this year is $400,000, covering travel and tools."),
    ("year_not_money", "Applications open in 2,026 and the program starts that year."),
]


def test_positive_cases_extract_expected_string():
    for name, text, expected in POSITIVE_CASES:
        got = db._extract_compensation_from_description(text)
        assert got == expected, f"{name}: expected {expected!r}, got {got!r}"


def test_negative_cases_extract_nothing():
    for name, text in NEGATIVE_CASES:
        got = db._extract_compensation_from_description(text)
        assert got == "", f"{name}: expected no match, got {got!r}"


def test_backfill_writes_when_extractable_and_compensation_missing():
    cur = MagicMock()
    wrote = db.backfill_compensation_from_text(cur, "vid-1", GIVEWELL_TEXT)
    assert wrote is True
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "COALESCE(NULLIF(compensation" in sql
    assert params == ("$280,000–$308,000 (by location)", "vid-1")


def test_backfill_noop_when_text_has_no_pay():
    cur = MagicMock()
    wrote = db.backfill_compensation_from_text(cur, "vid-2", "No compensation info here.")
    assert wrote is False
    cur.execute.assert_not_called()
