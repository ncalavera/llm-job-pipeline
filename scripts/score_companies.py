#!/usr/bin/env python3
"""Company scoring — local subagent + API backends, deterministic LLM input.

Usage:
    score_companies.py --local --limit 5          # subagent scoring (default)
    score_companies.py --api --limit 20           # Anthropic API
"""

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TOKENS = 2000
BILLING_ABORT_THRESHOLD = 5

# Default parallelism for the API backend.
MAX_CONCURRENT = 3


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    parser = argparse.ArgumentParser(description="Company scoring")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--api", action="store_true", help="Anthropic API")
    mode.add_argument("--local", action="store_true", help="JSON → stdout for subagents")
    mode.add_argument("--save", action="store_true", help="stdin JSON → DB (internal)")

    parser.add_argument("--limit", type=int)
    parser.add_argument("--company", type=str, help="Specific companies (comma-separated)")
    parser.add_argument("--model", type=str)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-workers", type=int, default=MAX_CONCURRENT)
    parser.add_argument("--no-auto-review", action="store_true",
                        help="Skip auto_review_candidates() — scored companies stay 'candidate'")
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help
if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

# Redirect stdout → stderr during imports so db_conn diagnostics don't pollute
# JSON output in --prepare mode
_real_stdout = sys.stdout
if "--local" in sys.argv:
    sys.stdout = sys.stderr

from prompts import (  # noqa: E402
    COMPANY_SCORING_PROMPT,
    COMPANY_SCORING_USER_TEMPLATE,
    CUSTOM_BOOST_FIELD,
    CUSTOM_BOOST_KEYS,
)

sys.stdout = _real_stdout

# Placeholder values to sanitize from LLM output
_PLACEHOLDER_VALUES = {
    "not specified", "n/a", "unknown", "year not specified",
    "na", "none",
}

# Strategy sections to extract from strategy.md
_STRATEGY_SECTIONS = [
    "## Core Identity",
    "## Red Flags",
    "## Dream Job Letter",
    "## Three-Tier Classification",
    "## Key Strengths",
    "## MPA/MPP Application Strategy",
]


