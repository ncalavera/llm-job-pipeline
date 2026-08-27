"""KISS CLI for vacancy triage — list / show / mark / open / companies.

Run: python3 scripts/vac.py list --status liked
Alias: alias vac="cd /path/to/llm-job-pipeline && python3 scripts/vac.py"
"""

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Imported up here, above build_parser, because the parser itself needs the
# channel list to generate one flag per channel — and `vac --help` builds the
# parser BEFORE the heavy imports below, to print usage without touching the
# database or the profile. statuses.py is pure constants, so it costs nothing.
from statuses import CONTACT_CHANNELS  # noqa: E402


def build_parser(handlers: dict | None = None) -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile. ``handlers`` maps each subcommand to its function
    (only needed when actually dispatching; for ``--help`` it may be omitted)."""
    h = handlers or {}
    parser = argparse.ArgumentParser(
        prog="vac", description="KISS vacancy triage from the terminal."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List vacancies with filters.")
    p_list.add_argument("--status", help="Filter by status: unseen, liked, passed, to_apply…")
    p_list.add_argument("--min-score", type=int, help="Minimum LLM score.")
    p_list.add_argument("--tier", help="Filter by company tier (S/A/B/C).")
    p_list.add_argument("--org", help="Filter by company-name substring.")
    p_list.add_argument("--limit", type=int, help="How many rows to show.")
    p_list.add_argument("--sort", choices=["score", "recent", "company"], default="score")
    p_list.add_argument(
        "--include-candidates",
        action="store_true",
        help="Show vacancies from non-approved companies.",
    )
    p_list.add_argument(
        "--geo",
        help="Geo buckets, comma-separated: uk,germany,europe,us,cis,other,unknown. Shows vacancies with at least one location in the chosen buckets.",
    )
    if "list" in h:
        p_list.set_defaults(func=h["list"])

    p_show = sub.add_parser("show", help="Details of a single vacancy.")
    p_show.add_argument("id", help="UUID prefix (at least 4 chars).")
    p_show.add_argument("--full", action="store_true", help="Show full_description.")
    if "show" in h:
        p_show.set_defaults(func=h["show"])

    p_mark = sub.add_parser("mark", help="Change a vacancy's status.")
    p_mark.add_argument("id", help="UUID prefix.")
    p_mark.add_argument("status", help="New status (liked, passed, to_apply, applied, …)")
    if "mark" in h:
        p_mark.set_defaults(func=h["mark"])

    p_add = sub.add_parser(
        "add",
        help="Record an application that never came from a job board "
        "(a course, career advising, a programme).",
    )
    p_add.add_argument("--company", required=True, help="Organisation name.")
    p_add.add_argument("--title", required=True, help="What you applied to.")
    p_add.add_argument(
        "--kind",
        default="job",
        help="job / programme / advising / consulting / grant / course. Default: job.",
    )
    p_add.add_argument(
        "--status",
        default="applied",
        help="Where it stands: applied, test_task, interview, declined, accepted. "
        "Default: applied.",
    )
    p_add.add_argument("--applied-at", dest="applied_at", help="Date sent, YYYY-MM-DD.")
    p_add.add_argument(
        "--status-at",
        dest="status_at",
        help="Date the status last changed, YYYY-MM-DD. Defaults to --applied-at.",
    )
    p_add.add_argument("--url", default="", help="Link to the posting or the page.")
    p_add.add_argument("--note", default="", help="What happens next, in one line.")
    if "add" in h:
        p_add.set_defaults(func=h["add"])

    p_open = sub.add_parser("open", help="Open the vacancy link in the browser.")
    p_open.add_argument("id", help="UUID prefix.")
    if "open" in h:
        p_open.set_defaults(func=h["open"])

    p_report = sub.add_parser(
        "report",
        help="Store and list research reports (the Reports tab on the dashboard).",
    )
    report_sub = p_report.add_subparsers(dest="report_cmd", required=True)

    p_report_add = report_sub.add_parser(
        "add", help="Store a markdown report (re-run to update it)."
    )
    p_report_add.add_argument("path", help="Path to the .md file.")
    p_report_add.add_argument("--title", help="Override the title. Default: the file's first H1.")
    p_report_add.add_argument(
        "--kind",
        help="research / grant / company / sector / other. Default: guessed from the path.",
    )

    report_sub.add_parser("list", help="List stored reports, newest first.")

    if "report" in h:
        p_report.set_defaults(func=h["report"])

    p_contact = sub.add_parser(
        "contact",
        help="People to reach out to (the Networking tab on the dashboard).",
    )
    contact_sub = p_contact.add_subparsers(dest="contact_cmd", required=True)

    p_contact_import = contact_sub.add_parser(
        "import", help="Import a sweep CSV of people (re-run to update them)."
    )
    p_contact_import.add_argument("path", help="Path to the .csv file.")
    p_contact_import.add_argument(
        "--group",
        default="other",
        help="Which list these people came from, e.g. ea-georgia. Used when the "
        "CSV has no group column. Default: other.",
    )
    p_contact_import.add_argument(
        "--source",
        help="What to record as the source of these rows. Default: the CSV path.",
    )
    p_contact_import.add_argument(
        "--derive-region",
        dest="derive_region",
        action="store_true",
        help="Read the regional list off each person's city and org "
        "(ea-georgia / ea-turkey), falling back to --group.",
    )

    p_contact_add = contact_sub.add_parser("add", help="Add or edit one person.")
    p_contact_add.add_argument("--name", required=True, help="Their name.")
    p_contact_add.add_argument("--group", default="other", help="Which list they belong to.")
    p_contact_add.add_argument("--name-local", dest="name_local", help="Name as they write it.")
    p_contact_add.add_argument("--city", help="Where they are.")
    p_contact_add.add_argument("--org", help="Where they work.")
    p_contact_add.add_argument("--role", help="What they do there.")
    p_contact_add.add_argument("--why", dest="why_matters", help="Why this person, in one line.")
    p_contact_add.add_argument(
        "--status",
        default="planned",
        help="planned / contacted / replied / met / declined / stale. Default: planned.",
    )
    p_contact_add.add_argument("--last-active", dest="last_active", help="Their last activity.")
    p_contact_add.add_argument("--opener", help="The first line to send them.")
    p_contact_add.add_argument("--notes", help="Anything else worth keeping.")
    p_contact_add.add_argument("--source", dest="source_path", help="Where this came from.")
    for channel in CONTACT_CHANNELS:
        p_contact_add.add_argument(
            f"--{channel.replace('_', '-')}",
            dest=f"ch_{channel}",
            help=f"Their {channel.replace('_', ' ')}.",
        )

    p_contact_list = contact_sub.add_parser("list", help="List people, newest activity first.")
    p_contact_list.add_argument("--status", help="Filter: planned / contacted / replied / …")
    p_contact_list.add_argument("--group", help="Filter by list, e.g. ea-georgia.")

    if "contact" in h:
        p_contact.set_defaults(func=h["contact"])

    p_publish = sub.add_parser(
        "publish",
        help="Rewrite the dashboard snapshot from what is already in the database "
        "(no fetch, no scoring, no LLM calls).",
    )
    if "publish" in h:
        p_publish.set_defaults(func=h["publish"])

    p_co = sub.add_parser("companies", help="List companies.")
    p_co.add_argument("--status", help="Filter by status: active / candidate / inactive.")
    p_co.add_argument("--limit", type=int, default=50)
    if "companies" in h:
        p_co.set_defaults(func=h["companies"])

    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from database_supabase import (
    _vacancy_has_column,
    activate_company,
    ensure_company,
    get_conn,
    load_vacancies,
    make_vacancy_id,
    update_vacancy_status,
    upsert_vacancy,
)
from geo import geo_bucket

# Imported, not re-listed. This file used to keep its own copy, which silently
# drifted the moment a status was added elsewhere: `vac.py mark <id> declined`
# was rejected here while the database, the API and the board all accepted it.
# One list, one place — database_supabase.VALID_STATUSES is the source.
from database_supabase import VALID_STATUSES  # noqa: E402

# Same rule for the `kind` vocabulary: read it, never retype it. The SQL CHECK
# on vacancy.kind (migration 0022) is the other half of the contract.
from statuses import (  # noqa: E402
    CONTACT_STATUSES,
    VALID_CONTACT_STATUSES,
    VALID_KINDS,
    VALID_REPORT_KINDS,
)

GEO_BUCKETS = {"uk", "germany", "europe", "us", "cis", "other", "unknown"}


def _term_width() -> int:
    return shutil.get_terminal_size((100, 20)).columns


def _ansi(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _color_score(score) -> str:
    if score is None:
        return _ansi("90", "  —")
    s = int(score)
    txt = f"{s:>3}"
    if s >= 70:
        return _ansi("32", txt)
    if s >= 50:
        return _ansi("33", txt)
    return _ansi("31", txt)


def _color_status(status: str) -> str:
    colors = {
        "liked": "32",
        "to_apply": "32;1",
        "applied": "36",
        "to_research": "34",
        "to_network": "34",
        "passed": "90",
        "skipped": "90",
        "unseen": "37",
    }
    return _ansi(colors.get(status, "0"), status or "?")


def _short_id(uid: str) -> str:
    return uid[:8]


def _resolve_uid(prefix: str, vacancies: dict) -> str | None:
    if prefix in vacancies:
        return prefix
    matches = [u for u in vacancies if u.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Prefix '{prefix}' is ambiguous — {len(matches)} matches.")
    return None


def _location_brief(locations) -> str:
    if not locations:
        return "—"
    parts = []
    for loc in locations[:2]:
        bits = []
        wm = loc.get("work_mode")
        if wm:
            bits.append(wm)
        city = loc.get("city")
        country = loc.get("country")
        if city:
            bits.append(city)
        elif country:
            bits.append(country)
        if bits:
            parts.append(" ".join(bits))
    return ", ".join(parts) if parts else "—"


def cmd_list(args):
    vacancies = load_vacancies(
        status=args.status,
        light=True,
        limit=args.limit,
        include_candidate_companies=args.include_candidates,
        # By default hide archived rows unless a status filter explicitly asks.
        status_exclude=None if args.status else ["archived"],
    )

    geo_filter = None
    if args.geo:
        geo_filter = {g.strip().lower() for g in args.geo.split(",") if g.strip()}
        bad = geo_filter - GEO_BUCKETS
        if bad:
            print(
                f"Unknown geo buckets: {', '.join(sorted(bad))}. "
                f"Allowed: {', '.join(sorted(GEO_BUCKETS))}"
            )
            sys.exit(1)

    rows = []
    for uid, v in vacancies.items():
        score = v.get("llm_score")
        if args.min_score is not None and (score is None or score < args.min_score):
            continue
        if args.tier and v.get("tier") != args.tier:
            continue
        if args.org:
            org = (v.get("org") or "").lower()
            if args.org.lower() not in org:
                continue
        if geo_filter is not None:
            locs = v.get("locations") or []
            if not any(geo_bucket(loc) in geo_filter for loc in locs):
                continue
        rows.append((uid, v))

    if args.sort == "score":
        rows.sort(key=lambda r: r[1].get("llm_score") or 0, reverse=True)
    elif args.sort == "recent":
        rows.sort(key=lambda r: r[1].get("created_at") or "", reverse=True)
    elif args.sort == "company":
        rows.sort(key=lambda r: (r[1].get("org") or "").lower())

    if not rows:
        if not vacancies:
            # Empty database (or no rows for this status) — point at the
            # pipeline's first action instead of a dead end.
            print("No vacancies yet.")
            print("  Fetch some first: /jobs-new (or python3 scripts/fetch_vacancies.py)")
            print("  No companies tracked yet? Run /jobs-new to discover the first ones,")
            print("  or /jobs-add to add a company by name.")
        else:
            # Rows exist but every one was filtered out.
            print(
                "No vacancies match these filters. Loosen --status / --min-score / "
                "--tier / --org / --geo, or run /jobs-new for fresh listings."
            )
        return

    width = _term_width()
    company_w = 18
    title_w = max(20, width - 8 - 1 - company_w - 1 - 4 - 1 - 10 - 1 - 14 - 5)

    header = f"{'ID':<8} {'Company':<{company_w}} {'Title':<{title_w}} {'Score':>4} {'Status':<10} {'Location':<14}"
    print(_ansi("1", header))
    print("─" * min(width, len(header) + 2))

    for uid, v in rows:
        company = (v.get("org") or "—")[:company_w]
        title = (v.get("title") or "—")[:title_w]
        score_s = _color_score(v.get("llm_score"))
        status_s = _color_status(v.get("status") or "unseen")
        loc = _location_brief(v.get("locations"))[:14]
        print(
            f"{_short_id(uid):<8} {company:<{company_w}} {title:<{title_w}} {score_s} {status_s:<19} {loc:<14}"
        )

    print()
    print(f"Total: {len(rows)}")


def cmd_show(args):
    vacancies = load_vacancies(
        light=False,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Not found: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    width = min(_term_width(), 100)

    print(_ansi("1", f"{v.get('org', '—')} — {v.get('title', '—')}"))
    print(
        f"ID: {uid}   Score: {v.get('llm_score') or '—'}   Status: {_color_status(v.get('status') or 'unseen')}"
    )
    if v.get("compensation"):
        print(f"Compensation: {v['compensation']}")
    if v.get("deadline"):
        print(f"Deadline: {v['deadline']}")
    locs = v.get("locations") or []
    if locs:
        print("Locations:")
        for loc in locs:
            url = loc.get("url") or ""
            bits = [
                loc.get("work_mode") or "",
                loc.get("city") or loc.get("country") or "",
                loc.get("compensation") or "",
            ]
            print(f"  - {' / '.join(b for b in bits if b)}  {url}")
    if v.get("llm_summary"):
        print()
        print(_ansi("1", "Summary:"))
        print(textwrap.fill(v["llm_summary"], width=width))
    if v.get("llm_reasoning"):
        print()
        print(_ansi("1", "Why this score:"))
        print(textwrap.fill(v["llm_reasoning"], width=width))
    if v.get("llm_hard_requirements"):
        print()
        print(_ansi("1", "Hard requirements:"))
        for r in v["llm_hard_requirements"]:
            print(f"  • {r}")
    if args.full and v.get("full_description"):
        print()
        print(_ansi("1", "Description:"))
        print(textwrap.fill(v["full_description"], width=width))


def cmd_mark(args):
    if args.status not in VALID_STATUSES:
        print(f"Invalid status: {args.status}. Allowed: {', '.join(sorted(VALID_STATUSES))}")
        sys.exit(1)
    vacancies = load_vacancies(
        light=True,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Not found: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    update_vacancy_status(uid, args.status)
    get_conn().commit()
    print(f"ok: {v.get('org', '—')} — {v.get('title', '—')[:50]} → {args.status}")


#: Manually added rows carry this instead of a job board's name, so the boards
#: report never counts a hand-entered application as a board's yield.
MANUAL_SOURCE_BOARD = "manual"


def _parse_day(value, label):
    """A YYYY-MM-DD argument as an explicit UTC instant, or exit naming the flag.

    Refusing an unparseable date is the point: a silently-dropped one would
    leave the Applications table showing the wrong send date with no way to
    notice.

    The UTC suffix matters as much as the parsing. A bare "2026-08-10" written
    into a TIMESTAMPTZ is midnight in the SERVER's timezone — on a +04 box that
    is 2026-08-09T20:00Z, and the dashboard, which pins dates to UTC so a
    timestamp never shifts a day between viewers, renders it as "9 Aug". Pinning
    it here means the day typed is the day stored and the day shown.
    """
    if not value:
        return None
    from datetime import date as _date

    try:
        return _date.fromisoformat(value).isoformat() + "T00:00:00+00:00"
    except ValueError:
        print(f"{label} must be a date like 2026-07-03 — got: {value}")
        sys.exit(1)


def cmd_add(args):
    """Record an application that never came from a job board.

    Not every application is a job. A course, an incubation programme, a
    career-advising session, a consulting engagement — he sent those too, and
    they belong in the same funnel as everything else or the count of "what I
    sent" is wrong. They land as ordinary vacancy rows, so every existing
    surface (the board, the Applications table, the company page) shows them
    with no special case anywhere.

    Re-running with the same company and title UPDATES that row rather than
    creating a second one — the dedup hash is derived from those two fields, so
    fixing a typo is a re-run, not a cleanup.
    """
    if args.status not in VALID_STATUSES:
        print(f"Invalid status: {args.status}. Allowed: {', '.join(sorted(VALID_STATUSES))}")
        sys.exit(1)
    if args.kind not in VALID_KINDS:
        print(f"Invalid kind: {args.kind}. Allowed: {', '.join(sorted(VALID_KINDS))}")
        sys.exit(1)

    company = args.company.strip()
    title = args.title.strip()
    if not company or not title:
        print("--company and --title cannot be blank.")
        sys.exit(1)

    # `kind` and `applied_at` are what this command exists to record, so a
    # pre-0022 database gets a clear instruction rather than a row that quietly
    # loses half of what was typed. (Unlike source_board, which the DAL is happy
    # to degrade on, these are not provenance extras.)
    missing = [c for c in ("kind", "applied_at") if not _vacancy_has_column(c)]
    if missing:
        print(
            f"This database has no {' or '.join(missing)} column yet — "
            "`vac add` needs migration 0022."
        )
        print("  Run it first: python3 scripts/migrate.py")
        sys.exit(1)

    applied_at = _parse_day(args.applied_at, "--applied-at")
    status_at = _parse_day(args.status_at, "--status-at") or applied_at
    seen = applied_at or _today()

    # Active, not candidate: he applied there, so the company is not awaiting a
    # review — and a candidate company's roles are hidden from the board.
    #
    # ensure_company only sets a status when it CREATES the row, so an
    # organisation already on file as 'inactive' or 'candidate' keeps that
    # status and the company filter then hides the application that was just
    # recorded. Activating explicitly is the second half of the same intent.
    company_id = ensure_company(company, status="active")
    reactivated = activate_company(company_id, "applied via vac add")

    data = {
        "company_id": company_id,
        "title": title,
        "status": args.status,
        "first_seen": seen,
        "last_seen": seen,
        # Never scored, and never will be: nothing here came from the scorer.
        # data_prep keeps an application on the dashboard at any score,
        # including none, precisely so these rows are visible.
        "llm_score": None,
        "source_board": MANUAL_SOURCE_BOARD,
        "kind": args.kind,
        "locations": [{"url": args.url}] if args.url else [],
    }
    if status_at:
        data["status_updated_at"] = status_at
    if applied_at:
        data["applied_at"] = applied_at
    if args.note:
        data["triage"] = {"note": args.note}

    dedup_hash = make_vacancy_id(company, title)
    uid = upsert_vacancy(dedup_hash, data)
    get_conn().commit()

    when = f" sent {applied_at}" if applied_at else ""
    print(f"ok: {company} — {title[:50]} [{args.kind}] → {args.status}{when}")
    print(f"    id {_short_id(uid)}   (re-run with the same company and title to edit it)")
    if reactivated:
        # Said out loud: this changed a company he had previously set aside,
        # and a silent status flip is the kind of thing that is discovered
        # months later while wondering why a company came back.
        print(f"    note: {company} was not active — set to active so this stays visible")


def _today() -> str:
    from datetime import date as _date

    return _date.today().isoformat()


def cmd_open(args):
    vacancies = load_vacancies(
        light=False,
        include_candidate_companies=True,
        include_inactive_companies=True,
    )
    uid = _resolve_uid(args.id, vacancies)
    if not uid:
        print(f"Not found: {args.id}")
        sys.exit(1)
    v = vacancies[uid]
    locs = v.get("locations") or []
    url = next((loc.get("url") for loc in locs if loc.get("url")), None)
    if not url:
        url = v.get("org_url")
    if not url:
        print("No link.")
        sys.exit(1)
    print(f"Opening: {url}")
    if sys.platform == "darwin":
        subprocess.run(["open", url])
    elif os.name == "nt":
        os.startfile(url)
    else:
        subprocess.run(["xdg-open", url])


def cmd_report(args):
    """Dispatch `vac report <add|list>`."""
    import reports as reports_mod

    if not reports_mod.table_ready():
        print("This database has no `report` table yet — reports need migration 0023.")
        print("  Run it first: python3 scripts/migrate.py")
        sys.exit(1)

    if args.report_cmd == "add":
        return _report_add(args, reports_mod)
    return _report_list(reports_mod)


def _report_add(args, reports_mod):
    """Store one markdown file as a report.

    The slug comes from the filename, so re-running on an edited file UPDATES
    that report instead of forking a second copy — the same rule `vac add` uses
    for an application. --title and --kind override what the file implies; both
    are guesses (the first H1, the directory) and a guess must be correctable.
    """
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}")
        sys.exit(1)
    if path.suffix.lower() not in (".md", ".markdown"):
        print(f"{path} is not a markdown file.")
        sys.exit(1)

    try:
        report = reports_mod.read_report_file(path)
    except ValueError as exc:
        print(str(exc))
        sys.exit(1)

    if args.title:
        report["title"] = args.title.strip()
    if args.kind:
        if args.kind not in VALID_REPORT_KINDS:
            print(f"Invalid kind: {args.kind}. Allowed: {', '.join(sorted(VALID_REPORT_KINDS))}")
            sys.exit(1)
        report["kind"] = args.kind

    existing = reports_mod.get_report(report["slug"])
    reports_mod.upsert_report(**report)
    get_conn().commit()

    verb = "updated" if existing else "stored"
    words = len(report["body_md"].split())
    print(f"ok: {verb} \u201c{report['title']}\u201d [{report['kind']}] \u2014 {words} words")
    print(f"    slug {report['slug']}   from {report['source_path']}")


def _report_list(reports_mod):
    rows = reports_mod.list_reports()
    if not rows:
        print("No reports stored yet.")
        print("  Add one: python3 scripts/vac.py report add path/to/report.md")
        return

    width = _term_width()
    slug_w = 28
    kind_w = 9
    title_w = max(20, width - slug_w - kind_w - 14 - 6)

    print(
        _ansi(
            "1",
            f"{'Slug':<{slug_w}} {'Kind':<{kind_w}} {'Title':<{title_w}} {'Updated':<12}",
        )
    )
    print("\u2500" * min(width, slug_w + kind_w + title_w + 14))
    for r in rows:
        updated = r.get("updated_at")
        updated_s = str(updated)[:10] if updated else "\u2014"
        print(
            f"{(r['slug'] or '')[:slug_w]:<{slug_w}} "
            f"{(r['kind'] or '')[:kind_w]:<{kind_w}} "
            f"{(r['title'] or '')[:title_w]:<{title_w}} "
            f"{updated_s:<12}"
        )
    print()
    print(f"Total: {len(rows)}")


def cmd_companies(args):
    from database_supabase import get_conn as gc

    conn = gc()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.canonical_name, c.tier, c.alignment_score, c.status,
               COUNT(v.id) FILTER (WHERE v.status != 'unseen') AS triaged,
               COUNT(v.id) AS total
        FROM company c
        LEFT JOIN vacancy v ON v.company_id = c.id
        WHERE (%s IS NULL OR c.status = %s)
        GROUP BY c.id, c.canonical_name, c.tier, c.alignment_score, c.status
        ORDER BY c.alignment_score DESC NULLS LAST, c.canonical_name
        LIMIT %s
    """,
        (args.status, args.status, args.limit),
    )
    rows = cur.fetchall()
    cur.close()

    if not rows:
        if args.status:
            print(
                f"No companies with status '{args.status}'. "
                "Drop --status to see all tracked companies."
            )
        else:
            print("No companies tracked yet.")
            print("  Run /jobs-new to discover your first companies,")
            print("  or /jobs-add to add a company by name.")
        return

    print(_ansi("1", f"{'Company':<32} {'Tier':<5} {'Align':>5}  {'Status':<10} {'Triaged':>10}"))
    print("─" * 80)
    for name, tier, align, status, triaged, total in rows:
        align_s = f"{align:>3}" if align is not None else "  —"
        triaged_s = f"{triaged}/{total}"
        print(
            f"{(name or '')[:32]:<32} {tier or '—':<5} {align_s}  {_color_status(status):<19} {triaged_s:>10}"
        )
    print()
    print(f"Total: {len(rows)}")


