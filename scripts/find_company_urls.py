#!/usr/bin/env python3
"""
Find official website URLs for board companies using Firecrawl search.
Adds found companies to Supabase for subsequent enrichment.

Usage:
    python3 scripts/find_company_urls.py [--dry-run] [--limit N]
"""

import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_parser():
    """Construct the CLI parser. Defined before the heavy project imports so
    ``--help`` / ``-h`` prints usage without connecting to the database or
    loading the user profile (those happen only when a real command runs)."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    return parser


# Print help and exit BEFORE importing anything that touches the DB or profile.
from cli_help import wants_help

if __name__ == "__main__" and wants_help():
    build_parser().parse_args()

from config import get_firecrawl_client

# Job board / aggregator domains to skip in search results
SKIP_DOMAINS = {
    "linkedin.com",
    "glassdoor.com",
    "indeed.com",
    "ziprecruiter.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "80000hours.org",
    "devex.com",
    "idealist.org",
    "impactsource.com",
    "jobs.ffwd.org",
    "wikipedia.org",
    "bloomberg.com",
    "crunchbase.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "youtube.com",
    "angel.co",
    "wellfound.com",
    "builtinnyc.com",
    "builtinsf.com",
    "charity.com",
    "charitynavigator.org",
    "guidestar.org",
}


def _load_known_companies() -> set[str]:
    """Return set of all canonical company names from Supabase."""
    from company_registry import _ALL_KNOWN_NAMES

    return _ALL_KNOWN_NAMES


def _load_companies_to_find() -> list[str]:
    """Load companies needing URLs.

    Two sources:
    1. Companies with high-scoring vacancies (llm_score >= 40) but no website
    2. Ghost candidates: status='candidate', no website, no alignment_score
    """
    from db_conn import get_conn
    from database_supabase import load_all_enrichment

    conn = get_conn()
    cur = conn.cursor()

    # Source 1: companies with high-scoring vacancies but no website
    cur.execute("""
        SELECT c.canonical_name
        FROM vacancy v
        JOIN company c ON v.company_id = c.id
        WHERE v.llm_score IS NOT NULL AND v.llm_score >= 40
          AND (c.website IS NULL OR c.website = '')
        GROUP BY c.canonical_name
    """)
    from_vacancies = {row[0] for row in cur.fetchall()}

    # Source 2: ghost candidates (no website, no alignment, pending review)
    cur.execute("""
        SELECT canonical_name
        FROM company
        WHERE status = 'candidate'
          AND (website IS NULL OR website = '')
          AND alignment_score IS NULL
    """)
    from_ghosts = {row[0] for row in cur.fetchall()}
    cur.close()

    all_names = from_vacancies | from_ghosts

    enrichment = load_all_enrichment()
    junk_patterns = [
        "[via ",
        " USD ",
        "New York",
        "San Francisco",
        "SAGE",
        "Individual Philanthropy",
        "Various Fellowship",
        "HUD",
        "Grants Associate",
        "Manager,",
        "Associate,",
        "Program Officer",
    ]

    result = []
    for name in all_names:
        if any(p in name for p in junk_patterns):
            continue
        has_mission = (
            name in enrichment
            and enrichment[name].get("mission_fit", {}).get("alignment_score") is not None
        )
        if not has_mission:
            result.append(name)

    return sorted(result)


def _get_search_results(search_data) -> list:
    """Extract result items from Firecrawl SearchData response."""
    if hasattr(search_data, "data") and search_data.data:
        return search_data.data
    if hasattr(search_data, "web") and search_data.web:
        return search_data.web
    if isinstance(search_data, dict):
        return search_data.get("data", []) or search_data.get("web", [])
    return []


# Org-suffix / stopword tokens that carry no identifying signal for a domain
# match — nearly every nonprofit name contains one, so matching on them would
# accept almost any homepage.
_GENERIC_ORG_TOKENS = {
    "the",
    "and",
    "for",
    "our",
    "org",
    "organization",
    "organisation",
    "foundation",
    "fund",
    "funds",
    "institute",
    "institution",
    "trust",
    "council",
    "association",
    "society",
    "network",
    "initiative",
    "project",
    "program",
    "programme",
    "center",
    "centre",
    "group",
    "global",
    "international",
    "national",
    "worldwide",
    "company",
    "inc",
    "ltd",
    "llc",
    "gmbh",
    "corp",
    "limited",
    "charity",
    "charitable",
    "nonprofit",
    "ngo",
}

# Public-suffix labels to skip when picking the registrable domain label
# (handles history.com → "history" and example.co.uk → "example").
_TLD_LABELS = {"com", "org", "net", "edu", "gov", "int", "co", "ac", "io", "ngo"}

# Connector words skipped when building the acronym: real-world org acronyms
# drop them (International Committee of the Red Cross → ICRC, not ICOTRC).
_ACRONYM_STOPWORDS = {"of", "the", "for", "and"}


def _domain_label(url: str) -> str:
    """Return the registrable domain label (e.g. history.com → 'history')."""
    host = url.split("/")[2] if "://" in url else url.split("/")[0]
    host = host.split(":")[0].lower().replace("www.", "")
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    label = parts[-2]
    # example.co.uk / example.org.uk → step back past the second-level suffix.
    if label in _TLD_LABELS and len(parts) >= 3:
        label = parts[-3]
    return label


def _domain_matches_company(company_name: str, url: str) -> bool:
    """Return True if ``url``'s domain plausibly belongs to ``company_name``.

    Simple, conservative heuristics (BUG-7) — a wrong homepage feeds the
    evidence scrape another org's content and corrupts the WANT score, so the
    default when nothing matches is to REJECT (leave the site unresolved):

    - a significant name token appears in the domain label (or vice versa);
    - the whitespace-stripped full name equals / is contained in the label
      ("80,000 Hours" → 80000hours);
    - the acronym of the name words equals the label (Children's Investment
      Fund Foundation → ciff).

    Rejects the observed failures: ALONE → history.com, 01Health → vestbee.com.
    """
    label = _domain_label(url)
    if not label:
        return False
    # NFKD-fold diacritics first: "Médecins Sans Frontières" → "medecins sans
    # frontieres", so accented org names tokenize cleanly and can match their
    # ASCII domains (msf.org).
    folded = unicodedata.normalize("NFKD", company_name).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", folded.lower())
    significant = [w for w in words if len(w) >= 3 and w not in _GENERIC_ORG_TOKENS]
    for w in significant:
        if w in label or label in w:
            return True
    # Whitespace-stripped full name equals the label ("80,000 Hours" →
    # 80000hours) or is contained in a longer label. NOT the reverse
    # (label in compressed) — that would let a generic suffix substring like
    # "foundation" match "acmehealthfoundation".
    compressed = "".join(words)
    if compressed and (compressed == label or compressed in label):
        return True
    # Acronym from the words' initials, skipping 1-char tokens (a possessive
    # "'s" — Children's → children + s — must not corrupt the acronym) and
    # connector stopwords (International Committee OF THE Red Cross → icrc).
    acronym = "".join(w[0] for w in words if len(w) >= 2 and w not in _ACRONYM_STOPWORDS)
    if len(acronym) >= 2 and acronym == label:
        return True
    return False


def _search_website(client, company_name: str) -> str | None:
    """Search for a company's official website via Firecrawl.

    Only returns a URL whose domain plausibly matches the org
    (``_domain_matches_company``). When no confident match is found the site is
    left UNRESOLVED (returns None) with a visible flag, so the evidence scrape
    skips instead of scraping a stranger's site (BUG-7).
    """
    query = f"{company_name} official website"
    try:
        results = client.search(query=query, limit=5)

        top_hit = None
        for item in _get_search_results(results):
            url = getattr(item, "url", None) or (
                item.get("url") if isinstance(item, dict) else None
            )
            if not url:
                continue
            # Skip known job boards and aggregators
            domain = url.split("/")[2] if "://" in url else ""
            domain = domain.replace("www.", "")
            if any(skip in domain for skip in SKIP_DOMAINS):
                continue
            # Normalize to root domain
            parts = url.split("/")
            root = "/".join(parts[:3])  # https://domain.com
            if top_hit is None:
                top_hit = root
            # Verify the domain actually belongs to this org before trusting it.
            if _domain_matches_company(company_name, root):
                return root

        if top_hit:
            print(
                f"    ⚠ website unresolved — needs manual check "
                f"(top hit {top_hit} did not match '{company_name}')",
                flush=True,
            )
        return None
    except Exception as e:
        print(f"    ⚠ Search error: {e}", flush=True)
        return None


def _save_found_urls(entries: list[dict]):
    """Update company website URLs in Supabase."""
    if not entries:
        return
    from db_conn import get_conn

    conn = get_conn()
    cur = conn.cursor()
    for entry in entries:
        name = entry["Company Name"]
        website = entry["Website"]
        cur.execute(
            "UPDATE company SET website = %s WHERE canonical_name = %s",
            (website, name),
        )
        if cur.rowcount == 0:
            # Company doesn't exist yet — create as candidate
            cur.execute(
                """INSERT INTO company (canonical_name, website, status, aliases)
                   VALUES (%s, %s, 'candidate', ARRAY[%s]::text[])
                   ON CONFLICT (canonical_name) DO UPDATE SET website = EXCLUDED.website""",
                (name, website, name),
            )
    conn.commit()
    cur.close()


def main():
    args = build_parser().parse_args()

    companies = _load_companies_to_find()
    if args.limit:
        companies = companies[: args.limit]

    print(f"🔍 Companies to find URLs for: {len(companies)}")
    if args.dry_run:
        print("🏃 Dry run — no API calls")
        for c in companies:
            print(f"  • {c}")
        return

    client = get_firecrawl_client()
    found = []
    not_found = []

    for i, company in enumerate(companies, 1):
        print(f"\n[{i}/{len(companies)}] {company}", flush=True)
        url = _search_website(client, company)
        if url:
            print(f"    ✅ {url}", flush=True)
            found.append(
                {
                    "Company Name": company,
                    "Category": "",
                    "Product": "",
                    "Website": url,
                    "Careers URL": "",
                    "Offices": "",
                    "Notes": "auto-added for enrichment",
                    "Tier": "C",
                    "Experience Match": "",
                    "Personal Interest": "",
                    "User Comments": "",
                    "Status": "candidate",
                    "Fetch Strategy": "",
                    "ATS Slug": "",
                    "ATS Config": "",
                }
            )
        else:
            print("    ⚠ Not found", flush=True)
            not_found.append(company)

        # Brief pause to avoid rate limiting
        time.sleep(0.5)

    print(f"\n{'=' * 50}")
    print(f"✅ Found URLs: {len(found)}")
    print(f"⚠  Not found:  {len(not_found)}")

    if not_found:
        print("\nNot found:")
        for c in not_found:
            print(f"  • {c}")

    if found:
        _save_found_urls(found)
        print(f"\n📝 Updated {len(found)} company URLs in Supabase")
        print("Next: run enrichment with --tiers-only 'about,mission'")


if __name__ == "__main__":
    main()
