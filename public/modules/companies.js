// =============================================================================
// companies.js — Companies table + company profile page
// =============================================================================

import {
  config,
  state,
  groups,
  getCompanies,
  getCompanyBySlug,
  groupsById,
  STATUS_BASKET,
  STATUS_PRI,
  CHIP_TO_COL,
  getGroupStatus,
  getCompanyStatusCounts,
  updateStatus,
  emit,
  scheduleRender,
} from "./state.js";
import {
  escHtml,
  jsAttr,
  relativeTime,
  llmScoreBadge,
  screenScoreBadge,
  parseLocationChips,
  renderLocationChips,
  mdToHtml,
  getFlagForChip,
  formatDeadlineHtml,
  qualityBand,
  tierClass,
  safeUrl,
} from "./helpers.js";
import { saveCompanyReview, showSyncStatus } from "./api.js";
import { T } from "./i18n.js";
// statusChipLabel is U6's vacancy-status pill vocabulary (Liked/Passed/To
// apply/…) — reused here so an open role's status reads identically whether
// you're on its own detail page or in this company's role list (U7).
import { statusChipLabel } from "./vacancy.js";

// MPA Prestige is a personal metric (MPA/MPP application strategy), off by
// default. Opt in with DASHBOARD_MPA=1 / [dashboard] show_mpa_column = true.
const SHOW_MPA = !!(config && config.show_mpa_column);

// ---------------------------------------------------------------------------
// Companies table — init, render, sort, filter
// ---------------------------------------------------------------------------

export function initCompanies() {
  renderCompanies();
}

// Per-company liked/unseen counts. Live rows (/api/companies) carry their own
// counts; snapshot rows fall back to counting vacancy_ids against dbData.
function _counts(c) {
  if (c && (c.liked_count != null || c.new_count != null)) {
    return { liked: c.liked_count || 0, unseen: c.new_count || 0 };
  }
  return getCompanyStatusCounts(c.vacancy_ids);
}

// ---------------------------------------------------------------------------
// Card filter toggle
// ---------------------------------------------------------------------------

export function toggleCompanyCardFilter(filter) {
  // Click same card again → deselect (show all)
  if (state.companyCardFilter === filter) {
    state.companyCardFilter = null;
    state.companyMonitorFilters.clear();
  } else {
    state.companyCardFilter = filter;
    // "Needs attention" card → preset chips
    if (filter === "needAction") {
      state.companyMonitorFilters = new Set(["error", "stale", "never"]);
    } else {
      state.companyMonitorFilters.clear();
    }
  }
  renderCompanies();
}

// ---------------------------------------------------------------------------
// Monitoring chip toggle
// ---------------------------------------------------------------------------

export function toggleMonitoringChip(level) {
  if (state.companyMonitorFilters.has(level)) {
    state.companyMonitorFilters.delete(level);
  } else {
    state.companyMonitorFilters.add(level);
  }
  // Clear card filter to avoid conflicts
  state.companyCardFilter = null;
  renderCompanies();
}

// ---------------------------------------------------------------------------
// Sub-tab switching
// ---------------------------------------------------------------------------

export function switchCompanySubTab(tab) {
  state.companyCardFilter = null;
  state.companyMonitorFilters.clear();
  state.companySubTab = tab;
  // Reset sort to sensible defaults per tab
  if (tab === "archived") {
    state.companySortCol = "name";
    state.companySortAsc = true;
  } else if (tab === "pending") {
    state.companySortCol = "fit";
    state.companySortAsc = false;
  } else {
    state.companySortCol = "applyable";
    state.companySortAsc = false;
  }
  // Update tab button active state
  document.querySelectorAll(".company-sub-tab").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.subtab === tab);
  });
  // Update sort chip active state
  document.querySelectorAll(".chip-sort[data-csort]").forEach(function (c) {
    var col = CHIP_TO_COL[c.dataset.csort] || c.dataset.csort;
    c.classList.toggle("active", col === state.companySortCol);
  });
  renderCompanies();
}

// ---------------------------------------------------------------------------
// Review status resolution (live API > baked data)
// ---------------------------------------------------------------------------

function _getReviewStatus(c) {
  if (
    state.companyStatusesLoaded &&
    c.company_id &&
    state.companyStatuses[c.company_id]
  ) {
    return state.companyStatuses[c.company_id];
  }
  return c.review_status || "pending";
}

// ---------------------------------------------------------------------------
// Filtering + sorting
// ---------------------------------------------------------------------------

function getFilteredSortedCompanies() {
  var query = (
    document.getElementById("companySearch").value || ""
  ).toLowerCase();
  var tierFilter = document.getElementById("companyTierFilter").value;
  var subTab = state.companySubTab;

  var filtered = getCompanies().filter(function (c) {
    var reviewSt = _getReviewStatus(c);
    // Sub-tab filter
    if (subTab === "approved" && reviewSt !== "approved") return false;
    if (subTab === "pending" && reviewSt !== "pending") return false;
    if (subTab === "archived" && reviewSt !== "rejected") return false;
    // Tier filter
    if (tierFilter === "__unscored") {
      if (c.calculated_tier != null) return false;
    } else if (tierFilter) {
      if (c.calculated_tier !== tierFilter) return false;
    }
    // Search
    if (query) {
      var searchable = [c.name, c.category, c.product, c.notes, c.offices]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!searchable.includes(query)) return false;
    }
    return true;
  });

  // Card filter (approved tab only) — applied AFTER base filters
  var cardFilter = state.companyCardFilter;
  if (subTab === "approved" && cardFilter) {
    if (cardFilter === "withNew") {
      filtered = filtered.filter(function (c) {
        return _counts(c).unseen > 0;
      });
    }
    // needAction card uses monitoring chips, not separate filter
  }

  // Monitoring chip filter (approved tab only)
  if (subTab === "approved" && state.companyMonitorFilters.size > 0) {
    filtered = filtered.filter(function (c) {
      return state.companyMonitorFilters.has(_getMonitoringStatus(c).level);
    });
  }

  var col = state.companySortCol;
  var asc = state.companySortAsc;
  filtered.sort(function (a, b) {
    var va, vb;
    if (col === "name") {
      va = a.name || "";
      vb = b.name || "";
      return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    }
    if (col === "tier") {
      var tierOrd = { S: 0, A: 1, B: 2, C: 3 };
      va = a.calculated_tier != null ? tierOrd[a.calculated_tier] : 4;
      vb = b.calculated_tier != null ? tierOrd[b.calculated_tier] : 4;
    } else if (col === "fit") {
      va = a.alignment_score != null ? a.alignment_score : -1;
      vb = b.alignment_score != null ? b.alignment_score : -1;
    } else if (col === "mpa") {
      va = a.mpa_prestige != null ? a.mpa_prestige : -1;
      vb = b.mpa_prestige != null ? b.mpa_prestige : -1;
    } else if (col === "score") {
      va = a.avg_llm_score != null ? a.avg_llm_score : -1;
      vb = b.avg_llm_score != null ? b.avg_llm_score : -1;
    } else if (col === "vacancies") {
      va = a.vacancy_count || 0;
      vb = b.vacancy_count || 0;
    } else if (col === "applyable") {
      va = a.applyable_count || 0;
      vb = b.applyable_count || 0;
    } else if (col === "liked") {
      va = _counts(a).liked;
      vb = _counts(b).liked;
    } else if (col === "freshness") {
      va = a.last_fetched || "";
      vb = b.last_fetched || "";
      return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    } else if (col === "new") {
      va = _counts(a).unseen;
      vb = _counts(b).unseen;
    } else if (col === "monitoring") {
      var MON_ORD = {
        error: 0,
        stale: 1,
        never: 2,
        nosource: 3,
        nodata: 4,
        manual: 5,
        ok: 6,
      };
      va = MON_ORD[_getMonitoringStatus(a).level] ?? 99;
      vb = MON_ORD[_getMonitoringStatus(b).level] ?? 99;
      if (va === vb)
        return asc
          ? (a.name || "").localeCompare(b.name || "")
          : (b.name || "").localeCompare(a.name || "");
    } else if (col && col.indexOf("dim_") === 0) {
      var dk = col.slice(4);
      va =
        a.fit_dimensions && a.fit_dimensions[dk] != null
          ? a.fit_dimensions[dk]
          : -1;
      vb =
        b.fit_dimensions && b.fit_dimensions[dk] != null
          ? b.fit_dimensions[dk]
          : -1;
    } else {
      va = 0;
      vb = 0;
    }
    return asc ? va - vb : vb - va;
  });

  // In Pending Review, float companies that have a strong vacancy (🔥) to the
  // very top so a forgotten company with a great role gets seen. Stable
  // partition preserves the chosen sort order within each bucket.
  if (subTab === "pending") {
    var hot = [];
    var rest = [];
    filtered.forEach(function (c) {
      if (c.hot_vacancy && c.hot_vacancy.score != null) hot.push(c);
      else rest.push(c);
    });
    hot.sort(function (a, b) {
      return (b.hot_vacancy.score || 0) - (a.hot_vacancy.score || 0);
    });
    filtered = hot.concat(rest);
  }

  return filtered;
}

// ---------------------------------------------------------------------------
// WANT aspect breakdown — shared between the profile chart, the sortable table
// columns and the row cells so key order + labels stay in sync. Column sort
// keys are "dim_" + the fit_dimensions field name.
// ---------------------------------------------------------------------------