def cmd_contact(args):
    """Dispatch `vac contact <import|add|list>`."""
    import contacts as contacts_mod

    if not contacts_mod.table_ready():
        print("This database has no `contact` table yet — networking needs migration 0024.")
        print("  Run it first: python3 scripts/migrate.py")
        sys.exit(1)

    if args.contact_cmd == "import":
        return _contact_import(args, contacts_mod)
    if args.contact_cmd == "add":
        return _contact_add(args, contacts_mod)
    return _contact_list(args, contacts_mod)


def _contact_import(args, contacts_mod):
    """Import a sweep CSV of people.

    Keyed on (name, group), so re-running on a corrected file UPDATES those
    people rather than forking a second copy of the list — the same rule
    `vac add` and `vac report add` use.
    """
    path = Path(args.path)
    if not path.exists():
        print(f"No such file: {path}")
        sys.exit(1)

    try:
        result = contacts_mod.import_csv(
            path,
            group=args.group,
            source_path=args.source,
            derive_region=args.derive_region,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc))
        sys.exit(1)

    get_conn().commit()

    group = contacts_mod.normalise_group(args.group)
    parts = [f"{result['added']} added", f"{result['updated']} updated"]
    if result["skipped"]:
        # Named rather than silent: a skipped row is usually a blank line, but
        # it can be a row whose name column was empty by mistake.
        parts.append(f"{result['skipped']} skipped (no name)")
    print(f"ok: {path.name} -> [{group}] " + ", ".join(parts))


