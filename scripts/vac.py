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
from statuses import VALID_KINDS  # noqa: E402

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
    """A YYYY-MM-DD argument, or exit with a message naming the flag. Refusing
    an unparseable date is the point: a silently-dropped one would leave the
    Applications table showing the wrong send date with no way to notice."""
    if not value:
        return None
    from datetime import date as _date

    try:
        return _date.fromisoformat(value).isoformat()
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
    company_id = ensure_company(company, status="active")

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


def main():
    parser = build_parser(
        {
            "list": cmd_list,
            "show": cmd_show,
            "mark": cmd_mark,
            "add": cmd_add,
            "open": cmd_open,
            "companies": cmd_companies,
        }
    )
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