// [field, full name, short header code, hover tip — what the dimension measures]
var WANT_DIMS = [
  [
    "mission_authenticity",
    "Mission",
    "Mis",
    "Is the social good direct and real, or a marketing veneer?",
  ],
  [
    "domain_desirability",
    "Domain",
    "Dom",
    "How desirable the field is for this candidate, per their profile.",
  ],
  [
    "breadth_rotation",
    "Breadth",
    "Bre",
    "Room to grow broad — many areas + role rotation vs one narrow lane.",
  ],
  [
    "builder_stage",
    "Stage 0→1",
    "Stg",
    "0→1 building inside a stable org, not a fragile startup or sleepy giant.",
  ],
  [
    "career_entry_value",
    "Career",
    "Car",
    "Brand, network and trajectory value — measured objectively.",
  ],
  [
    "money_stability",
    "Money",
    "Mon",
    "Financially safe, well-funded employer plus pay potential.",
  ],
  [
    "culture_fit",
    "Culture",
    "Cul",
    "Analytical, entrepreneurial, modern vs bureaucratic and traditional.",
  ],
];

function _dimColor(v) {
  return v >= 75
    ? "#059669"
    : v >= 55
      ? "#0284C7"
      : v >= 35
        ? "#D97706"
        : "#DC2626";
}

function _dimNumHtml(v) {
  if (v == null) return '<span class="ct-dim-empty">—</span>';
  return (
    '<span class="ct-dim-num" style="color:' +
    _dimColor(v) +
    '">' +
    v +
    "</span>"
  );
}

function _dimColumns() {
  return WANT_DIMS.map(function (d) {
    return {
      key: "dim_" + d[0],
      label: d[2],
      tip: d[1] + " — " + d[3],
      sortable: true,
      cls: "ct-col-dim",
    };
  });
}

function _dimCellsHtml(c) {
  var fd = c.fit_dimensions || {};
  var out = "";
  for (var i = 0; i < WANT_DIMS.length; i++) {
    var v = fd[WANT_DIMS[i][0]];
    out +=
      '<td class="ct-td ct-col-dim">' +
      _dimNumHtml(v != null ? v : null) +
      "</td>";
  }
  return out;
}

// ---------------------------------------------------------------------------
// Column definitions per sub-tab
// ---------------------------------------------------------------------------

function _getColumns() {
  var subTab = state.companySubTab;
  if (subTab === "pending") {
    return [
      {
        key: "name",
        label: T("col_company", "Company"),
        sortable: true,
        cls: "ct-col-name ct-col-name--pending",
      },
      {
        key: "category",
        label: T("col_category", "Category"),
        sortable: false,
        cls: "ct-col-cat ct-col-cat--pending",
      },
      {
        key: "fit",
        label: T("col_want", "WANT"),
        sortable: true,
        cls: "ct-col-fit ct-col-fit--pending",
      },
      ..._dimColumns(),
      {
        key: "vacancies",
        label: T("col_vacancies", "Vacancies"),
        sortable: true,
        cls: "ct-col-vac ct-col-vac--pending",
      },
      {
        key: "offices",
        label: T("col_location", "Location"),
        sortable: false,
        cls: "ct-col-loc ct-col-loc--pending",
      },
      {
        key: "source",
        label: T("col_source", "Source"),
        sortable: false,
        cls: "ct-col-source",
      },
      {
        key: "review",
        label: T("col_review_hdr", "Review"),
        sortable: false,
        cls: "ct-col-review",
      },
    ];
  }
  if (subTab === "archived") {
    return [
      {
        key: "name",
        label: T("col_company", "Company"),
        sortable: true,
        cls: "ct-col-name ct-col-name--archived",
      },
      {
        key: "category",
        label: T("col_category", "Category"),
        sortable: false,
        cls: "ct-col-cat ct-col-cat--archived",
      },
      {
        key: "fit",
        label: T("col_want", "WANT"),
        sortable: true,
        cls: "ct-col-fit ct-col-fit--archived",
      },
      {
        key: "offices",
        label: T("col_location", "Location"),
        sortable: false,
        cls: "ct-col-loc ct-col-loc--archived",
      },
      {
        key: "reason",
        label: T("col_reason", "Reason"),
        sortable: false,
        cls: "ct-col-reason",
      },
      {
        key: "review",
        label: T("col_review_hdr", "Review"),
        sortable: false,
        cls: "ct-col-review",
      },
    ];
  }
  // approved (default)
  return [
    {
      key: "name",
      label: T("col_company", "Company"),
      sortable: true,
      cls: "ct-col-name",
    },
    {
      key: "tier",
      label: T("col_tier", "Tier"),
      sortable: true,
      cls: "ct-col-tier",
    },
    {
      key: "fit",
      label: T("col_want", "WANT"),
      sortable: true,
      cls: "ct-col-fit",
    },
    ..._dimColumns(),
    ...(SHOW_MPA
      ? [{ key: "mpa", label: "MPA", sortable: true, cls: "ct-col-mpa" }]
      : []),
    {
      key: "liked",
      label: T("col_liked", "Liked"),
      sortable: true,
      cls: "ct-col-liked",
    },
    {
      key: "new",
      label: T("col_new", "New"),
      sortable: true,
      cls: "ct-col-new",
    },
    {
      key: "freshness",
      label: T("col_freshness", "Freshness"),
      sortable: true,
      cls: "ct-col-freshness",
    },
    {
      key: "offices",
      label: T("col_location", "Location"),
      sortable: false,
      cls: "ct-col-loc",
    },
    {
      key: "monitoring",
      label: T("col_monitoring", "Monitoring"),
      sortable: true,
      cls: "ct-col-monitoring",
    },
    {
      key: "review",
      label: T("col_review_hdr", "Review"),
      sortable: false,
      cls: "ct-col-review",
    },
  ];
}

// ---------------------------------------------------------------------------
// Stats cards (context-aware per tab)
// ---------------------------------------------------------------------------

function _renderStatsCards(unfilteredByCard, filtered) {
  var statsEl = document.getElementById("companyEnrichmentStats");
  if (!statsEl) return;

  var subTab = state.companySubTab;

  if (subTab === "approved") {
    var total = unfilteredByCard.length;
    var withNew = 0;
    var staleCount = 0;
    var errorCount = 0;
    for (var i = 0; i < unfilteredByCard.length; i++) {
      var counts = _counts(unfilteredByCard[i]);
      if (counts.unseen > 0) withNew++;
      var ms = _getMonitoringStatus(unfilteredByCard[i]);
      if (ms.level === "stale" || ms.level === "never") staleCount++;
      if (ms.level === "error") errorCount++;
    }
    var needAction = staleCount + errorCount;
    var activeFilter = state.companyCardFilter;

    // Subtitle for "Needs attention"
    var actionParts = [];
    if (staleCount > 0)
      actionParts.push(staleCount + " " + T("stat_stale_suffix", "stale"));
    if (errorCount > 0)
      actionParts.push(errorCount + " " + T("stat_errors_suffix", "errors"));
    var actionSub = actionParts.join(" \u00B7 ") || "\u2014";

    // Subtitle for "With new"
    var newSub =
      withNew > 0
        ? withNew + " " + T("stat_unreviewed_suffix", "unreviewed")
        : "\u2014";

    statsEl.innerHTML =
      '<div class="ces-card ces-card--approved ces-card--clickable' +
      (activeFilter === null ? " ces-card--active" : "") +
      '" onclick="toggleCompanyCardFilter(null)">' +
      '<span class="ces-number">' +
      total +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_total", "Total")) +
      "</span>" +
      '<span class="ces-sub">\u00A0</span>' +
      "</div>" +
      '<div class="ces-card ces-card--new ces-card--clickable' +
      (activeFilter === "withNew" ? " ces-card--active" : "") +
      '" onclick="toggleCompanyCardFilter(\'withNew\')">' +
      '<span class="ces-number">' +
      withNew +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_with_new", "With new")) +
      "</span>" +
      '<span class="ces-sub">' +
      escHtml(newSub) +
      "</span>" +
      "</div>" +
      '<div class="ces-card ces-card--attention ces-card--clickable' +
      (activeFilter === "needAction" ? " ces-card--active" : "") +
      '" onclick="toggleCompanyCardFilter(\'needAction\')">' +
      '<span class="ces-number">' +
      needAction +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_needs_attention", "Needs attention")) +
      "</span>" +
      '<span class="ces-sub">' +
      escHtml(actionSub) +
      "</span>" +
      "</div>";
  } else if (subTab === "pending") {
    var pendingCount = filtered.length;
    var enrichedCount = 0;
    for (var pi = 0; pi < filtered.length; pi++) {
      if (filtered[pi].alignment_score != null) enrichedCount++;
    }
    statsEl.innerHTML =
      '<div class="ces-card ces-card--pending">' +
      '<span class="ces-number">' +
      pendingCount +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_pending", "Pending")) +
      "</span>" +
      "</div>" +
      '<div class="ces-card ces-card--approved">' +
      '<span class="ces-number">' +
      enrichedCount +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_enriched", "Enriched")) +
      "</span>" +
      "</div>";
  } else {
    // archived
    statsEl.innerHTML =
      '<div class="ces-card ces-card--total">' +
      '<span class="ces-number">' +
      filtered.length +
      "</span>" +
      '<span class="ces-label">' +
      escHtml(T("stat_rejected", "Rejected")) +
      "</span>" +
      "</div>";
  }
}