def _contact_add(args, contacts_mod):
    """Add or edit one person by hand."""
    if args.status not in VALID_CONTACT_STATUSES:
        print(
            f"Invalid status: {args.status}. "
            f"Allowed: {', '.join(sorted(VALID_CONTACT_STATUSES))}"
        )
        sys.exit(1)

    name = (args.name or "").strip()
    if not name:
        print("--name cannot be blank.")
        sys.exit(1)

    channels = {}
    for channel in CONTACT_CHANNELS:
        value = getattr(args, f"ch_{channel}", None)
        if value and value.strip():
            channels[channel] = value.strip()

    contact = {
        "name": name,
        "name_local": args.name_local or "",
        "city": args.city or "",
        "org": args.org or "",
        "role": args.role or "",
        "why_matters": args.why_matters or "",
        "channels": channels,
        "group": args.group,
        "status": args.status,
        "last_active": args.last_active or "",
        "opener": args.opener or "",
        "notes": args.notes or "",
        "source_path": args.source_path or "",
    }

    before = {(c["name"], c["group"]) for c in contacts_mod.list_contacts()}
    group = contacts_mod.normalise_group(args.group)
    contacts_mod.upsert_contact(contact)
    get_conn().commit()

    verb = "updated" if (name, group) in before else "stored"
    where = f" at {args.org}" if args.org else ""
    reach = ", ".join(sorted(channels)) if channels else "no channel on file"
    print(f"ok: {verb} {name}{where} [{group}] -> {args.status}")
    print(f"    {reach}   (re-run with the same name and group to edit)")


