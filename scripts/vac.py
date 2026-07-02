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
    get_conn,
    load_vacancies,
    update_vacancy_status,
)
from geo import geo_bucket

VALID_STATUSES = {
    "unseen",
    "liked",
    "passed",
    "to_apply",
    "to_research",
    "to_network",
    "skipped",
    "applied",
    "expiring",
    "archived",
}

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
            "open": cmd_open,
            "companies": cmd_companies,
        }
    )
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