// ---------------------------------------------------------------------------
// Monitoring status logic
// ---------------------------------------------------------------------------

function _getMonitoringStatus(c) {
  // Priority order: error → no_data → manual → nosource → never → stale → ok
  if (
    c.fetch_status &&
    c.fetch_status !== "ok" &&
    c.fetch_status !== "no_data"
  ) {
    return {
      level: "error",
      label: T("mon_error", "Fetch error"),
      dotCls: "mon-dot--error",
      tooltip: c.fetch_status,
    };
  }
  if (c.fetch_status === "no_data") {
    return {
      level: "nodata",
      label: T("mon_no_data", "No data"),
      dotCls: "mon-dot--nodata",
      tooltip: "Fetch succeeded, but no vacancies found",
    };
  }
  if (c.is_manual_check) {
    return {
      level: "manual",
      label: T("mon_manual", "Manual"),
      dotCls: "mon-dot--manual",
      tooltip: "Manual check",
    };
  }
  if (c.needs_source || !c.strategy) {
    return {
      level: "nosource",
      label: T("mon_no_source", "No source"),
      dotCls: "mon-dot--nosource",
      tooltip: "Source not configured",
    };
  }
  if (!c.last_fetched) {
    return {
      level: "never",
      label: T("mon_never", "Never run"),
      dotCls: "mon-dot--never",
      tooltip: "Fetch never run",
    };
  }
  // Check staleness
  var daysSince = _daysSince(c.last_fetched);
  if (daysSince > 7) {
    return {
      level: "stale",
      label: T("mon_days_ago", "{n}d ago").replace("{n}", daysSince),
      dotCls: "mon-dot--stale",
      tooltip: "Last fetch " + daysSince + " days ago",
    };
  }
  return {
    level: "ok",
    label: T("mon_working", "Working"),
    dotCls: "mon-dot--ok",
    tooltip: "Last fetch " + _daysSince(c.last_fetched) + "d ago",
  };
}

function _daysSince(dateStr) {
  if (!dateStr) return 999;
  var d = new Date(dateStr);
  if (isNaN(d.getTime())) return 999;
  return Math.floor((Date.now() - d.getTime()) / 86400000);
}

function _freshnessHtml(c) {
  if (!c.last_fetched)
    return '<span class="freshness-cell"><span class="freshness-dot freshness-never"></span>\u2014</span>';
  var days = _daysSince(c.last_fetched);
  var dotCls =
    days <= 3
      ? "freshness-green"
      : days <= 7
        ? "freshness-amber"
        : "freshness-red";
  return (
    '<span class="freshness-cell"><span class="freshness-dot ' +
    dotCls +
    '"></span>' +
    relativeTime(c.last_fetched, T) +
    "</span>"
  );
}

function _monitoringHtml(c) {
  var ms = _getMonitoringStatus(c);
  return (
    '<span class="mon-cell" title="' +
    escHtml(ms.tooltip) +
    '"><span class="mon-dot ' +
    ms.dotCls +
    '"></span>' +
    escHtml(ms.label) +
    "</span>"
  );
}

// ---------------------------------------------------------------------------
// Monitoring chips (per-status filter row)
// ---------------------------------------------------------------------------

var MON_CHIP_ORDER = [
  "error",
  "stale",
  "never",
  "nosource",
  "nodata",
  "manual",
  "ok",
];
function _monChipLabel(level) {
  switch (level) {
    case "error":
      return T("monchip_error", "Errors");
    case "stale":
      return T("monchip_stale", "Stale");
    case "never":
      return T("monchip_never", "Never run");
    case "nosource":
      return T("monchip_nosource", "No source");
    case "nodata":
      return T("monchip_nodata", "No data");
    case "manual":
      return T("monchip_manual", "Manual");
    case "ok":
      return T("monchip_ok", "OK");
    default:
      return level;
  }
}

function _renderMonitoringChips(baseList) {
  var container = document.getElementById("monitoringChips");
  if (!container) {
    // Create container after stats cards
    var statsEl = document.getElementById("companyEnrichmentStats");
    if (!statsEl) return;
    container = document.createElement("div");
    container.id = "monitoringChips";
    statsEl.parentNode.insertBefore(container, statsEl.nextSibling);
  }

  // Only show on approved tab
  if (state.companySubTab !== "approved") {
    container.innerHTML = "";
    return;
  }

  // Count per monitoring level from base list (before chip/card filters)
  var levelCounts = {};
  for (var i = 0; i < baseList.length; i++) {
    var lvl = _getMonitoringStatus(baseList[i]).level;
    levelCounts[lvl] = (levelCounts[lvl] || 0) + 1;
  }

  var html = '<div class="mon-chips-row">';
  var hasAny = false;
  for (var ci = 0; ci < MON_CHIP_ORDER.length; ci++) {
    var level = MON_CHIP_ORDER[ci];
    var count = levelCounts[level] || 0;
    if (count === 0) continue;
    hasAny = true;
    var isActive = state.companyMonitorFilters.has(level);
    html +=
      '<span class="mon-chip mon-chip--' +
      level +
      (isActive ? " active" : "") +
      '" onclick="toggleMonitoringChip(\'' +
      level +
      "')\">" +
      '<span class="mon-dot ' +
      "mon-dot--" +
      level +
      '"></span>' +
      count +
      " " +
      escHtml(_monChipLabel(level)) +
      "</span>";
  }
  html += "</div>";
  container.innerHTML = hasAny ? html : "";
}

// ---------------------------------------------------------------------------
// Review complete banner
// ---------------------------------------------------------------------------

// Persistent note on the Pending sub-tab: vacancies from not-yet-approved
// companies stay hidden from the job list until the company is approved.
function _renderPendingDisclaimer(pendingCompanies) {
  var statsEl = document.getElementById("companyEnrichmentStats");
  if (!statsEl) return;
  var existing = document.getElementById("companyPendingDisclaimer");
  if (existing) existing.remove();

  if (state.companySubTab !== "pending") return;

  var orgs = 0;
  var vacs = 0;
  for (var i = 0; i < pendingCompanies.length; i++) {
    var n = pendingCompanies[i].vacancy_count || 0;
    if (n > 0) {
      orgs++;
      vacs += n;
    }
  }
  if (vacs === 0) return;

  var tpl = T(
    "companies_pending_hidden",
    "ℹ️ {orgs} companies here have {vacs} vacancies hidden from your job list — approve a company to surface its roles.",
  );
  var note = document.createElement("div");
  note.id = "companyPendingDisclaimer";
  note.className = "ces-pending-hidden";
  note.textContent = tpl.replace("{orgs}", orgs).replace("{vacs}", vacs);
  statsEl.parentNode.insertBefore(note, statsEl.nextSibling);
}