def _contact_list(args, contacts_mod):
    rows = contacts_mod.list_contacts(status=args.status, group=args.group)
    if not rows:
        which = " matching that filter" if (args.status or args.group) else ""
        print(f"No contacts{which}.")
        print("  Import a list: python3 scripts/vac.py contact import path/to/people.csv")
        return

    width = _term_width()
    name_w = 24
    status_w = 10
    group_w = 16
    org_w = max(16, width - name_w - status_w - group_w - 6)

    print(
        _ansi(
            "1",
            f"{'Name':<{name_w}} {'Org':<{org_w}} {'Group':<{group_w}} {'Status':<{status_w}}",
        )
    )
    print("\u2500" * min(width, name_w + org_w + group_w + status_w + 3))
    for c in rows:
        print(
            f"{(c['name'] or '')[:name_w]:<{name_w}} "
            f"{(c['org'] or '\u2014')[:org_w]:<{org_w}} "
            f"{(c['group'] or '')[:group_w]:<{group_w}} "
            f"{(c['status'] or ''):<{status_w}}"
        )

    counts = contacts_mod.count_by_status(rows)
    # Every status, including the zeroes: this list is a queue, and "0 replied"
    # is the number that says a sweep has not paid off yet.
    summary = " \u00b7 ".join(f"{counts[s]} {s}" for s in CONTACT_STATUSES)
    print()
    print(f"Total: {len(rows)}  \u2014  {summary}")