# ---------------------------------------------------------------------------
# Inline utilities
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Parse JSON from LLM response, handling fences, nested objects."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Greedy match for outermost braces (company responses have nested objects)
    brace = re.search(r'\{.*\}', text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass
    return {"error": "JSON parse failed", "raw": text[:500]}


def _clean_placeholders(about: dict) -> dict:
    """Remove placeholder values from LLM output."""
    for key in ("founded_year", "employee_count", "funding_status", "hq_location", "sector"):
        val = about.get(key, "")
        if isinstance(val, str) and val.strip().lower() in _PLACEHOLDER_VALUES:
            about[key] = ""
    return about


def _get_main_repo_root() -> Path:
    """Get the MAIN repo root (not worktree) for cache persistence."""
    try:
        git_common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return Path(git_common).parent
    except Exception:
        return Path(__file__).resolve().parent.parent


_MAIN_ROOT = _get_main_repo_root()
SCRAPE_CACHE_PATH = _MAIN_ROOT / ".firecrawl" / "scrape_cache.json"
STRATEGY_PATH = _MAIN_ROOT / "strategy.md"


def _load_strategy_context() -> str:
    """Load relevant sections from strategy.md for scoring prompt."""
    if not STRATEGY_PATH.exists():
        return ""
    text = STRATEGY_PATH.read_text(encoding="utf-8")
    sections = []
    for header in _STRATEGY_SECTIONS:
        pattern = re.compile(
            rf"({re.escape(header)}.*?)(?=\n---|\n## |\Z)",
            re.DOTALL,
        )
        m = pattern.search(text)
        if m:
            sections.append(m.group(1).strip())
    return "\n\n".join(sections)


def _load_scrape_cache() -> dict[str, tuple[str, str]]:
    """Load cached scrape results. Returns {name: (url, markdown)}."""
    if not SCRAPE_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(SCRAPE_CACHE_PATH.read_text(encoding="utf-8"))
        return {k: (v["url"], v["markdown"]) for k, v in data.items()}
    except Exception:
        return {}


def _build_csv_context(company: dict) -> str:
    """Build metadata context string from company DB row."""
    parts = []
    if company.get("category"):
        parts.append(f"Category: {company['category']}")
    if company.get("tier"):
        parts.append(f"Tier (user-assigned): {company['tier']}")
    if company.get("experience_match"):
        parts.append(f"Experience Match: {company['experience_match']}/5")
    if company.get("personal_interest"):
        parts.append(f"Personal Interest: {company['personal_interest']}/5")
    if company.get("user_comments"):
        parts.append(f"User Notes: {company['user_comments']}")
    if company.get("product"):
        parts.append(f"Product: {company['product']}")
    if not parts:
        return ""
    return "Additional context:\n" + "\n".join(parts)


def _build_user_msg(company: dict, scrape_cache: dict, strategy_context: str) -> str | None:
    """Build user message — identical for all backends.

    Returns None if company has no cached markdown (needs fetch_companies.py first).
    """
    name = company["canonical_name"]
    url = (company.get("website") or "").strip()

    if name not in scrape_cache:
        return None

    cached_url, markdown = scrape_cache[name]
    if not markdown or len(markdown) < 50:
        return None

    if len(markdown) > 10000:
        markdown = markdown[:10000] + "\n\n[Content truncated]"

    csv_context = _build_csv_context(company)

    return COMPANY_SCORING_USER_TEMPLATE.format(
        name=name,
        url=url or cached_url,
        csv_context=csv_context,
        content=markdown,
    )


# ---------------------------------------------------------------------------
# System prompt (with strategy context injected)
# ---------------------------------------------------------------------------

def _get_system_prompt() -> str:
    """Build system prompt with strategy context."""
    strategy = _load_strategy_context()
    if not strategy:
        print("WARNING: strategy.md not found or empty", file=sys.stderr)
    return COMPANY_SCORING_PROMPT.format(strategy_context=strategy)


# ---------------------------------------------------------------------------
# API backend
# ---------------------------------------------------------------------------

def _score_api(client, model: str, system_prompt: str, user_msg: str) -> dict:
    """Score via Anthropic API with exponential backoff."""
    t0 = time.time()
    delays = [5, 15, 45]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            time.sleep(delay)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            latency = time.time() - t0
            text = response.content[0].text.strip()
            parsed = _parse_json(text)
            parsed["latency"] = round(latency, 2)
            parsed["model"] = model
            return parsed
        except Exception as e:
            err_str = str(e)
            is_overload = "529" in err_str or "overloaded" in err_str.lower()
            if is_overload and attempt < len(delays):
                continue
            return {"error": err_str, "latency": round(time.time() - t0, 2)}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_companies(*, company_filter=None, limit=None):
    """Load candidate companies needing scoring (have website, no alignment_score).

    Returns list of company dicts with all metadata.
    """
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()

    if company_filter:
        names = [n.strip() for n in company_filter.split(",")]
        cur.execute("""
            SELECT id, canonical_name, website, category, product, tier,
                   user_comments, experience_match, personal_interest
            FROM company
            WHERE canonical_name = ANY(%s)
              AND website IS NOT NULL AND website != ''
            ORDER BY canonical_name
        """, (names,))
    else:
        cur.execute("""
            SELECT id, canonical_name, website, category, product, tier,
                   user_comments, experience_match, personal_interest
            FROM company
            WHERE status = 'candidate'
              AND alignment_score IS NULL
              AND website IS NOT NULL AND website != ''
            ORDER BY canonical_name
        """)

    columns = [desc[0] for desc in cur.description]
    rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    cur.close()
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------
# Shared: extract enrichment from LLM result
# ---------------------------------------------------------------------------

def _read_custom_boost(mission: dict):
    """Return the custom-boost value from a mission_fit dict, or None.

    Reads the configured key first (CUSTOM_BOOST_FIELD, e.g.
    'career_narrative_boost') and falls back to the legacy 'mpa_narrative_boost'
    for older payloads. Non-numeric values are ignored.
    """
    if not isinstance(mission, dict):
        return None
    for key in CUSTOM_BOOST_KEYS:
        val = mission.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return val
    return None


def _extract_enrichment(result: dict) -> dict | None:
    """Extract about + mission_fit from parsed LLM response.

    Returns enrichment dict or None on failure.
    """
    about = result.get("about", {})
    mission = result.get("mission_fit", {})
    alignment = (mission or {}).get("alignment_score")

    if alignment is None:
        alignment = result.get("alignment_score")

    if alignment is None:
        return None

    if not isinstance(alignment, (int, float)) or not (0 <= alignment <= 100):
        return None

    now = datetime.now().isoformat()

    # Clean about data
    about_data = None
    if about and about.get("description"):
        about_data = _clean_placeholders(about)
        about_data["about_enriched_at"] = now
        about_data["about_source"] = "llm_from_markdown"

    # Build mission_fit. The custom-boost key is user-configurable
    # (CUSTOM_BOOST_FIELD), so allow both the configured + legacy names plus
    # their *_reasoning siblings.
    mission_data = mission if isinstance(mission, dict) else {}
    boost_keys = set(CUSTOM_BOOST_KEYS) | {f"{k}_reasoning" for k in CUSTOM_BOOST_KEYS}
    keep_keys = (
        "alignment_score", "alignment_label", "strengths", "risks",
        "approach", "experience_match_reasoning", "mission_verdict",
    ) + tuple(boost_keys)
    mission_fit = {
        k: mission_data[k]
        for k in keep_keys
        if k in mission_data and mission_data[k] is not None
    }
    mission_fit["mission_fit_enriched_at"] = now

    return {
        "about": about_data,
        "mission_fit": mission_fit,
        "alignment_score": alignment,
    }


# ---------------------------------------------------------------------------
# Mode: --local
# ---------------------------------------------------------------------------

def cmd_local(args):
    """Output JSON to stdout for subagent scoring."""
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr

    companies = _load_companies(company_filter=args.company, limit=args.limit)
    scrape_cache = _load_scrape_cache()
    system_prompt = _get_system_prompt()

    print(
        f"Loaded {len(companies)} candidates, {len(scrape_cache)} in scrape cache",
        file=sys.stderr,
    )

    if not companies:
        sys.stdout = _real_stdout
        json.dump([], _real_stdout, ensure_ascii=False)
        return

    strategy_context = _load_strategy_context()
    output = []
    skipped = 0
    for c in companies:
        user_msg = _build_user_msg(c, scrape_cache, strategy_context)
        if user_msg is None:
            skipped += 1
            continue

        output.append({
            "payload_kind": "company",
            "id": str(c["id"]),
            "canonical_name": c["canonical_name"],
            "url": (c.get("website") or "").strip(),
            "system_prompt": system_prompt,
            "user_msg": user_msg,
        })

    if skipped:
        print(f"  Skipped {skipped} companies (not in scrape cache)", file=sys.stderr)
    print(f"Prepared {len(output)} companies for scoring", file=sys.stderr)

    sys.stdout = _real_stdout
    json.dump(output, _real_stdout, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Mode: --save
# ---------------------------------------------------------------------------

def cmd_save(_args):
    """Read scored results from stdin JSON, save to DB."""
    from db_conn import get_conn
    from database_supabase import auto_review_candidates
    from psycopg2.extras import Json

    data = json.load(sys.stdin)
    if not data:
        print("No results to save.")
        return

    conn = get_conn()
    saved = 0
    errors = 0

    for entry in data:
        name = entry.get("canonical_name", "?")

        if entry.get("payload_kind") != "company":
            print(
                f"ERROR: wrong payload_kind={entry.get('payload_kind')!r}, "
                f"expected 'company'. Skipping {name}",
                file=sys.stderr,
            )
            errors += 1
            continue

        enrichment = entry.get("enrichment")
        if not enrichment:
            print(f"ERROR: missing 'enrichment' for {name}", file=sys.stderr)
            errors += 1
            continue

        # Validate alignment_score
        alignment = enrichment.get("alignment_score")
        if alignment is None:
            # Try nested mission_fit
            mission = enrichment.get("mission_fit", {})
            alignment = mission.get("alignment_score") if mission else None

        if alignment is None:
            print(f"SKIP {name}: no alignment_score in enrichment", file=sys.stderr)
            errors += 1
            continue

        if not isinstance(alignment, (int, float)) or not (0 <= alignment <= 100):
            print(f"SKIP {name}: invalid alignment_score={alignment}", file=sys.stderr)
            errors += 1
            continue

        try:
            now = datetime.now().isoformat()
            company_id = entry["id"]
            cur = conn.cursor()

            updates = ["enriched_at = now()"]
            vals = []

            # About data
            about = enrichment.get("about")
            if about and about.get("description"):
                about = _clean_placeholders(about)
                about["about_enriched_at"] = now
                about["about_source"] = "llm_subagent"
                updates.append("about = %s")
                vals.append(Json(about))

                # Mirror top-level columns read by the dashboard. Only fill
                # when source is empty so we don't clobber manually-curated values.
                sector = about.get("sector")
                if sector:
                    updates.append("category = COALESCE(NULLIF(category, ''), %s)")
                    vals.append(sector)

                office_parts = []
                hq = about.get("hq_location")
                if hq:
                    office_parts.append(hq)
                offs = about.get("office_locations") or []
                if isinstance(offs, list):
                    extra = [o for o in offs if o and o != hq]
                    office_parts.extend(extra)
                if office_parts:
                    offices_str = ", ".join(dict.fromkeys(office_parts))
                    updates.append("offices = COALESCE(NULLIF(offices, ''), %s)")
                    vals.append(offices_str)

            # Mission fit
            mission = enrichment.get("mission_fit", {})
            if isinstance(mission, dict):
                mission["mission_fit_enriched_at"] = now
                mission["mission_fit_model"] = "claude-sonnet-4-6"
                updates.append("mission_fit = %s")
                vals.append(Json(mission))

            updates.append("alignment_score = %s")
            vals.append(alignment)

            from database_supabase import calculate_company_tier
            boost = _read_custom_boost(mission)
            tier, _ = calculate_company_tier(alignment, boost)
            if tier is not None:
                updates.append("tier = %s")
                vals.append(tier)

            vals.append(company_id)

            cur.execute(
                f"UPDATE company SET {', '.join(updates)} WHERE id = %s",
                vals,
            )
            cur.close()
            saved += 1

        except Exception as e:
            print(f"ERROR saving {name}: {e}", file=sys.stderr)
            errors += 1
            conn.rollback()
            continue

    conn.commit()

    # Auto-review scored companies
    review = {"approved": [], "rejected": [], "pending": []}
    if saved > 0 and not getattr(_args, "no_auto_review", False):
        review = auto_review_candidates()

    approved = len(review.get("approved", []))
    rejected = len(review.get("rejected", []))
    print(
        f"Saved {saved}/{len(data)} enrichments ({errors} errors). "
        f"Auto-review: {approved} approved, {rejected} rejected."
    )


# ---------------------------------------------------------------------------
# Mode: --api
# ---------------------------------------------------------------------------

def cmd_api(args):
    """Score companies via Anthropic API."""
    import anthropic

    client = anthropic.Anthropic()
    model = args.model or "claude-sonnet-4-6"

    companies = _load_companies(company_filter=args.company, limit=args.limit)
    scrape_cache = _load_scrape_cache()
    system_prompt = _get_system_prompt()
    strategy_context = _load_strategy_context()

    if not companies:
        print("No companies to score.")
        return

    from database_supabase import save_company_enrichment, auto_review_candidates, get_conn
    from psycopg2.extras import Json

    conn = get_conn()
    print(f"Scoring {len(companies)} companies with {model}")

    errors = 0
    scored = 0
    consecutive_errors = 0

    for i, c in enumerate(companies, 1):
        name = c["canonical_name"]
        user_msg = _build_user_msg(c, scrape_cache, strategy_context)
        if user_msg is None:
            print(f"  [{i}/{len(companies)}] {name:40s} SKIP: not in scrape cache")
            continue

        print(f"  [{i}/{len(companies)}] {name:40s}", end="", flush=True)

        result = _score_api(client, model, system_prompt, user_msg)

        if result.get("error"):
            errors += 1
            consecutive_errors += 1
            print(f"  ERR: {result['error'][:80]}")
            if consecutive_errors >= BILLING_ABORT_THRESHOLD:
                conn.commit()
                print(f"\n  {consecutive_errors} consecutive errors — stopping.")
                return
            continue

        enrichment = _extract_enrichment(result)
        if enrichment is None:
            errors += 1
            consecutive_errors += 1
            raw = json.dumps(result, ensure_ascii=False)[:120]
            print(f"  ERR: no alignment_score: {raw}")
            continue

        consecutive_errors = 0
        alignment = enrichment["alignment_score"]
        label = enrichment["mission_fit"].get("alignment_label", "")
        print(f"  -> {alignment:3.0f} ({label})  ({result['latency']:.1f}s)")

        enrichment["mission_fit"]["mission_fit_model"] = model
        save_company_enrichment(
            name,
            about=enrichment["about"],
            mission_fit=enrichment["mission_fit"],
            alignment_score=alignment,
        )
        conn.commit()
        scored += 1

    conn.commit()
    print(f"\nDone! Scored {scored} companies. Errors: {errors}")

    if scored > 0 and not args.no_auto_review:
        review = auto_review_candidates()
        print(
            f"Auto-review: {len(review.get('approved', []))} approved, "
            f"{len(review.get('rejected', []))} rejected"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = build_parser().parse_args()

    if args.api:
        cmd_api(args)
    elif args.local:
        cmd_local(args)
    elif args.save:
        cmd_save(args)


if __name__ == "__main__":
    main()