function showReviewComplete() {
  var statsEl = document.getElementById("companyEnrichmentStats");
  if (statsEl) {
    var banner = document.createElement("div");
    banner.className = "ces-review-complete";
    banner.textContent = "\u2705 All companies reviewed!";
    statsEl.parentNode.insertBefore(banner, statsEl.nextSibling);
    setTimeout(function () {
      banner.remove();
    }, 4000);
  }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

export function renderCompanies() {
  var grid = document.getElementById("companiesGrid");
  // Get base list (before card/chip filters) for stats cards + chip counts
  var savedCardFilter = state.companyCardFilter;
  var savedMonitorFilters = state.companyMonitorFilters;
  state.companyCardFilter = null;
  state.companyMonitorFilters = new Set();
  var unfilteredByCard = getFilteredSortedCompanies();
  state.companyCardFilter = savedCardFilter;
  state.companyMonitorFilters = savedMonitorFilters;
  // Get final filtered list (with card + chip filters applied)
  var filtered = getFilteredSortedCompanies();

  _renderStatsCards(unfilteredByCard, filtered);
  _renderPendingDisclaimer(unfilteredByCard);
  _renderMonitoringChips(unfilteredByCard);

  var shownEl = document.getElementById("companyShownCount");
  if (shownEl) {
    var cardFilter = state.companyCardFilter;
    var cardLabels = { withNew: "With new", needAction: "Needs attention" };
    var hasChips = state.companyMonitorFilters.size > 0;
    if (cardFilter && cardLabels[cardFilter]) {
      shownEl.innerHTML =
        filtered.length +
        " shown \u00B7 " +
        escHtml(cardLabels[cardFilter]) +
        ' <span class="ces-filter-dismiss" onclick="toggleCompanyCardFilter(\'' +
        cardFilter +
        "')\">✕</span>";
    } else if (hasChips) {
      shownEl.textContent = filtered.length + " shown";
    } else {
      // Count total for current sub-tab
      var subTab = state.companySubTab;
      var tabTotal = 0;
      var allCompanies = getCompanies();
      for (var ci = 0; ci < allCompanies.length; ci++) {
        var rs = _getReviewStatus(allCompanies[ci]);
        if (subTab === "approved" && rs === "approved") tabTotal++;
        else if (subTab === "pending" && rs === "pending") tabTotal++;
        else if (subTab === "archived" && rs === "rejected") tabTotal++;
      }
      shownEl.textContent =
        filtered.length !== tabTotal ? filtered.length + " shown" : "";
    }
  }

  // Update sub-tab counts
  _updateSubTabCounts();

  if (filtered.length === 0) {
    grid.innerHTML =
      '<div class="company-empty">\uD83C\uDFE2 Nothing found</div>';
    return;
  }

  var cols = _getColumns();
  var thead = '<thead><tr class="ct-header-row">';
  for (var i = 0; i < cols.length; i++) {
    var c = cols[i];
    var isActive = state.companySortCol === c.key;
    var arrow = "";
    if (isActive) arrow = state.companySortAsc ? " \u2191" : " \u2193";
    // data-tip drives the instant CSS hover tooltip (.ct-th[data-tip]::after);
    // no native title, so we don't get a second OS tooltip a second later.
    var titleAttr = c.tip ? ' data-tip="' + escHtml(c.tip) + '"' : "";
    if (c.sortable) {
      thead +=
        '<th class="ct-th ' +
        c.cls +
        (isActive ? " ct-th-active" : "") +
        '"' +
        titleAttr +
        " onclick=\"sortCompanyTable('" +
        c.key +
        "')\">" +
        escHtml(c.label) +
        arrow +
        "</th>";
    } else {
      thead +=
        '<th class="ct-th ' +
        c.cls +
        '"' +
        titleAttr +
        ">" +
        escHtml(c.label) +
        "</th>";
    }
  }
  thead += "</tr></thead>";

  var rows = [];
  for (var j = 0; j < filtered.length; j++) {
    rows.push(_buildRow(filtered[j]));
  }

  grid.innerHTML =
    '<table class="company-table">' +
    thead +
    "<tbody>" +
    rows.join("") +
    "</tbody></table>";
}

function _updateSubTabCounts() {
  var counts = { approved: 0, pending: 0, rejected: 0 };
  var allCompanies = getCompanies();
  for (var i = 0; i < allCompanies.length; i++) {
    var rs = _getReviewStatus(allCompanies[i]);
    if (rs === "approved") counts.approved++;
    else if (rs === "pending") counts.pending++;
    else if (rs === "rejected") counts.rejected++;
  }
  document.querySelectorAll(".company-sub-tab").forEach(function (btn) {
    var tab = btn.dataset.subtab;
    var count =
      tab === "approved"
        ? counts.approved
        : tab === "pending"
          ? counts.pending
          : counts.rejected;
    // Update or create count badge
    var badge = btn.querySelector(".sub-tab-count");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "sub-tab-count";
      btn.appendChild(badge);
    }
    badge.textContent = count;
  });
}

// ---------------------------------------------------------------------------
// Row builder (dispatches to sub-tab-specific builders)
// ---------------------------------------------------------------------------

function _buildRow(c) {
  var subTab = state.companySubTab;
  if (subTab === "pending") return _buildPendingRow(c);
  if (subTab === "archived") return _buildArchivedRow(c);
  return _buildApprovedRow(c);
}

function _buildApprovedRow(c) {
  var fg = (c.org_color || ["#F97316"])[0];
  var counts = _counts(c);
  var tierCls = c.calculated_tier
    ? "ctier-" + c.calculated_tier.toLowerCase()
    : "ctier-none";
  var tierLabel = c.calculated_tier || "\u2014";
  var fitBadge =
    c.alignment_score != null ? llmScoreBadge(c.alignment_score) : "\u2014";
  var likedCount = counts.liked;
  var likedHtml =
    likedCount > 0
      ? '<span class="ct-liked-nonzero">' + likedCount + "</span>"
      : '<span class="ct-liked-zero">0</span>';
  var unseenCount = counts.unseen;
  var newHtml =
    unseenCount > 0
      ? '<span class="ct-new-nonzero">' + unseenCount + "</span>"
      : "";
  var locText = c.offices ? escHtml(c.offices) : "\u2014";

  return (
    '<tr class="ct-row" style="--row-accent:' +
    fg +
    '" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\">" +
    '<td class="ct-td ct-col-name"><span class="ct-name-text">' +
    escHtml(c.name) +
    "</span></td>" +
    '<td class="ct-td ct-col-tier"><span class="company-tier-badge ' +
    tierCls +
    '">' +
    tierLabel +
    "</span></td>" +
    '<td class="ct-td ct-col-fit">' +
    fitBadge +
    "</td>" +
    _dimCellsHtml(c) +
    (SHOW_MPA
      ? '<td class="ct-td ct-col-mpa">' +
        (c.mpa_prestige != null ? llmScoreBadge(c.mpa_prestige) : "\u2014") +
        "</td>"
      : "") +
    '<td class="ct-td ct-col-liked">' +
    likedHtml +
    "</td>" +
    '<td class="ct-td ct-col-new">' +
    newHtml +
    "</td>" +
    '<td class="ct-td ct-col-freshness">' +
    _freshnessHtml(c) +
    "</td>" +
    '<td class="ct-td ct-col-loc"><span class="ct-location-text">' +
    locText +
    "</span></td>" +
    '<td class="ct-td ct-col-monitoring">' +
    _monitoringHtml(c) +
    "</td>" +
    '<td class="ct-td ct-col-review">' +
    '<button class="cr-btn cr-reject" onclick="event.stopPropagation();reviewCompany(\'' +
    escHtml(c.company_id || "") +
    "','reject')\" title=\"Archive\">✗</button>" +
    "</td>" +
    "</tr>"
  );
}

function _buildPendingRow(c) {
  var fg = (c.org_color || ["#F97316"])[0];
  var fitBadge =
    c.alignment_score != null ? llmScoreBadge(c.alignment_score) : "\u2014";
  var locText = c.offices ? escHtml(c.offices) : "\u2014";
  var catText = c.category ? escHtml(c.category) : "\u2014";
  var sourceText = c.strategy ? escHtml(c.strategy) : "\u2014";

  var cid = escHtml(c.company_id || "");
  var reviewHtml =
    '<button class="cr-btn cr-approve" onclick="event.stopPropagation();reviewCompany(\'' +
    cid +
    "','approve')\" title=\"Approve\">\u2713</button>" +
    '<button class="cr-btn cr-reject" onclick="event.stopPropagation();reviewCompany(\'' +
    cid +
    "','reject')\" title=\"Reject\">\u2717</button>";

  // \ud83d\udd25 badge when a strong vacancy is waiting behind this unreviewed
  // company, plus a \u23f0 deadline marker if it expires soon.
  var hotBadge = "";
  if (c.hot_vacancy && c.hot_vacancy.score != null) {
    var dl = c.hot_vacancy.deadline_label
      ? ' <span class="ct-hot-deadline">\u23f0 ' +
        escHtml(c.hot_vacancy.deadline_label) +
        "</span>"
      : "";
    hotBadge =
      ' <span class="ct-hot-badge" title="Strong vacancy at an unreviewed company">\ud83d\udd25 ' +
      c.hot_vacancy.score +
      "</span>" +
      dl;
  }

  return (
    '<tr class="ct-row" style="--row-accent:' +
    fg +
    '" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\">" +
    '<td class="ct-td ct-col-name ct-col-name--pending"><span class="ct-name-text">' +
    escHtml(c.name) +
    "</span>" +
    hotBadge +
    "</td>" +
    '<td class="ct-td ct-col-cat ct-col-cat--pending">' +
    catText +
    "</td>" +
    '<td class="ct-td ct-col-fit ct-col-fit--pending">' +
    fitBadge +
    "</td>" +
    _dimCellsHtml(c) +
    '<td class="ct-td ct-col-vac ct-col-vac--pending">' +
    (c.vacancy_count || 0) +
    "</td>" +
    '<td class="ct-td ct-col-loc ct-col-loc--pending"><span class="ct-location-text">' +
    locText +
    "</span></td>" +
    '<td class="ct-td ct-col-source">' +
    sourceText +
    "</td>" +
    '<td class="ct-td ct-col-review">' +
    reviewHtml +
    "</td>" +
    "</tr>"
  );
}