def cmd_publish(args):
    """Rewrite the dashboard snapshot from what the database already holds.

    The dashboard reads a single baked snapshot, not the tables directly, so
    anything that changes what the snapshot CONTAINS — a status set from the
    terminal, a company reactivated, a new translation baked into config.i18n —
    is invisible until the snapshot is rewritten. Until now the only things that
    did that were a fetch and a scoring run, both of which cost time and LLM
    calls to produce a result neither of them needed.

    This does the last step alone: read, assemble, write. No network, no
    scorer, no fetch.

    It has to run where a Postgres driver exists. On the server there is none
    (and the app user does not own the tables), so this is a LAPTOP command,
    over the tunnel — the same place `vac add` and `vac report add` run.
    """
    from report import generate_dashboard

    generate_dashboard()
    get_conn().commit()

    counts = {}
    cur = get_conn().cursor()
    for table in ("vacancy", "company", "report", "contact"):
        try:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
        except Exception:
            # An optional table this database has not migrated to yet: the
            # snapshot is still valid, so say nothing rather than fail.
            get_conn().rollback()
    cur.close()

    print("ok: dashboard snapshot rewritten")
    if counts:
        print("    " + ", ".join(f"{n} {name}" for name, n in counts.items()))
    print("    the dashboard shows this on its next load (no restart needed)")


def main():
    parser = build_parser(
        {
            "list": cmd_list,
            "show": cmd_show,
            "mark": cmd_mark,
            "add": cmd_add,
            "open": cmd_open,
            "report": cmd_report,
            "publish": cmd_publish,
            "contact": cmd_contact,
            "companies": cmd_companies,
        }
    )
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
