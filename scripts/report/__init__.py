"""Dashboard assembler — generates data.js + index.html shell."""

import json
import os
import urllib.parse

from .assets import FAVICON_SVG
from .data_prep import prepare_report_data, prepare_company_data, prepare_triage_data, prepare_scoring_feed, prepare_archived_data

from config import PUBLIC_DIR, resolve_canonical_name


def generate_dashboard(db: dict = None) -> None:
    """Generate the vacancy dashboard: data.js + index.html.

    Loads all data from Supabase. db param is accepted but ignored (backward compat).
    """
    data = prepare_report_data()
    groups = data["groups"]
    stats = data["stats"]
    org_colors = data["org_colors"]

    # --- Build company data ---
    companies = prepare_company_data(org_colors=org_colors)

    # --- Inject calculated_tier + company_slug into groups via canonical name ---
    tier_lookup = {c["name"]: c.get("calculated_tier") for c in companies}
    slug_lookup = {c["name"]: c["slug"] for c in companies if c.get("is_monitored")}
    for g in groups:
        canonical = resolve_canonical_name(g["org"])
        g["calculated_tier"] = tier_lookup.get(canonical)
        g["company_slug"] = slug_lookup.get(canonical)
        g["company_name"] = canonical if slug_lookup.get(canonical) else None

    # --- Enrichment stats (CSV companies only) ---
    csv_companies = [c for c in companies if c.get("is_monitored")]
    monitored = [c for c in csv_companies if c.get("strategy")]
    has_about = sum(1 for c in csv_companies if c.get("description"))
    has_mission = sum(1 for c in csv_companies if c.get("alignment_score") is not None)
    has_both = sum(1 for c in csv_companies if c.get("description") and c.get("alignment_score") is not None)
    has_neither = sum(1 for c in csv_companies if not c.get("description") and c.get("alignment_score") is None)
    # --- Monitoring strategy breakdown ---
    auto_fetch = sum(1 for c in monitored if c.get("strategy") and c["strategy"] != "manual_check")
    manual_check = sum(1 for c in monitored if c.get("strategy") == "manual_check")
    # Strategy type counts
    strategy_counts = {}
    for c in monitored:
        s = c.get("strategy", "")
        if s:
            # Group ATS strategies by type
            if s in ("greenhouse", "lever", "ashby", "workable", "recruitee",
                      "teamtailor_rss", "bamboohr"):
                key = "ats_api"
            elif s in ("workday_api", "amazon_jobs", "unops_widget"):
                key = "custom_api"
            elif s == "firecrawl_scrape":
                key = "firecrawl"
            elif s == "manual_check":
                key = "manual"
            else:
                key = s
            strategy_counts[key] = strategy_counts.get(key, 0) + 1

    enrichment_stats = {
        "total_csv": len(csv_companies),
        "monitored": len(monitored),
        "auto_fetch": auto_fetch,
        "manual_check": manual_check,
        "strategy_counts": strategy_counts,
        "enriched_about": has_about,
        "enriched_mission": has_mission,
        "enriched_full": has_both,
        "empty_shells": has_neither,
    }

    # --- Build triage data for Triage tab ---
    triage_reviews = prepare_triage_data()

    # --- Build archived vacancies for Archive tab ---
    archived_groups = prepare_archived_data()
    for g in archived_groups:
        canonical = resolve_canonical_name(g["org"])
        g["calculated_tier"] = tier_lookup.get(canonical)
        g["company_slug"] = slug_lookup.get(canonical)
        g["company_name"] = canonical if slug_lookup.get(canonical) else None

    # --- Build VACANCY_DATA payload for JS ---
    from datetime import datetime
    vacancy_data = {
        "config": {
            "last_updated": datetime.now().isoformat(timespec="seconds"),
            "api_base": "",  # Vercel uses relative paths
        },
        "stats": stats,
        "enrichment_stats": enrichment_stats,
        "vacancy_ids": [g["id"] for g in groups],
        "groups": groups,
        "companies": companies,
        "triage_reviews": triage_reviews,
        "archived_groups": archived_groups,
    }

    # --- Write data.js ---
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    data_js_path = PUBLIC_DIR / "data.js"
    payload_json = json.dumps(vacancy_data, ensure_ascii=False).replace("</", "<\\/")
    with open(data_js_path, "w", encoding="utf-8") as f:
        f.write("// Auto-generated \u2014 DO NOT EDIT\n")
        f.write(f"var VACANCY_DATA = {payload_json};\n")

    # --- Write index.html ---
    favicon_data_uri = "data:image/svg+xml," + urllib.parse.quote(FAVICON_SVG.strip())

    total_roles = stats["total_roles"]
    relevant = stats["relevant"]
    with_comp = stats["with_comp"]
    europe_count = stats["europe_count"]
    last_updated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- Scoring feed (compact horizontal bar) ---
    scoring_feed = prepare_scoring_feed()
    scoring_feed_html = ""
    if scoring_feed:
        items = " &middot; ".join(
            f'<span class="feed-entry"><span class="feed-count">+{s["count"]}</span> ({s["display"]})</span>'
            for s in scoring_feed
        )
        scoring_feed_html = f'\n<div class="scoring-feed-bar">&#9889; {items}</div>'

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Job Vacancy Dashboard</title>
<link rel="icon" type="image/svg+xml" href="{favicon_data_uri}">
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css">
</head>
<body>
<nav class="top-nav">
  <div class="top-nav-brand">Job Vacancies</div>
  <div class="mode-toggle">
    <button class="mode-btn" id="modeCompanies" onclick="switchMode('companies')">&#9881; Companies</button>
    <button class="mode-btn" id="modeCatalog" onclick="switchMode('catalog')">&#9636; Vacancies</button>
    <button class="mode-btn" id="modePipeline" onclick="switchMode('pipeline')">&#9654; Triage</button>
    <button class="mode-btn" id="modeStats" onclick="switchMode('stats')">&#128205; Geo</button>
    <button class="mode-btn" id="modeArchive" onclick="switchMode('archive')">&#128230; Archive</button>
  </div>
  <div class="top-nav-right">
    <span class="hero-date" id="heroDate">Updated: {last_updated}</span>
  </div>
