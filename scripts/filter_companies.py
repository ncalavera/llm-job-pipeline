#!/usr/bin/env python3
"""Pre-filter candidate companies by name/type patterns.

Removes structural junk (aggregators, board duplicates, government,
universities, animal welfare) before expensive enrichment steps.

Usage:
    python3 scripts/filter_companies.py                 # Analyze + markdown report
    python3 scripts/filter_companies.py --apply          # Execute deletions (set inactive)
    python3 scripts/filter_companies.py --limit N        # Limit to N companies
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_conn import get_conn

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Classification patterns
# ---------------------------------------------------------------------------

def _check_aggregator(name: str) -> str | None:
    """Pattern 1: Aggregators — '[via ...]', 'Various ...'"""
    if name.startswith("[via ") or name.startswith("Various "):
        return "aggregator"
    return None


_BOARD_SLUGS = {
    "80k_hours", "80000hours", "80000_hours", "80k",
    "fast_forward", "ffwd",
    "idealist", "reliefweb", "devex",
}


def _check_board_duplicate(name: str) -> str | None:
    """Pattern 2: Board duplicates (lowercase slug matches known boards)."""
    slug = name.lower().replace(" ", "_").replace("-", "_")
    if slug in _BOARD_SLUGS:
        return "board duplicate"
    return None


_GOVT_PREFIXES = [
    "US Government,", "UK Government,", "California Dept", "California Department",
    "New York State,", "Anser (US government",
]
_GOVT_EXCEPTIONS = ["AI Security Institute"]


def _check_government(name: str) -> str | None:
    """Pattern 3: Government structures (with AI Security Institute exception)."""
    for prefix in _GOVT_PREFIXES:
        if name.startswith(prefix):
            if any(exc in name for exc in _GOVT_EXCEPTIONS):
                return None
            return "government"
    return None


_UNIVERSITY_PATTERNS = [
    "University of", "MIT,", "MIT ", "Massachusetts Institute of Technology",
    "Yale", "Cornell", "Oxford",
    "Rutgers", "Northeastern", "Stanford", "Harvard", "Princeton",
    "Columbia University", "Georgetown", "NYU", "UC Berkeley",
    "Carnegie Mellon", "Johns Hopkins",
]


def _check_university(name: str) -> str | None:
    """Pattern 4: Universities and academic institutions."""
    for pattern in _UNIVERSITY_PATTERNS:
        if pattern in name:
            return "university"
    return None


_ANIMAL_WELFARE = {
    "Advocates for Animals", "AIxAnimals Funding", "Animal Law Foundation",
    "Animal Legal Defense Fund", "Sentient Futures", "Shrimp Welfare Project",
    "We Animals Media",
}


def _check_animal_welfare(name: str) -> str | None:
    """Pattern 5: Animal welfare organizations (explicit list)."""
    if name in _ANIMAL_WELFARE:
        return "animal welfare"
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_CHECKS = [
    _check_aggregator,
    _check_board_duplicate,
    _check_government,
    _check_university,
    _check_animal_welfare,
]


def classify_companies() -> list[tuple[dict, str, str]]:
    """Classify candidate companies without alignment_score.

    Returns list of (company_row, "DELETE"|"READY", reason).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, canonical_name, status, website, careers_url
        FROM company
        WHERE status = 'candidate' AND alignment_score IS NULL
        ORDER BY canonical_name
    """)
    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()

    results = []
    for row in rows:
        name = row["canonical_name"]
        matched = None
        for check in _CHECKS:
            matched = check(name)
            if matched:
                break

        if matched:
            results.append((row, "DELETE", matched))
        else:
            has_url = bool(row.get("website") or row.get("careers_url"))
            results.append((row, "READY", "has URL" if has_url else "needs URL discovery"))

    return results


# ---------------------------------------------------------------------------
# Report generation (markdown)
# ---------------------------------------------------------------------------

def generate_report(classified: list[tuple[dict, str, str]]) -> str:
    """Generate markdown report."""
    deletes = [(r, reason) for r, action, reason in classified if action == "DELETE"]
    readies = [(r, reason) for r, action, reason in classified if action == "READY"]

    lines = [
        "# Company Pre-Filter Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Total candidates:** {len(classified)}",
        f"**DELETE:** {len(deletes)} | **READY:** {len(readies)}",
        "",
    ]

    # DELETE section — group by subcategory
    categories: dict[str, list] = {}
    for row, reason in deletes:
        categories.setdefault(reason, []).append(row)

    lines.append("## DELETE")
    lines.append("")
    for cat in sorted(categories):
        items = categories[cat]
        lines.append(f"### {cat.title()} ({len(items)})")
        lines.append("")
        for row in items:
            lines.append(f"- {row['canonical_name']}")
        lines.append("")

    # READY section
    has_url = [(r, reason) for r, reason in readies if reason == "has URL"]
    needs_url = [(r, reason) for r, reason in readies if reason == "needs URL discovery"]

    lines.append(f"## READY ({len(readies)})")
    lines.append("")
    lines.append(f"**Has URL:** {len(has_url)} | **Needs URL discovery:** {len(needs_url)}")
    lines.append("")
    for row, reason in readies:
        marker = "+" if reason == "has URL" else "?"
        lines.append(f"- [{marker}] {row['canonical_name']}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------

def apply_deletions(classified: list[tuple[dict, str, str]]):
    """Set DELETE companies to inactive with status_reason. Saves snapshot first.

    SAFETY: Only archives pattern-matched companies (DELETE action).
    READY companies are never touched — they must go through enrichment → user review.
    """
    deletes = [(r, reason) for r, action, reason in classified if action == "DELETE"]
    readies = [(r, reason) for r, action, reason in classified if action == "READY"]
    if not deletes:
        print("No companies to delete.")
        if readies:
            print(f"  {len(readies)} READY companies need enrichment before review.")
        return

    # Save JSON snapshot (safety net)
    snapshot_dir = PROJECT_ROOT / "companies" / "archive"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    snapshot_path = snapshot_dir / f"company_filter_{ts}.json"

    snapshot = {
        "archived_at": datetime.now().isoformat(),
        "source": "filter_companies.py --apply",
        "count": len(deletes),
        "companies": [
            {
                "id": str(row["id"]),
                "canonical_name": row["canonical_name"],
                "reason": reason,
            }
            for row, reason in deletes
        ],
    }
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Snapshot saved: {snapshot_path.name}")

    # Update DB
    conn = get_conn()
    cur = conn.cursor()
    for row, reason in deletes:
        cur.execute(
            "UPDATE company SET status = 'inactive', status_reason = %s WHERE id = %s AND status = 'candidate'",
            (f"pre-filter: {reason}", row["id"]),
        )
    conn.commit()
    cur.close()
    print(f"Rejected {len(deletes)} companies (status='inactive')")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pre-filter candidate companies")
    parser.add_argument("--apply", action="store_true", help="Execute deletions")
    parser.add_argument("--limit", type=int, default=0, help="Limit companies")
    args = parser.parse_args()

    classified = classify_companies()
    if args.limit:
        classified = classified[: args.limit]

    if args.apply:
        if args.limit:
            print(f"WARNING: --limit {args.limit} active — only first {args.limit} companies processed")
        apply_deletions(classified)
    else:
        report = generate_report(classified)
        report_path = PROJECT_ROOT / "REPORT-company-filter.md"
        report_path.write_text(report, encoding="utf-8")
        print(report)
        print(f"\nReport saved: {report_path.name}")


if __name__ == "__main__":
    main()