function _buildArchivedRow(c) {
  var fitBadge =
    c.alignment_score != null ? llmScoreBadge(c.alignment_score) : "\u2014";
  var locText = c.offices ? escHtml(c.offices) : "\u2014";
  var catText = c.category ? escHtml(c.category) : "\u2014";
  var reasonText = c.status_reason ? escHtml(c.status_reason) : "\u2014";

  return (
    '<tr class="ct-row ct-row--archived" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\">" +
    '<td class="ct-td ct-col-name ct-col-name--archived"><span class="ct-name-text">' +
    escHtml(c.name) +
    "</span></td>" +
    '<td class="ct-td ct-col-cat ct-col-cat--archived">' +
    catText +
    "</td>" +
    '<td class="ct-td ct-col-fit ct-col-fit--archived">' +
    fitBadge +
    "</td>" +
    '<td class="ct-td ct-col-loc ct-col-loc--archived"><span class="ct-location-text">' +
    locText +
    "</span></td>" +
    '<td class="ct-td ct-col-reason">' +
    reasonText +
    "</td>" +
    '<td class="ct-td ct-col-review">' +
    '<button class="cr-btn cr-approve" onclick="event.stopPropagation();reviewCompany(\'' +
    escHtml(c.company_id || "") +
    "','approve')\" title=\"Restore to active\">✓</button>" +
    "</td>" +
    "</tr>"
  );
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

export function sortCompanyTable(col) {
  if (state.companySortCol === col) {
    state.companySortAsc = !state.companySortAsc;
  } else {
    state.companySortCol = col;
    state.companySortAsc = col === "name";
  }
  renderCompanies();
}

export function toggleCompanySort(btn) {
  var col = CHIP_TO_COL[btn.dataset.csort] || btn.dataset.csort;
  if (state.companySortCol === col) {
    state.companySortAsc = !state.companySortAsc;
  } else {
    state.companySortCol = col;
    state.companySortAsc = col === "name";
  }
  document.querySelectorAll(".chip-sort[data-csort]").forEach(function (c) {
    c.classList.toggle("active", c === btn);
  });
  renderCompanies();
}

// ---------------------------------------------------------------------------
// Company review
// ---------------------------------------------------------------------------

export function reviewCompany(companyId, action) {
  if (!companyId) return;
  var newStatus = action === "approve" ? "approved" : "rejected";

  // Remember the status to restore if the server call fails. The company may
  // currently be pending, approved, or rejected (active/archived tabs now have
  // action buttons too), so we can't assume "pending".
  var prevCompany = getCompanies().find(function (c) {
    return c.company_id === companyId;
  });
  var prevStatus = prevCompany ? _getReviewStatus(prevCompany) : "pending";

  // Capture current position BEFORE optimistic update (for auto-nav in pending tab)
  var shouldAutoNav =
    state.companySubTab === "pending" && state.currentProfileSlug;
  var nextSlug = null;

  if (shouldAutoNav) {
    var currentList = getFilteredSortedCompanies();
    var currentIdx = currentList.findIndex(function (c) {
      return c.slug === state.currentProfileSlug;
    });
    if (currentIdx === -1) {
      shouldAutoNav = false;
    } else {
      for (var i = currentIdx + 1; i < currentList.length; i++) {
        if (
          _getReviewStatus(currentList[i]) === "pending" &&
          currentList[i].slug !== state.currentProfileSlug
        ) {
          nextSlug = currentList[i].slug;
          break;
        }
      }
    }
  }

  // Optimistic update
  state.companyStatuses[companyId] = newStatus;
  renderCompanies();
  scheduleRender();

  // Auto-navigate to next pending company
  if (shouldAutoNav) {
    if (nextSlug) {
      openCompanyProfile(nextSlug);
    } else {
      closeCompanyProfile();
      showReviewComplete();
    }
  } else if (state.currentProfileSlug) {
    renderProfileForSlug(state.currentProfileSlug);
  }

  // Persist to server
  saveCompanyReview(companyId, action).then(function (ok) {
    if (!ok) {
      state.companyStatuses[companyId] = prevStatus;
      renderCompanies();
      scheduleRender();
      if (state.currentProfileSlug) {
        renderProfileForSlug(state.currentProfileSlug);
      }
    }
  });
}

export function companyVacancyAction(canonId, memberIds, newStatus) {
  updateStatus(canonId, memberIds, newStatus);
}

export function viewOrgInCatalog(orgName) {
  emit("switchToCatalog", { orgFilter: orgName });
}

// ---------------------------------------------------------------------------
// Company profile page
// ---------------------------------------------------------------------------

export function getCompanySlugFromUrl() {
  try {
    var params = new URLSearchParams(window.location.search);
    return params.get("company") || null;
  } catch (e) {
    return null;
  }
}

export function openCompanyProfile(slug) {
  var c = getCompanyBySlug(slug);
  if (!c) return;
  // Exclusive with the vacancy detail overlay (U4 review): drop it before
  // showing the profile so the two can never stack (e.g. the vacancy page's
  // "link to parent company"). renderProfileForSlug also enforces this.
  state.currentVacancyId = null;
  var vd = document.getElementById("vacancyDetail");
  if (vd) {
    vd.classList.remove("active");
    vd.innerHTML = "";
  }
  state.currentProfileSlug = slug;
  var url = new URL(window.location);
  url.searchParams.set("company", slug);
  url.searchParams.delete("vacancy");
  // inApp mirrors U6's vacancy route (app.js openVacancyRoute): it marks that
  // a list sits beneath this entry, so closeCompanyProfile can step back
  // instead of pushing a bare entry that Back would then reopen the profile
  // from (the history-pollution wart U4's review flagged).
  history.pushState({ company: slug, inApp: true }, "", url);
  renderProfileForSlug(slug);
}

export function closeCompanyProfile() {
  // Mirrors U6's closeDetail (app.js): step back through an in-app entry so
  // popstate restores the underlying list with no forward entry left to
  // reopen the profile; only a cold deep link (nothing beneath) gets a bare
  // replace-in-place.
  if (history.state && history.state.inApp) {
    history.back();
    return;
  }
  state.currentProfileSlug = null;
  var url = new URL(window.location);
  url.searchParams.delete("company");
  history.pushState({}, "", url);
  hideProfile();
}

export function renderProfileForSlug(slug) {
  var c = getCompanyBySlug(slug);
  if (!c) return;
  state.currentProfileSlug = slug;
  // The company profile and the vacancy detail are mutually exclusive overlays
  // (U4 review): showing one always drops the other, so the on("render") loop
  // can never paint both and a reload can't resolve to a stacked state.
  state.currentVacancyId = null;

  document.getElementById("catalogSection").classList.remove("active");
  document.getElementById("companiesSection").classList.remove("active");
  var vacancyEl = document.getElementById("vacancyDetail");
  if (vacancyEl) {
    vacancyEl.classList.remove("active");
    vacancyEl.innerHTML = "";
  }

  var statsPanel = document.getElementById("statsPanel");
  if (statsPanel) statsPanel.style.display = "none";

  var el = document.getElementById("companyProfile");
  el.innerHTML = buildCompanyProfilePage(c);
  el.classList.add("active");
}

export function hideProfile() {
  state.currentProfileSlug = null;
  var el = document.getElementById("companyProfile");
  if (el) {
    el.classList.remove("active");
    el.innerHTML = "";
  }

  var statsPanel = document.getElementById("statsPanel");
  if (statsPanel) statsPanel.style.display = "";

  if (state.currentMode === "catalog") {
    document.getElementById("catalogSection").classList.add("active");
  } else if (state.currentMode === "companies") {
    document.getElementById("companiesSection").classList.add("active");
  }
}

// ---------------------------------------------------------------------------
// Company profile page (U7 reorg, DHA-391)
//
// Reading order (design protocol §6 "Detail screens — Company"): header band
// on the material (back, score tile, name, tier, meta) → optional review
// banner (workflow actions) → sheet split into a reading column (about lede →
// fit analysis → WANT breakdown → strengths → risks → evidence → approach →
// experience match → deep analysis → verdict → applications & research → open
// roles + strong/moderate/weak distribution) and a quiet rail (facts · status
// · monitoring). A company with no WANT breakdown (never scored) renders the
// lighter evidence-list variant instead of bars (AE2) — never fabricated
// zeros. All score/tier coloring flows through the shared qualityBand/
// tierClass helpers (R1/R8) — WANT bars, the fit score and the roles
// distribution all use the SAME 70/50 bands, retiring this page's old private
// 75/55 thresholds.
//
// Split per KTD2: companyProfileHtml is a pure, exported function (company +
// pre-resolved roles + opts) that unit-tests under node --test; the DOM shell
// (renderProfileForSlug) is thin and resolves roles/review/monitoring state
// before calling it. Every externally-sourced string (about, evidence,
// strengths/risks, verdict, role titles, news titles) goes through escHtml;
// every href through safeUrl (R14).
// ---------------------------------------------------------------------------

// Strong/moderate/weak counts + percentages over a set of ALREADY-SCORED role
// numbers (callers filter out null scores first — an unscored role is neither
// strong nor weak, it's simply not counted). Uses the shared qualityBand so
// the strip can never drift from the WANT breakdown's own bands.
export function roleScoreDistribution(scores) {
  var counts = { strong: 0, moderate: 0, weak: 0 };
  for (var i = 0; i < scores.length; i++) {
    var band = qualityBand(scores[i]);
    if (band === "good") counts.strong++;
    else if (band === "moderate") counts.moderate++;
    else counts.weak++;
  }
  var total = scores.length;
  var pct = function (n) {
    return total ? (n / total) * 100 : 0;
  };
  return {
    strong: counts.strong,
    moderate: counts.moderate,
    weak: counts.weak,
    total: total,
    strongPct: pct(counts.strong),
    moderatePct: pct(counts.moderate),
    weakPct: pct(counts.weak),
  };
}

// A CSS var() reference for a score's band — used for inline bar/dot fills so
// the SAME three colors used by .q-good/.q-moderate/.q-weak text never drift
// from the solid fills a text-color class can't provide.
function _bandVar(score) {
  return "var(--q-" + qualityBand(score) + ")";
}

// ---------------------------------------------------------------------------
// Reading column — pure block builders. Each returns "" when its data is
// absent so the caller can filter(Boolean) without a placeholder per field.
// ---------------------------------------------------------------------------

function companyAboutLedeHtml(c, t) {
  var parts = [];
  if (c.description) {
    parts.push('<div class="vac-desc">' + escHtml(c.description) + "</div>");
  }
  if (c.executive_summary && c.executive_summary !== c.description) {
    parts.push(
      '<div class="vac-desc">' + escHtml(c.executive_summary) + "</div>",
    );
  }
  if (c.notes) {
    parts.push('<div class="cp-note">“' + escHtml(c.notes) + "”</div>");
  }
  if (c.recent_news && c.recent_news.length) {
    var newsRows = c.recent_news
      .map(function (n) {
        var u = safeUrl(n.url);
        return u
          ? '<a class="vac-loc vac-loc--link" href="' +
              escHtml(u) +
              '" target="_blank" rel="noopener">' +
              escHtml(n.title) +
              "</a>"
          : '<div class="vac-loc">' + escHtml(n.title) + "</div>";
      })
      .join("");
    parts.push(
      '<div class="cp-block"><div class="vac-section-label">' +
        escHtml(t("cp_recent_news", "Recent news")) +
        "</div>" +
        newsRows +
        "</div>",
    );
  }
  if (!parts.length) return "";
  return '<div class="cp-lede">' + parts.join("") + "</div>";
}

function companyFitScoreHtml(c, t) {
  if (c.alignment_score == null) return "";
  var band = qualityBand(c.alignment_score);
  return (
    '<div class="cp-block">' +
    '<div class="vac-section-label">' +
    escHtml(t("cp_fit_analysis", "Fit analysis")) +
    "</div>" +
    '<div class="cp-fit-row"><span class="cp-fit-score q-' +
    band +
    '">' +
    c.alignment_score +
    "</span>" +
    (c.alignment_label
      ? '<span class="cp-fit-label q-' +
        band +
        '">' +
        escHtml(c.alignment_label) +
        "</span>"
      : "") +
    "</div>" +
    '<div class="cp-bar"><div class="cp-bar-fill" style="width:' +
    c.alignment_score +
    "%;background:" +
    _bandVar(c.alignment_score) +
    '"></div></div>' +
    "</div>"
  );
}

function companyWantBarsHtml(c, t) {
  var fd = c.fit_dimensions;
  if (!fd) return "";
  var rows = "";
  for (var i = 0; i < WANT_DIMS.length; i++) {
    var dv = fd[WANT_DIMS[i][0]];
    if (dv == null) continue;
    var band = qualityBand(dv);
    rows +=
      '<div class="cp-bar-row">' +
      '<span class="cp-bar-dim-label">' +
      escHtml(WANT_DIMS[i][1]) +
      "</span>" +
      '<div class="cp-bar"><div class="cp-bar-fill" style="width:' +
      dv +
      "%;background:" +
      _bandVar(dv) +
      '"></div></div>' +
      '<span class="cp-bar-value q-' +
      band +
      '">' +
      dv +
      "</span>" +
      "</div>";
  }
  if (!rows) return "";
  var mpaNote = "";
  if (SHOW_MPA && c.mpa_prestige != null) {
    mpaNote =
      '<div class="cp-mpa-note">MPA ' +
      c.mpa_prestige +
      (c.composite_score != null
        ? " · composite " + Math.round(c.composite_score)
        : "") +
      "</div>";
  }
  return (
    '<div class="cp-block">' +
    '<div class="vac-section-label">' +
    escHtml(t("cp_want_breakdown", "Want breakdown")) +
    "</div>" +
    rows +
    mpaNote +
    "</div>"
  );
}

// Strengths (band="good") / Risks (band="weak") — same shape, different
// meaning color, matching the mock's green/crimson list blocks.
function companyListBlock(title, items, band) {
  if (!items || !items.length) return "";
  var rows = items
    .map(function (s) {
      return (
        '<div class="cp-list-item"><span class="cp-bullet cp-bullet--' +
        band +
        '"></span><div class="cp-list-text">' +
        escHtml(s) +
        "</div></div>"
      );
    })
    .join("");
  return (
    '<div class="cp-block"><div class="vac-section-label cp-block-title--' +
    band +
    '">' +
    escHtml(title) +
    "</div>" +
    rows +
    "</div>"
  );
}

// Collapsible sourced evidence, shown alongside a full WANT breakdown (not to
// be confused with companyEvidenceListHtml's AE2 variant for a company with
// NO breakdown at all).
function companyEvidenceDetails(c, t) {
  var ev = c.fit_evidence;
  if (!ev || !ev.length) return "";
  var items = ev
    .map(function (e) {
      if (!e) return "";
      var q = e.quote || "";
      if (q.length > 240) q = q.slice(0, 240) + "…";
      return (
        '<div class="cp-evidence-item">' +
        (e.source
          ? '<span class="cp-evidence-source">' + escHtml(e.source) + "</span>"
          : "") +
        '<strong class="cp-evidence-claim">' +
        escHtml(e.claim || "") +
        "</strong>" +
        (q ? '<div class="cp-evidence-quote">«' + escHtml(q) + "»</div>" : "") +
        "</div>"
      );
    })
    .join("");
  return (
    '<details class="cp-evidence"><summary class="vac-section-label">' +
    escHtml(t("cp_evidence", "Evidence")) +
    " (" +
    ev.length +
    ")</summary>" +
    items +
    "</details>"
  );
}

// AE2: a pending/never-scored company with raw evidence anchors but no
// alignment_score/fit_dimensions gets this lighter list instead of bars.
function companyEvidenceListHtml(c, t) {
  var ev = c.fit_evidence;
  if (!ev || !ev.length) return "";
  var rows = ev
    .map(function (e) {
      if (!e) return "";
      var text = e.claim || e.quote || "";
      if (!text) return "";
      return (
        '<div class="cp-list-item"><span class="cp-bullet"></span>' +
        '<div class="cp-list-text">' +
        (e.source
          ? '<span class="cp-evidence-source">' + escHtml(e.source) + "</span> "
          : "") +
        escHtml(text) +
        "</div></div>"
      );
    })
    .join("");
  if (!rows) return "";
  return (
    '<div class="cp-block"><div class="vac-section-label">' +
    escHtml(t("cp_why_tier", "Why this tier")) +
    "</div>" +
    rows +
    "</div>"
  );
}

function companyProseBlock(title, text) {
  if (!text) return "";
  return (
    '<div class="cp-block"><div class="vac-section-label">' +
    escHtml(title) +
    '</div><div class="cp-prose">' +
    escHtml(text) +
    "</div></div>"
  );
}

function companyDeepAnalysisHtml(c, t) {
  if (!c.md_content) return "";
  // .md-content (not .vac-desc) — this can be a full markdown document
  // (headers, tables, blockquotes), and only .md-content styles those.
  return (
    '<details class="cp-deep"><summary class="vac-section-label">' +
    escHtml(t("cp_deep_analysis", "Deep analysis")) +
    '</summary><div class="md-content">' +
    mdToHtml(c.md_content) +
    "</div></details>"
  );
}

function companyVerdictHtml(c) {
  if (!c.mission_verdict) return "";
  return (
    '<div class="cp-verdict q-moderate-bg">' +
    escHtml(c.mission_verdict) +
    "</div>"
  );
}

// Applications you've made + research collected in company_evidence. Content
// unchanged from the pre-U7 profile (still gated on either list being
// non-empty) — only the section's place in the reading order moves.
function companyApplicationsHtml(c, t) {
  var apps = c.applications || [];
  var research = c.research || [];
  if (apps.length === 0 && research.length === 0) return "";

  var inner = "";
  if (apps.length > 0) {
    inner +=
      '<div class="cp-app-list">' +
      apps
        .map(function (a) {
          var meta = [];
          if (a.channel) meta.push(escHtml(a.channel));
          if (a.applied_at) meta.push(escHtml(a.applied_at));
          var artifactKeys = a.artifacts ? Object.keys(a.artifacts) : [];
          var artifactsHtml = artifactKeys.length
            ? '<div class="cp-app-artifacts">' +
              artifactKeys
                .map(function (k) {
                  return (
                    '<span class="cp-app-artifact">' + escHtml(k) + "</span>"
                  );
                })
                .join("") +
              "</div>"
            : "";
          return (
            '<div class="cp-app-item">' +
            '<span class="cp-app-status cp-app-status-' +
            escHtml(a.status) +
            '">' +
            escHtml(t("app_status_" + a.status, a.status)) +
            "</span>" +
            (meta.length
              ? '<span class="cp-app-meta">' + meta.join(" · ") + "</span>"
              : "") +
            artifactsHtml +
            "</div>"
          );
        })
        .join("") +
      "</div>";
  }
  if (research.length > 0) {
    inner +=
      '<div class="cp-app-research-label">' +
      escHtml(t("cp_research", "Research")) +
      " (" +
      research.length +
      ")</div>" +
      '<div class="cp-app-list">' +
      research
        .map(function (r) {
          var label = escHtml(r.source || "research");
          var researchUrl = safeUrl(r.url);
          var body = researchUrl
            ? '<a href="' +
              escHtml(researchUrl) +
              '" target="_blank" rel="noopener">' +
              escHtml(r.url) +
              "</a>"
            : label;
          var when = r.fetched_at
            ? '<span class="cp-app-meta">' +
              escHtml(String(r.fetched_at).slice(0, 10)) +
              "</span>"
            : "";
          return (
            '<div class="cp-app-item"><span class="cp-app-source">' +
            label +
            "</span>" +
            body +
            when +
            "</div>"
          );
        })
        .join("") +
      "</div>";
  }
  return (
    '<div class="cp-block"><div class="vac-section-label">' +
    escHtml(t("cp_applications_research", "Applications & research")) +
    "</div>" +
    inner +
    "</div>"
  );
}

// A single open-role row — the whole row routes to the vacancy detail page
// (U4/U6 contract: context "company" so "Move to apply" never auto-advances
// through the company's role list, mirroring vacancyMoveToApply's context
// gate). The score tile and status chip reuse the SAME helpers the vacancy
// page and its own header use, so a role never reads two different colors
// depending on which screen you saw it from.
function companyRoleRowHtml(r, t) {
  var scoreCls =
    r.score == null
      ? "cp-role-score--none"
      : "q-" + qualityBand(r.score) + "-bg";
  var scoreTxt = r.score == null ? "—" : String(r.score);
  var metaParts = [];
  if (r.loc) metaParts.push(escHtml(r.loc));
  if (r.seenRaw) {
    metaParts.push(
      escHtml(t("vac_seen_prefix", "seen")) +
        " " +
        escHtml(relativeTime(r.seenRaw, t)),
    );
  }
  var chipText = statusChipLabel(r.status, t);
  var chip = chipText
    ? '<span class="vac-status-chip">' + escHtml(chipText) + "</span>"
    : "";
  var screenBadge = r.screenOnly
    ? screenScoreBadge({ screen_only_score: true })
    : "";
  return (
    '<div class="cp-role-row" onclick="openVacancyRoute(\'' +
    jsAttr(r.id) +
    "',{context:'company'})\">" +
    '<div class="cp-role-score ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</div>" +
    '<div class="cp-role-body"><div class="cp-role-title-row">' +
    '<span class="cp-role-title">' +
    escHtml(r.title) +
    "</span>" +
    chip +
    screenBadge +
    "</div>" +
    (metaParts.length
      ? '<div class="cp-role-meta">' + metaParts.join(" · ") + "</div>"
      : "") +
    "</div>" +
    '<span class="cp-role-arrow">›</span>' +
    "</div>"
  );
}

function companyRolesBlockHtml(c, roles, counts, t) {
  if (!roles.length) return "";

  var scored = [];
  for (var i = 0; i < roles.length; i++) {
    if (roles[i].score != null) scored.push(roles[i].score);
  }
  var distHtml = "";
  if (scored.length) {
    var d = roleScoreDistribution(scored);
    distHtml =
      '<div class="cp-dist"><div class="cp-dist-legend">' +
      (d.strong
        ? '<span class="cp-dist-legend-item"><span class="cp-dist-dot" style="background:var(--q-good)"></span>' +
          d.strong +
          " " +
          escHtml(t("cp_dist_strong", "strong")) +
          "</span>"
        : "") +
      (d.moderate
        ? '<span class="cp-dist-legend-item"><span class="cp-dist-dot" style="background:var(--q-moderate)"></span>' +
          d.moderate +
          " " +
          escHtml(t("cp_dist_moderate", "moderate")) +
          "</span>"
        : "") +
      (d.weak
        ? '<span class="cp-dist-legend-item"><span class="cp-dist-dot" style="background:var(--q-weak)"></span>' +
          d.weak +
          " " +
          escHtml(t("cp_dist_weak", "weak")) +
          "</span>"
        : "") +
      '</div><div class="cp-dist-bar">' +
      (d.strong
        ? '<div style="width:' +
          d.strongPct +
          '%;background:var(--q-good)"></div>'
        : "") +
      (d.moderate
        ? '<div style="width:' +
          d.moderatePct +
          '%;background:var(--q-moderate)"></div>'
        : "") +
      (d.weak
        ? '<div style="width:' +
          d.weakPct +
          '%;background:var(--q-weak)"></div>'
        : "") +
      "</div></div>";
  }

  var statsHtml =
    '<div class="cp-status-bar">' +
    '<span class="cp-stat cp-stat-vacancies"><span class="cp-stat-icon">💼</span> ' +
    roles.length +
    " " +
    escHtml(t("cp_stat_vacancies_suffix", "vacancies")) +
    "</span>" +
    (counts.liked > 0
      ? '<span class="cp-stat cp-stat-liked"><span class="cp-stat-icon">💚</span> ' +
        counts.liked +
        " " +
        escHtml(t("cp_stat_liked_suffix", "liked")) +
        "</span>"
      : "") +
    (counts.passed > 0
      ? '<span class="cp-stat cp-stat-passed"><span class="cp-stat-icon">👎</span> ' +
        counts.passed +
        " " +
        escHtml(t("cp_stat_passed_suffix", "passed")) +
        "</span>"
      : "") +
    (counts.unseen > 0
      ? '<span class="cp-stat cp-stat-unseen"><span class="cp-stat-icon">❓</span> ' +
        counts.unseen +
        " " +
        escHtml(t("cp_stat_unseen_suffix", "unseen")) +
        "</span>"
      : "") +
    (c.avg_llm_score != null
      ? '<span class="cp-stat"><span class="q-' +
        qualityBand(c.avg_llm_score) +
        '">' +
        Math.round(c.avg_llm_score) +
        "</span> " +
        escHtml(t("cp_stat_avg_suffix", "avg")) +
        "</span>"
      : "") +
    "</div>";

  var rows = roles
    .map(function (r) {
      return companyRoleRowHtml(r, t);
    })
    .join("");

  return (
    '<div class="cp-block">' +
    '<div class="cp-roles-header"><span class="vac-section-label">' +
    escHtml(t("cp_open_roles", "Open roles here")) +
    '</span><span class="cp-roles-count">' +
    roles.length +
    "</span></div>" +
    statsHtml +
    distHtml +
    '<div class="cp-roles-list">' +
    rows +
    "</div>" +
    '<span class="cp-view-all-link" onclick="viewOrgInCatalog(\'' +
    jsAttr(c.name) +
    "')\">" +
    escHtml(t("cp_view_all_browse", "View all in Browse")) +
    " →</span>" +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Rail — facts · status · monitoring (all pure).
// ---------------------------------------------------------------------------

function companyFactsHtml(c, t) {
  var facts = [];
  if (c.hq_location && c.hq_location !== "N/A")
    facts.push([t("cp_hq", "HQ"), escHtml(c.hq_location)]);
  if (c.offices) facts.push([t("cp_offices", "Offices"), escHtml(c.offices)]);
  if (c.employee_count && c.employee_count !== "N/A")
    facts.push([t("cp_size", "Size"), escHtml(c.employee_count)]);
  if (c.founded_year && c.founded_year !== "N/A")
    facts.push([t("cp_founded", "Founded"), escHtml(c.founded_year)]);
  if (c.funding_status && c.funding_status !== "N/A")
    facts.push([t("cp_funding", "Funding"), escHtml(c.funding_status)]);
  if (c.sector && c.sector !== c.category)
    facts.push([t("cp_sector", "Sector"), escHtml(c.sector)]);
  if (c.glassdoor_rating != null)
    facts.push(["Glassdoor", c.glassdoor_rating + "/5"]);
  if (c.linkedin_employees)
    facts.push([
      "LinkedIn",
      escHtml(c.linkedin_employees) +
        " " +
        escHtml(t("cp_employees_suffix", "employees")),
    ]);
  if (c.experience_match != null)
    facts.push([
      t("cp_exp_rating", "Experience rating"),
      c.experience_match + "/10",
    ]);
  if (c.personal_interest != null)
    facts.push([
      t("cp_interest_rating", "Interest rating"),
      c.personal_interest + "/10",
    ]);
  var websiteUrl = safeUrl(c.website);
  if (websiteUrl) {
    facts.push([
      t("cp_website", "Website"),
      '<a class="cp-fact-link" href="' +
        escHtml(websiteUrl) +
        '" target="_blank" rel="noopener">' +
        escHtml(t("cp_visit", "Visit")) +
        " ↗</a>",
    ]);
  }
  var careersUrl = safeUrl(c.careers_url);
  if (careersUrl) {
    facts.push([
      t("cp_careers", "Careers"),
      '<a class="cp-fact-link" href="' +
        escHtml(careersUrl) +
        '" target="_blank" rel="noopener">' +
        escHtml(t("cp_visit", "Visit")) +
        " ↗</a>",
    ]);
  }
  if (!facts.length) return "";
  return (
    '<div class="vac-rail-group"><div class="vac-section-label">' +
    escHtml(t("vac_facts", "Facts")) +
    "</div>" +
    facts
      .map(function (f) {
        return (
          '<div class="vac-fact"><span class="vac-fact-k">' +
          escHtml(f[0]) +
          '</span><span class="vac-fact-v">' +
          f[1] +
          "</span></div>"
        );
      })
      .join("") +
    "</div>"
  );
}

function companyStatusPillHtml(reviewStatus, t) {
  var STATUS_MAP = {
    approved: ["q-good-bg", t("cp_review_approved", "Approved")],
    pending: ["q-moderate-bg", t("cp_review_pending", "Pending review")],
    rejected: ["q-weak-bg", t("cp_review_archived", "Archived")],
  };
  var m = STATUS_MAP[reviewStatus] || STATUS_MAP.pending;
  return (
    '<div class="vac-rail-group"><div class="vac-section-label">' +
    escHtml(t("apps_col_status", "Status")) +
    '</div><span class="cp-status-pill ' +
    m[0] +
    '">' +
    escHtml(m[1]) +
    "</span></div>"
  );
}

function companyMonitoringHtml(c, monStatus, t) {
  var rows = "";
  if (c.strategy) {
    rows +=
      '<div class="vac-fact"><span class="vac-fact-k">' +
      escHtml(t("cp_strategy", "Strategy")) +
      '</span><span class="vac-fact-v">' +
      escHtml(c.strategy) +
      "</span></div>";
  }
  if (c.last_fetched) {
    rows +=
      '<div class="vac-fact"><span class="vac-fact-k">' +
      escHtml(t("cp_last_fetched", "Last fetched")) +
      '</span><span class="vac-fact-v">' +
      escHtml(c.last_fetched.slice(0, 10)) +
      "</span></div>";
  }
  var statusRow = monStatus
    ? '<div class="cp-mon-status" title="' +
      escHtml(monStatus.tooltip || "") +
      '"><span class="mon-dot ' +
      monStatus.dotCls +
      '"></span><span>' +
      escHtml(monStatus.label) +
      "</span></div>"
    : "";
  if (!rows && !statusRow) return "";
  return (
    '<div class="vac-rail-group"><div class="vac-section-label">' +
    escHtml(t("col_monitoring", "Monitoring")) +
    "</div>" +
    rows +
    statusRow +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Page assembly (pure) — c + pre-resolved roles + opts. No DOM/state read, so
// it renders identically in a test as in the browser.
// ---------------------------------------------------------------------------

export function companyProfileHtml(c, roles, opts) {
  var o = opts || {};
  var t =
    o.t ||
    function (k, fb) {
      return fb !== undefined ? fb : k;
    };
  var reviewStatus = o.reviewStatus || "pending";
  var monStatus = o.monStatus || null;
  var counts = o.counts || { liked: 0, passed: 0, unseen: 0 };

  var score = c.alignment_score;
  var scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  var scoreTxt = score == null ? "—" : String(score);
  var tierHtml = c.calculated_tier
    ? '<span class="vac-tier ' +
      tierClass(c.calculated_tier) +
      '">' +
      escHtml(c.calculated_tier) +
      "</span>"
    : "";
  var subtitle = c.category
    ? '<div class="cp-subtitle">' + escHtml(c.category) + "</div>"
    : "";

  var header =
    '<button class="vac-back" onclick="closeCompanyProfile()">← ' +
    escHtml(t("vac_back", "Back")) +
    "</button>" +
    '<div class="vac-header-main">' +
    '<div class="vac-score-tile ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</div>" +
    '<div class="vac-header-text"><div class="vac-title-row">' +
    '<h1 class="vac-title">' +
    escHtml(c.name) +
    "</h1>" +
    tierHtml +
    "</div>" +
    subtitle +
    "</div></div>";

  var banner = "";
  if (reviewStatus === "pending" && c.company_id) {
    var cid = jsAttr(c.company_id);
    banner =
      '<div class="cp-review-banner q-moderate-bg">' +
      '<span class="cp-review-label">' +
      escHtml(t("cp_review_pending", "Pending review")) +
      "</span>" +
      '<button class="vac-btn vac-btn--like" onclick="reviewCompany(\'' +
      cid +
      "','approve')\">" +
      escHtml(t("btn_approve", "Approve")) +
      "</button>" +
      '<button class="vac-btn vac-btn--pass" onclick="reviewCompany(\'' +
      cid +
      "','reject')\">" +
      escHtml(t("btn_reject", "Reject")) +
      "</button></div>";
  } else if (reviewStatus === "approved" && c.company_id) {
    var cidA = jsAttr(c.company_id);
    banner =
      '<div class="cp-review-banner q-good-bg">' +
      '<span class="cp-review-label">' +
      escHtml(t("cp_review_approved", "Approved")) +
      "</span>" +
      '<button class="vac-btn vac-btn--pass" onclick="reviewCompany(\'' +
      cidA +
      "','reject')\">" +
      escHtml(t("cp_archive", "Archive")) +
      "</button></div>";
  } else if (reviewStatus === "rejected" && c.company_id) {
    var cidR = jsAttr(c.company_id);
    banner =
      '<div class="cp-review-banner q-weak-bg">' +
      '<span class="cp-review-label">' +
      escHtml(t("cp_review_archived", "Archived")) +
      "</span>" +
      '<button class="vac-btn vac-btn--like" onclick="reviewCompany(\'' +
      cidR +
      "','approve')\">" +
      escHtml(t("cp_restore", "Restore to active")) +
      "</button></div>";
  }

  var hasDims = !!(
    c.fit_dimensions &&
    Object.keys(c.fit_dimensions).some(function (k) {
      return c.fit_dimensions[k] != null;
    })
  );
  var hasAnalysis = !!(c.is_enriched && c.alignment_score != null);
  var hasEvidence = !hasAnalysis && !!(c.fit_evidence && c.fit_evidence.length);

  var readParts = [companyAboutLedeHtml(c, t)];
  if (hasAnalysis) {
    readParts.push(companyFitScoreHtml(c, t));
    if (hasDims) readParts.push(companyWantBarsHtml(c, t));
    readParts.push(
      companyListBlock(t("cp_strengths", "Strengths"), c.fit_strengths, "good"),
    );
    readParts.push(
      companyListBlock(t("cp_risks", "Risks"), c.fit_risks, "weak"),
    );
    readParts.push(companyEvidenceDetails(c, t));
    readParts.push(
      companyProseBlock(t("cp_approach", "Approach"), c.fit_approach),
    );
    readParts.push(
      companyProseBlock(
        t("cp_experience_match", "Experience match"),
        c.experience_reasoning,
      ),
    );
    readParts.push(companyDeepAnalysisHtml(c, t));
    readParts.push(companyVerdictHtml(c));
  } else if (hasEvidence) {
    readParts.push(companyEvidenceListHtml(c, t));
  } else {
    readParts.push(
      '<div class="cp-empty-fit">' +
        escHtml(t("cp_no_fit_data", "Run /enrich to add mission fit data")) +
        "</div>",
    );
  }
  readParts.push(companyApplicationsHtml(c, t));
  readParts.push(companyRolesBlockHtml(c, roles, counts, t));

  var reading =
    '<div class="vac-reading">' + readParts.filter(Boolean).join("") + "</div>";

  var railParts = [
    companyFactsHtml(c, t),
    companyStatusPillHtml(reviewStatus, t),
    companyMonitoringHtml(c, monStatus, t),
  ];
  var rail =
    '<aside class="vac-rail">' +
    railParts.filter(Boolean).join("") +
    "</aside>";

  return (
    '<div class="vac-page">' +
    header +
    banner +
    '<div class="vac-sheet">' +
    reading +
    rail +
    "</div></div>"
  );
}

// ---------------------------------------------------------------------------
// DOM shell — thin: sort this company's roles from live state, resolve
// review/monitoring status, call the pure builder above.
// ---------------------------------------------------------------------------

// Composite sort: status group first (liked/apply-track ahead of unseen ahead
// of passed), then score DESC within group — unchanged from the pre-U7 list.
var ROLE_STATUS_GROUP = {
  liked: 0,
  to_apply: 0,
  to_research: 0,
  to_network: 0,
  applied: 0,
  unseen: 1,
  passed: 2,
  skipped: 2,
};

function getCompanyRoles(c) {
  if (!c.vacancy_ids || !c.vacancy_ids.length) return [];
  var ids = c.vacancy_ids.slice().sort(function (a, b) {
    var stA = (state.dbData[a] && state.dbData[a].status) || "unseen";
    var stB = (state.dbData[b] && state.dbData[b].status) || "unseen";
    var grpDiff =
      (ROLE_STATUS_GROUP[stA] != null ? ROLE_STATUS_GROUP[stA] : 1) -
      (ROLE_STATUS_GROUP[stB] != null ? ROLE_STATUS_GROUP[stB] : 1);
    if (grpDiff !== 0) return grpDiff;
    var ga = groupsById.get(a);
    var gb = groupsById.get(b);
    var sa = ga && ga.llm_score != null ? ga.llm_score : -1;
    var sb = gb && gb.llm_score != null ? gb.llm_score : -1;
    return sb - sa;
  });

  var roles = [];
  for (var i = 0; i < ids.length; i++) {
    var g = groupsById.get(ids[i]);
    if (!g) continue;
    var loc = (g.locations || []).find(function (l) {
      return l && l.location;
    });
    roles.push({
      id: g.id,
      title: g.title,
      score: g.llm_score,
      screenOnly: !!g.screen_only_score,
      status: (state.dbData[g.id] && state.dbData[g.id].status) || "unseen",
      loc: loc ? loc.location : "",
      seenRaw: g.last_seen || g.first_seen || "",
    });
  }
  return roles;
}

function buildCompanyProfilePage(c) {
  var roles = getCompanyRoles(c);
  var counts = getCompanyStatusCounts(c.vacancy_ids);
  return companyProfileHtml(c, roles, {
    t: T,
    reviewStatus: _getReviewStatus(c),
    monStatus: _getMonitoringStatus(c),
    counts: counts,
  });
}
