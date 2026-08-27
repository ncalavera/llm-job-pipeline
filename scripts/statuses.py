"""Vacancy status vocabulary — ONE source for every module that reasons about
statuses.

It sits in its own stdlib-only leaf module on purpose. ``filters.py`` may not
import the data-access layer (its docstring and tests/test_filters_module.py
enforce that), yet its dedup guard and the DAL have to agree on which statuses
record a user decision. Two hand-maintained copies drifted exactly where it
hurt: 'test_task', 'interview' and 'declined' shipped into the DAL but never
reached the filter stage's protected list, so the filter deleted and tombstoned
an application that was still in flight.

The SQL CHECK on ``vacancy.status`` (sql/schema.sql) is the other half of this
contract; tests/test_schema_integrity.py asserts the two never drift apart.
"""

#: Every value ``vacancy.status`` may hold, in board order (untriaged first,
#: then the triage baskets, then the application funnel, then the two
#: out-of-view states). Order is the ONLY reason this is a tuple — it drives
#: the generated ``models.VacancyStatus`` enum.
ALL_STATUSES: tuple[str, ...] = (
    "unseen",
    "liked",
    "passed",
    "to_apply",
    "to_research",
    "to_network",
    "skipped",
    "applied",
    "test_task",
    "interview",
    "declined",
    "accepted",
    "expiring",
    "archived",
)

#: Membership form of ALL_STATUSES — what validators check against.
VALID_STATUSES: frozenset[str] = frozenset(ALL_STATUSES)

#: The application funnel: the user applied, and this row IS the record of it.
#: These are the search statistics; nothing automatic may archive or delete one.
APPLICATION_STATUSES: frozenset[str] = frozenset(
    {
        "applied",
        "test_task",
        "interview",
        "declined",
        # The other way an application ends: an offer, or a place on a
        # programme. Kept next to 'declined' rather than folded into it —
        # "they said yes" and "they said no" are the two answers the funnel
        # exists to count, and only one of them is a rejection.
        "accepted",
    }
)

#: Statuses that carry a user decision — a renamed/language variant must inherit
#: one of these rather than resurface as 'unseen'.
DECIDED_STATUSES: frozenset[str] = APPLICATION_STATUSES | frozenset(
    {
        "liked",
        "to_apply",
        "to_research",
        "to_network",
        "passed",
        "skipped",
    }
)

#: What the filter/dedup stages must never delete: every user decision, plus
#: 'archived' (already out of view — deleting it would drop the tombstone that
#: keeps it from being re-fetched).
PROTECTED_STATUSES: frozenset[str] = DECIDED_STATUSES | frozenset({"archived"})