</nav>{scoring_feed_html}

<div class="container">

  <div id="syncIndicator" class="sync-indicator"></div>

  <!-- === CATALOG MODE === -->
  <div class="catalog-section active" id="catalogSection">

    <div class="catalog-loading" id="catalogLoading">
      <img src="images/illus-catalog.png" alt="Manny reviews vacancies" class="loading-manny">
      <p class="loading-text">Sorting vacancies…</p>
    </div>

    <div class="gf-companion">
      <img src="images/illus-catalog.png" alt="Manny reviews vacancies">
    </div>
    <div class="gf-accent">
      <img src="images/accent-catalog.png" alt="">
    </div>

    <div class="basket-tabs">
      <button class="basket-tab" data-basket="liked" onclick="switchBasket(this)">
        <span class="basket-dot"></span>
        Liked
        <span class="basket-count" id="countLiked">0</span>
      </button>
      <button class="basket-tab active" data-basket="unseen" onclick="switchBasket(this)">
        <span class="basket-dot"></span>
        Unreviewed
        <span class="basket-count" id="countUnseen">0</span>
      </button>
      <button class="basket-tab" data-basket="passed" onclick="switchBasket(this)">
        <span class="basket-dot"></span>
        Passed
        <span class="basket-count" id="countPassed">0</span>
      </button>
    </div>

    <div class="catalog-filters">
      <input type="text" class="catalog-search" id="catalogSearch" placeholder="Search by title, organization, location..." oninput="renderCatalog()">
      <select class="catalog-org-filter" id="catalogOrgFilter" onchange="renderCatalog()">
        <option value="">All companies</option>
      </select>
      <div class="catalog-loc-chips">
        <button class="chip" data-cloc="europe" onclick="toggleCatalogLoc(this)">Europe</button>
        <button class="chip" data-cloc="us" onclick="toggleCatalogLoc(this)">US</button>
        <button class="chip" data-cloc="remote" onclick="toggleCatalogLoc(this)">Remote</button>
        <button class="chip" data-cloc="other" onclick="toggleCatalogLoc(this)">Other</button>
      </div>
      <button class="chip catalog-sort-btn active" onclick="toggleCatalogSort(this)">Score&#160;&#8595;</button>
    </div>

    <div class="catalog-results-count" id="catalogResultsCount"></div>
    <div class="catalog-grid" id="catalogGrid"></div>

  </div><!-- /catalog-section -->

  <!-- === COMPANIES MODE === -->
  <div class="companies-section" id="companiesSection">
    <div class="gf-companion">
      <img src="images/illus-companies.png" alt="Glottis tunes the engine">
    </div>
    <div class="gf-accent">
      <img src="images/accent-companies.png" alt="">
    </div>
    <div class="company-sub-tabs" id="companySubTabs">
      <button class="company-sub-tab active" data-subtab="approved" onclick="switchCompanySubTab('approved')">Approved</button>
      <button class="company-sub-tab" data-subtab="pending" onclick="switchCompanySubTab('pending')">Pending Review</button>
      <button class="company-sub-tab" data-subtab="archived" onclick="switchCompanySubTab('archived')">Archived</button>
    </div>
    <div class="companies-filters">
      <input type="text" class="catalog-search" id="companySearch" placeholder="Search by company, description, location..." oninput="renderCompanies()">
      <select class="catalog-org-filter" id="companyTierFilter" onchange="renderCompanies()">
        <option value="">All tiers</option>
        <option value="S">S — Strategic</option>
        <option value="A">A — Strong Fit</option>
        <option value="B">B — Monitor</option>
        <option value="C">C — Low Priority</option>
        <option value="__unscored">— Unscored</option>
      </select>
      <div class="company-sort-chips">
        <button class="chip chip-sort active" data-csort="liked" onclick="toggleCompanySort(this)">Liked &#8595;</button>
        <button class="chip chip-sort" data-csort="score" onclick="toggleCompanySort(this)">Score</button>
        <button class="chip chip-sort" data-csort="interest" onclick="toggleCompanySort(this)">Interest</button>
      </div>
    </div>
    <div class="company-enrichment-stats" id="companyEnrichmentStats"></div>
    <div class="ces-shown" id="companyShownCount"></div>
    <div class="companies-grid" id="companiesGrid"></div>
  </div><!-- /companies-section -->

  <!-- === PIPELINE MODE === -->
  <div class="pipeline-section" id="pipelineSection">
    <div class="gf-companion">
      <img src="images/illus-pipeline.png" alt="Number Nine Express">
    </div>
    <div class="gf-accent">
      <img src="images/accent-pipeline.png" alt="">
    </div>
    <div class="triage-funnel" id="triageFunnel"></div>
    <div class="triage-board-controls" id="triageBoardControls"></div>
    <div class="pipeline-board" id="pipelineBoard"></div>
  </div>

  <!-- === STATS MODE === -->
  <div class="stats-section" id="statsSection"></div>

  <!-- === ARCHIVE MODE === -->
  <div class="archive-section" id="archiveSection">
    <div class="gf-companion">
      <img src="images/illus-archive.png" alt="Clayman at the archive">
    </div>
    <div class="gf-accent">
      <img src="images/accent-archive.png" alt="">
    </div>
    <div class="archive-intro">
      <h2 class="archive-title">&#128230; Vacancy archive</h2>
      <p class="archive-sub">Older vacancies from before the search restart. View only.</p>
    </div>
    <div class="catalog-filters">
      <input type="text" class="catalog-search" id="archiveSearch" placeholder="Search by title, organization, location..." oninput="renderArchive()">
      <select class="catalog-org-filter" id="archiveOrgFilter" onchange="renderArchive()">
        <option value="">All companies</option>
      </select>
    </div>
    <div class="catalog-results-count" id="archiveResultsCount"></div>
    <div class="catalog-grid" id="archiveGrid"></div>
  </div><!-- /archive-section -->

  <!-- === COMPANY PROFILE PAGE === -->
  <div class="company-profile-page" id="companyProfile"></div>

</div>

<script src="data.js"></script>
<script type="module" src="app.js"></script>
</body>
</html>'''

    html_path = PUBLIC_DIR / "index.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
