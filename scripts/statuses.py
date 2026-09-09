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

#: What was applied to. Every scraped role is a 'job'; the rest are things he
#: applied to that are not vacancies — a course, an incubation programme, a
#: career-advising session, a consulting engagement, a grant — and that are
#: stored as ordinary vacancy rows so the funnel counts them with everything
#: else. The SQL CHECK on ``vacancy.kind`` (migration 0022) is the other half of
#: this contract.
VACANCY_KINDS: tuple[str, ...] = (
    "job",
    "programme",
    "advising",
    "consulting",
    "grant",
    "course",
)

#: Membership form of VACANCY_KINDS — what validators check against.
VALID_KINDS: frozenset[str] = frozenset(VACANCY_KINDS)

#: What kind of reading a stored report is — the one axis the Reports list
#: groups by. A closed vocabulary: an unrecognised kind would silently create a
#: group of one, which reads as a bug in the grouping rather than as a typo. The
#: SQL CHECK on ``report.kind`` (migration 0023) is the other half.
REPORT_KINDS: tuple[str, ...] = (
    "research",
    "grant",
    "company",
    "sector",
    "other",
)

#: Membership form of REPORT_KINDS — what validators check against.
VALID_REPORT_KINDS: frozenset[str] = frozenset(REPORT_KINDS)

#: What the filter/dedup stages must never delete: every user decision, plus
#: 'archived' (already out of view — deleting it would drop the tombstone that
#: keeps it from being re-fetched).
PROTECTED_STATUSES: frozenset[str] = DECIDED_STATUSES | frozenset({"archived"})

#: Where a networking contact stands. Ordered as the funnel runs, so the UI can
#: lay the counts out in this order without a second list to keep in step.
#: Closed, because this vocabulary IS the funnel: a typo would drop someone out
#: of every count silently. The SQL CHECK on ``contact.status`` (migration 0024)
#: is the other half of the contract, and the dashboard's copy is the third.
CONTACT_STATUSES: tuple[str, ...] = (
    "planned",
    "contacted",
    "replied",
    "met",
    "declined",
    "stale",
)

#: Membership form of CONTACT_STATUSES — what validators check against.
VALID_CONTACT_STATUSES: frozenset[str] = frozenset(CONTACT_STATUSES)

#: Contacts who have not been written to yet — the queue the tab exists to
#: work through.
CONTACT_PENDING_STATUSES: frozenset[str] = frozenset({"planned"})

#: Sent, and the answer has not come. These are the rows that go stale if
#: nothing happens, which is the one thing a networking list gets wrong.
CONTACT_WAITING_STATUSES: frozenset[str] = frozenset({"contacted"})

#: The conversation happened. Counted together because "they wrote back" and
#: "we spoke" are both the outcome the list is for.
CONTACT_ENGAGED_STATUSES: frozenset[str] = frozenset({"replied", "met"})

#: Which list a contact came from. Deliberately NOT a SQL CHECK: the groups are
#: working sets that come and go with each sweep, and a constraint would turn
#: "I made a new list today" into a migration. This tuple is what the UI offers
#: as filter buttons; an unknown group still stores and still shows.
CONTACT_GROUPS: tuple[str, ...] = (
    "ea-russian",
    "ea-georgia",
    "ea-turkey",
    "ea-forum-open",
    "network-2026-07",
    "yandex-referees",
    "other",
)

#: The channels a contact can be reached on, in the order the UI shows them:
#: EA-native first, then the general networks, then the direct ones.
CONTACT_CHANNELS: tuple[str, ...] = (
    "ea_forum",
    "linkedin",
    "telegram",
    "x",
    "github",
    "site",
    "email",
    "calendly",
)

#: Membership form of CONTACT_CHANNELS — what the importer filters against, so
#: an unknown CSV column can never become a channel nothing knows how to render.
VALID_CONTACT_CHANNELS: frozenset[str] = frozenset(CONTACT_CHANNELS)
