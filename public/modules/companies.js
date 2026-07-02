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
  ratingDotsHtml,
  parseLocationChips,
  renderLocationChips,
  mdToHtml,
  getFlagForChip,
  formatDeadlineHtml,
  isVacancyExpired,
} from "./helpers.js";
import { saveCompanyReview, showSyncStatus } from "./api.js";
import { T } from "./i18n.js";

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
    relativeTime(c.last_fetched) +
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
  state.currentProfileSlug = slug;
  var url = new URL(window.location);
  url.searchParams.set("company", slug);
  history.pushState({ company: slug }, "", url);
  renderProfileForSlug(slug);
}

export function closeCompanyProfile() {
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

  document.getElementById("catalogSection").classList.remove("active");
  document.getElementById("companiesSection").classList.remove("active");

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
// Shared helpers: score bar + vacancy list
// ---------------------------------------------------------------------------

function buildScoreBarHtml(c) {
  if (!c.vacancy_ids || c.vacancy_ids.length === 0) return "";
  var buckets = { excellent: 0, good: 0, partial: 0, weak: 0 };
  var scoredTotal = 0;
  for (var i = 0; i < c.vacancy_ids.length; i++) {
    var g = groupsById.get(c.vacancy_ids[i]);
    var score = g ? g.llm_score : null;
    if (score != null) {
      scoredTotal++;
      if (score >= 75) buckets.excellent++;
      else if (score >= 55) buckets.good++;
      else if (score >= 35) buckets.partial++;
      else buckets.weak++;
    }
  }
  if (scoredTotal === 0) return "";
  var pct = function (n) {
    return ((n / scoredTotal) * 100).toFixed(1) + "%";
  };
  return (
    '<div class="score-distribution-legend">' +
    (buckets.excellent
      ? '<span class="score-legend-item"><span class="score-legend-dot score-bar-excellent"></span>' +
        buckets.excellent +
        " excellent</span>"
      : "") +
    (buckets.good
      ? '<span class="score-legend-item"><span class="score-legend-dot score-bar-good"></span>' +
        buckets.good +
        " good</span>"
      : "") +
    (buckets.partial
      ? '<span class="score-legend-item"><span class="score-legend-dot score-bar-partial"></span>' +
        buckets.partial +
        " partial</span>"
      : "") +
    (buckets.weak
      ? '<span class="score-legend-item"><span class="score-legend-dot score-bar-weak"></span>' +
        buckets.weak +
        " weak</span>"
      : "") +
    "</div>" +
    '<div class="score-distribution">' +
    (buckets.excellent
      ? '<div class="score-bar-segment score-bar-excellent" style="width:' +
        pct(buckets.excellent) +
        '"></div>'
      : "") +
    (buckets.good
      ? '<div class="score-bar-segment score-bar-good" style="width:' +
        pct(buckets.good) +
        '"></div>'
      : "") +
    (buckets.partial
      ? '<div class="score-bar-segment score-bar-partial" style="width:' +
        pct(buckets.partial) +
        '"></div>'
      : "") +
    (buckets.weak
      ? '<div class="score-bar-segment score-bar-weak" style="width:' +
        pct(buckets.weak) +
        '"></div>'
      : "") +
    "</div>"
  );
}

function buildVacancyListHtml(c, opts) {
  opts = opts || {};
  if (!c.vacancy_ids || c.vacancy_ids.length === 0) return "";

  // Composite sort: status group first, then score DESC within group
  var STATUS_GROUP = {
    liked: 0,
    to_apply: 0,
    to_research: 0,
    to_network: 0,
    applied: 0,
    unseen: 1,
    passed: 2,
    skipped: 2,
  };
  var sortedIds = c.vacancy_ids.slice().sort(function (a, b) {
    var stA = (state.dbData[a] && state.dbData[a].status) || "unseen";
    var stB = (state.dbData[b] && state.dbData[b].status) || "unseen";
    var grpDiff = (STATUS_GROUP[stA] || 1) - (STATUS_GROUP[stB] || 1);
    if (grpDiff !== 0) return grpDiff;
    var ga = groupsById.get(a);
    var gb = groupsById.get(b);
    var sa = ga && ga.llm_score != null ? ga.llm_score : -1;
    var sb = gb && gb.llm_score != null ? gb.llm_score : -1;
    return sb - sa;
  });

  var rows = [];
  for (var i = 0; i < sortedIds.length; i++) {
    var id = sortedIds[i];
    var g = groupsById.get(id);
    if (!g) continue;
    var status = (state.dbData[id] && state.dbData[id].status) || "unseen";
    var firstUrl = "";
    if (g.locations) {
      for (var j = 0; j < g.locations.length; j++) {
        if (g.locations[j].url) {
          firstUrl = g.locations[j].url;
          break;
        }
      }
    }
    if (!firstUrl) firstUrl = g.org_url || "";
    var titleHtml = firstUrl
      ? '<a href="' +
        escHtml(firstUrl) +
        '" target="_blank" rel="noopener">' +
        escHtml(g.title) +
        "</a>"
      : escHtml(g.title);

    var scoreBadge = llmScoreBadge(g.llm_score) + screenScoreBadge(g);
    var actionBtn = "";

    if (!opts.readOnly) {
      var mids = JSON.stringify(g.member_ids).replace(/"/g, "&quot;");
      if (status === "unseen") {
        actionBtn =
          '<button class="thumb-btn like" onclick="event.stopPropagation();companyVacancyAction(\'' +
          id +
          "'," +
          mids +
          ',\'liked\')" title="Like">\uD83D\uDC4D</button>' +
          '<button class="thumb-btn pass" onclick="event.stopPropagation();companyVacancyAction(\'' +
          id +
          "'," +
          mids +
          ',\'passed\')" title="Pass">\uD83D\uDC4E</button>';
      } else if (status === "liked") {
        actionBtn =
          '<button class="thumb-btn pass" onclick="event.stopPropagation();companyVacancyAction(\'' +
          id +
          "'," +
          mids +
          ',\'passed\')" title="Pass">\uD83D\uDC4E</button>';
      } else {
        actionBtn =
          '<button class="thumb-btn like" onclick="event.stopPropagation();companyVacancyAction(\'' +
          id +
          "'," +
          mids +
          ',\'liked\')" title="Like">\uD83D\uDC4D</button>';
      }
    }

    var rowClass = opts.readOnly ? "cp-vacancy-row" : "company-vacancy-row";
    rows.push(
      '<div class="' +
        rowClass +
        '">' +
        '<span class="company-vacancy-status-dot dot-' +
        status +
        '"></span>' +
        '<div class="company-vacancy-title">' +
        titleHtml +
        "</div>" +
        scoreBadge +
        (actionBtn
          ? '<div class="company-vacancy-actions">' + actionBtn + "</div>"
          : "") +
        "</div>",
    );
  }
  return rows.length > 0
    ? '<div class="company-vacancies-list">' + rows.join("") + "</div>"
    : "";
}

// ---------------------------------------------------------------------------
// Company profile sections (MECE)
// ---------------------------------------------------------------------------

function buildAboutSection(c) {
  var descHtml = "";
  if (c.description) {
    descHtml =
      '<div class="cp-about-desc">' + escHtml(c.description) + "</div>";
  }

  var facts = [];
  if (c.hq_location && c.hq_location !== "N/A")
    facts.push({ label: "HQ", value: escHtml(c.hq_location) });
  if (c.offices) facts.push({ label: "Offices", value: escHtml(c.offices) });
  if (c.employee_count && c.employee_count !== "N/A")
    facts.push({ label: "Size", value: escHtml(c.employee_count) });
  if (c.founded_year && c.founded_year !== "N/A")
    facts.push({ label: "Founded", value: escHtml(c.founded_year) });
  if (c.funding_status && c.funding_status !== "N/A")
    facts.push({ label: "Funding", value: escHtml(c.funding_status) });
  if (c.sector && c.sector !== c.category)
    facts.push({ label: "Sector", value: escHtml(c.sector) });
  if (c.glassdoor_rating != null)
    facts.push({ label: "Glassdoor", value: c.glassdoor_rating + "/5" });
  if (c.linkedin_employees)
    facts.push({
      label: "LinkedIn",
      value: escHtml(c.linkedin_employees) + " employees",
    });

  var factsHtml = "";
  if (facts.length > 0) {
    factsHtml = '<div class="cp-facts-grid">';
    for (var i = 0; i < facts.length; i++) {
      factsHtml +=
        '<div class="cp-facts-item"><div class="cp-facts-label">' +
        facts[i].label +
        '</div><div class="cp-facts-value">' +
        facts[i].value +
        "</div></div>";
    }
    factsHtml += "</div>";
  }

  var newsHtml = "";
  if (c.recent_news && c.recent_news.length) {
    newsHtml =
      '<div class="cp-about-news"><div class="cp-facts-label" style="margin-bottom:6px">Recent News</div>';
    for (var ni = 0; ni < c.recent_news.length; ni++) {
      var news = c.recent_news[ni];
      if (news.url) {
        newsHtml +=
          '<div style="padding:2px 0"><a href="' +
          escHtml(news.url) +
          '" target="_blank" rel="noopener" class="cp-facts-link">' +
          escHtml(news.title) +
          "</a></div>";
      } else {
        newsHtml +=
          '<div style="padding:2px 0;font-size:var(--text-sm);color:var(--text)">' +
          escHtml(news.title) +
          "</div>";
      }
    }
    newsHtml += "</div>";
  }

  if (!descHtml && !factsHtml && !newsHtml) return "";

  return (
    '<div class="cp-section">' +
    '<div class="cp-section-title"><span class="cp-section-icon">\uD83C\uDFE2</span> About</div>' +
    descHtml +
    factsHtml +
    newsHtml +
    "</div>"
  );
}

function buildFitSection(c) {
  var hasEnrichment = c.is_enriched && c.alignment_score != null;
  var hasRatings = c.experience_match != null || c.personal_interest != null;
  var hasComment = !!c.notes;
  var hasSummary = !!c.executive_summary;

  if (!hasEnrichment && !hasRatings && !hasComment && !hasSummary) {
    return (
      '<div class="cp-placeholder">' +
      '<div class="cp-section-title"><span class="cp-section-icon">\uD83C\uDFAF</span> Fit Analysis</div>' +
      '<div class="cp-placeholder-text">Run /enrich to add mission fit data</div></div>'
    );
  }

  var inner = "";

  if (hasRatings) {
    inner += '<div class="cp-ratings" style="margin-bottom:16px">';
    if (c.experience_match != null) {
      inner +=
        '<div class="cp-rating-item"><span>Experience Match</span>' +
        ratingDotsHtml(c.experience_match) +
        "</div>";
    }
    if (c.personal_interest != null) {
      inner +=
        '<div class="cp-rating-item"><span>Personal Interest</span>' +
        ratingDotsHtml(c.personal_interest) +
        "</div>";
    }
    inner += "</div>";
  }

  if (hasEnrichment) {
    var scoreColor =
      c.alignment_score >= 75
        ? "#059669"
        : c.alignment_score >= 55
          ? "#0284C7"
          : c.alignment_score >= 35
            ? "#D97706"
            : "#DC2626";
    inner +=
      '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">' +
      '<div style="font-size:28px;font-weight:700;color:' +
      scoreColor +
      '">' +
      c.alignment_score +
      "</div>" +
      '<div style="flex:1">' +
      '<div style="font-size:13px;color:#6B7280;margin-bottom:4px">' +
      escHtml(c.alignment_label || "") +
      "</div>" +
      '<div style="background:#E5E7EB;border-radius:4px;height:8px;overflow:hidden">' +
      '<div style="background:' +
      scoreColor +
      ";height:100%;width:" +
      c.alignment_score +
      '%;border-radius:4px"></div></div></div></div>';

    if (c.fit_dimensions && Object.keys(c.fit_dimensions).length) {
      // Derive the breakdown rows from the canonical factor set (WANT_DIMS) so
      // the modal never drifts from the table/columns. [field, full label].
      var DIMS = WANT_DIMS.map(function (d) {
        return [d[0], d[1]];
      });
      inner +=
        '<div style="margin-bottom:16px"><div style="font-size:12px;font-weight:600;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">WANT breakdown</div>';
      for (var di = 0; di < DIMS.length; di++) {
        var dv = c.fit_dimensions[DIMS[di][0]];
        if (dv == null) continue;
        var dc =
          dv >= 75
            ? "#059669"
            : dv >= 55
              ? "#0284C7"
              : dv >= 35
                ? "#D97706"
                : "#DC2626";
        inner +=
          '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">' +
          '<div style="width:78px;font-size:12px;color:#6B7280">' +
          DIMS[di][1] +
          "</div>" +
          '<div style="flex:1;background:#E5E7EB;border-radius:4px;height:8px;overflow:hidden">' +
          '<div style="background:' +
          dc +
          ";height:100%;width:" +
          dv +
          '%;border-radius:4px"></div></div>' +
          '<div style="width:24px;font-size:12px;font-weight:600;color:' +
          dc +
          ';text-align:right">' +
          dv +
          "</div></div>";
      }
      inner += "</div>";
    }

    if (SHOW_MPA && c.mpa_prestige != null) {
      inner +=
        '<div style="display:flex;gap:24px;margin-bottom:16px;font-size:13px">' +
        '<div><span style="color:#6B7280">MPA Prestige:</span> <strong>' +
        c.mpa_prestige +
        "</strong></div>" +
        (c.composite_score != null
          ? '<div><span style="color:#6B7280">Composite:</span> <strong>' +
            Math.round(c.composite_score) +
            "</strong></div>"
          : "") +
        "</div>";
    }

    if (c.fit_strengths && c.fit_strengths.length) {
      inner +=
        '<div style="margin-bottom:12px"><div style="font-size:12px;font-weight:600;color:#059669;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Strengths</div>';
      for (var si = 0; si < c.fit_strengths.length; si++) {
        inner +=
          '<div style="font-size:13px;color:#374151;padding:4px 0;line-height:1.4">\u2022 ' +
          escHtml(c.fit_strengths[si]) +
          "</div>";
      }
      inner += "</div>";
    }

    if (c.fit_risks && c.fit_risks.length) {
      inner +=
        '<div style="margin-bottom:12px"><div style="font-size:12px;font-weight:600;color:#DC2626;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Risks</div>';
      for (var ri = 0; ri < c.fit_risks.length; ri++) {
        inner +=
          '<div style="font-size:13px;color:#374151;padding:4px 0;line-height:1.4">\u2022 ' +
          escHtml(c.fit_risks[ri]) +
          "</div>";
      }
      inner += "</div>";
    }

    if (c.fit_evidence && c.fit_evidence.length) {
      inner +=
        '<details style="margin-bottom:12px"><summary style="font-size:12px;font-weight:600;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;cursor:pointer">Evidence (' +
        c.fit_evidence.length +
        ")</summary>";
      for (var evi = 0; evi < c.fit_evidence.length; evi++) {
        var ev = c.fit_evidence[evi];
        if (!ev) continue;
        var q = ev.quote || "";
        if (q.length > 240) q = q.slice(0, 240) + "…";
        inner +=
          '<div style="font-size:12px;padding:6px 0;border-bottom:1px solid #F3F4F6">' +
          '<span style="background:#EEF2FF;color:#3730A3;font-size:11px;padding:1px 6px;border-radius:4px;margin-right:6px">' +
          escHtml(ev.source || "") +
          "</span>" +
          '<strong style="color:#374151">' +
          escHtml(ev.claim || "") +
          "</strong>" +
          (q
            ? '<div style="color:#6B7280;font-style:italic;margin-top:2px">«' +
              escHtml(q) +
              "»</div>"
            : "") +
          "</div>";
      }
      inner += "</details>";
    }

    if (c.fit_approach) {
      inner +=
        '<div style="margin-bottom:12px"><div style="font-size:12px;font-weight:600;color:#6B7280;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Approach</div>' +
        '<div style="font-size:13px;color:#374151;line-height:1.4">' +
        escHtml(c.fit_approach) +
        "</div></div>";
    }

    if (c.experience_reasoning) {
      inner +=
        '<div style="margin-bottom:12px"><div style="font-size:12px;font-weight:600;color:#0284C7;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">Experience Match</div>' +
        '<div style="font-size:13px;color:#374151;line-height:1.4">' +
        escHtml(c.experience_reasoning) +
        "</div></div>";
    }

    if (c.mission_verdict) {
      inner +=
        '<div style="background:#FEF3C7;border-radius:8px;padding:12px 16px;margin-top:8px">' +
        '<div style="font-size:13px;color:#92400E;line-height:1.5;font-style:italic">' +
        escHtml(c.mission_verdict) +
        "</div></div>";
    }
  }

  if (hasSummary) {
    inner +=
      '<div class="company-detail-summary" style="margin-top:12px;margin-bottom:0">' +
      escHtml(c.executive_summary) +
      "</div>";
  }

  if (hasComment) {
    inner +=
      '<div class="cp-comment" style="margin-top:12px">\u201C' +
      escHtml(c.notes) +
      "\u201D</div>";
  }

  return (
    '<div class="cp-section">' +
    '<div class="cp-section-title"><span class="cp-section-icon">\uD83C\uDFAF</span> Fit Analysis</div>' +
    inner +
    "</div>"
  );
}

function buildVacanciesSection(c) {
  var counts = getCompanyStatusCounts(c.vacancy_ids);
  if (c.vacancy_count === 0) return "";

  var inner = "";
  inner +=
    '<div class="cp-status-bar" style="margin-bottom:16px">' +
    '<span class="cp-stat cp-stat-vacancies"><span class="cp-stat-icon">\uD83D\uDCBC</span> ' +
    c.vacancy_count +
    " vacancies</span>" +
    (counts.liked > 0
      ? '<span class="cp-stat cp-stat-liked"><span class="cp-stat-icon">\uD83D\uDC9A</span> ' +
        counts.liked +
        " liked</span>"
      : "") +
    (counts.passed > 0
      ? '<span class="cp-stat cp-stat-passed"><span class="cp-stat-icon">\uD83D\uDC4E</span> ' +
        counts.passed +
        " passed</span>"
      : "") +
    (counts.unseen > 0
      ? '<span class="cp-stat cp-stat-unseen"><span class="cp-stat-icon">\u2753</span> ' +
        counts.unseen +
        " unseen</span>"
      : "") +
    (c.avg_llm_score != null
      ? '<span class="cp-stat">' +
        llmScoreBadge(Math.round(c.avg_llm_score)) +
        " avg</span>"
      : "") +
    "</div>";

  if (counts.liked > 0 && c.vacancy_ids) {
    var likedRows = [];
    for (var i = 0; i < c.vacancy_ids.length; i++) {
      var id = c.vacancy_ids[i];
      var st = (state.dbData[id] && state.dbData[id].status) || "unseen";
      if (st !== "liked") continue;
      var g = groupsById.get(id);
      if (!g) continue;
      // Skip expired (closed) vacancies from the liked inline list
      if (isVacancyExpired(g)) continue;
      var firstUrl = "";
      if (g.locations) {
        for (var j = 0; j < g.locations.length; j++) {
          if (g.locations[j].url) {
            firstUrl = g.locations[j].url;
            break;
          }
        }
      }
      if (!firstUrl) firstUrl = g.org_url || "";
      var titleHtml = firstUrl
        ? '<a href="' +
          escHtml(firstUrl) +
          '" target="_blank" rel="noopener">' +
          escHtml(g.title) +
          "</a>"
        : escHtml(g.title);
      likedRows.push(
        '<div class="cp-vacancy-row">' +
          '<span class="company-vacancy-status-dot dot-liked"></span>' +
          '<div class="company-vacancy-title">' +
          titleHtml +
          "</div>" +
          llmScoreBadge(g.llm_score) +
          screenScoreBadge(g) +
          "</div>",
      );
    }
    if (likedRows.length > 0) {
      inner +=
        '<div class="cp-liked-inline">' +
        '<div class="cp-facts-label" style="margin-bottom:6px;color:var(--emerald)">\uD83D\uDC9A Liked</div>' +
        '<div class="company-vacancies-list">' +
        likedRows.join("") +
        "</div></div>";
    }
  }

  var scoreBar = buildScoreBarHtml(c);
  if (scoreBar) {
    inner +=
      '<div style="margin:16px 0 12px"><div class="cp-facts-label" style="margin-bottom:8px">Score Distribution</div>' +
      scoreBar +
      "</div>";
  }

  var vacList = buildVacancyListHtml(c, { readOnly: true });
  if (vacList) {
    inner +=
      '<div style="margin-top:8px"><div class="cp-facts-label" style="margin-bottom:8px">All Vacancies (' +
      c.vacancy_count +
      ")</div>" +
      vacList +
      "</div>";
  }

  return (
    '<div class="cp-section">' +
    '<div class="cp-section-title"><span class="cp-section-icon">\uD83D\uDCCB</span> Vacancies</div>' +
    inner +
    "</div>"
  );
}

function buildCompanyProfilePage(c) {
  var tierCls = c.calculated_tier
    ? "ctier-" + c.calculated_tier.toLowerCase()
    : "ctier-none";
  var tierLabel = c.calculated_tier || "\u2014";

  var linksHtml = "";
  if (c.website) {
    linksHtml +=
      '<a href="' +
      escHtml(c.website) +
      '" target="_blank" rel="noopener" class="cp-hero-link">Website</a>';
  }
  if (c.careers_url) {
    linksHtml +=
      '<a href="' +
      escHtml(c.careers_url) +
      '" target="_blank" rel="noopener" class="cp-hero-link">Careers</a>';
  }

  var atsHtml = "";
  var atsItems = [];
  if (c.strategy)
    atsItems.push(
      '<div class="cp-ats-item"><span class="cp-ats-label">Strategy</span><span class="cp-ats-value">' +
        escHtml(c.strategy) +
        "</span></div>",
    );
  if (c.last_fetched)
    atsItems.push(
      '<div class="cp-ats-item"><span class="cp-ats-label">Last Fetched</span><span class="cp-ats-value">' +
        escHtml(c.last_fetched.slice(0, 10)) +
        "</span></div>",
    );
  if (c.fetch_status)
    atsItems.push(
      '<div class="cp-ats-item"><span class="cp-ats-label">Fetch Status</span><span class="cp-ats-value">' +
        escHtml(c.fetch_status) +
        "</span></div>",
    );
  if (atsItems.length > 0) {
    atsHtml =
      '<div class="cp-section" style="opacity:0.7">' +
      '<div class="cp-section-title"><span class="cp-section-icon">\u2699\uFE0F</span> Monitoring</div>' +
      '<div class="cp-ats-grid">' +
      atsItems.join("") +
      "</div></div>";
  }

  var mdHtml = "";
  if (c.md_content) {
    mdHtml =
      '<div class="cp-section">' +
      '<div class="cp-section-title"><span class="cp-section-icon">\uD83D\uDD0D</span> Deep Analysis</div>' +
      '<div class="md-content">' +
      mdToHtml(c.md_content) +
      "</div></div>";
  }

  var catalogBtn =
    '<button class="company-view-catalog-btn" onclick="viewOrgInCatalog(\'' +
    jsAttr(c.name) +
    "')\">\u2192 Vacancies</button>";

  // Review banner for pending companies
  var reviewSt = _getReviewStatus(c);
  var reviewBanner = "";
  if (reviewSt === "pending" && c.company_id) {
    var cid = escHtml(c.company_id);
    reviewBanner =
      '<div class="cp-review-banner">' +
      '<span class="cp-review-label">Pending Review</span>' +
      '<button class="cr-btn cr-approve cr-btn-lg" onclick="reviewCompany(\'' +
      cid +
      "','approve')\">\u2713 Approve</button>" +
      '<button class="cr-btn cr-reject cr-btn-lg" onclick="reviewCompany(\'' +
      cid +
      "','reject')\">\u2717 Reject</button>" +
      "</div>";
  } else if (reviewSt === "approved" && c.company_id) {
    var cidA = escHtml(c.company_id);
    reviewBanner =
      '<div class="cp-review-banner cp-review-approved">' +
      '<span class="cp-review-label">\u2713 Approved</span>' +
      '<button class="cr-btn cr-reject cr-btn-lg" onclick="reviewCompany(\'' +
      cidA +
      "','reject')\">\u2717 Archive</button>" +
      "</div>";
  } else if (reviewSt === "rejected" && c.company_id) {
    var cidR = escHtml(c.company_id);
    reviewBanner =
      '<div class="cp-review-banner cp-review-rejected">' +
      '<span class="cp-review-label">\u2717 Archived</span>' +
      '<button class="cr-btn cr-approve cr-btn-lg" onclick="reviewCompany(\'' +
      cidR +
      "','approve')\">\u2713 Restore to active</button>" +
      "</div>";
  }

  return (
    '<div class="company-profile">' +
    '<button class="cp-back-btn" onclick="closeCompanyProfile()">\u2190 Back to Companies</button>' +
    reviewBanner +
    '<div class="cp-hero">' +
    '<div class="cp-hero-top">' +
    (c.logo_url
      ? '<img class="cp-hero-logo" src="' +
        escHtml(c.logo_url) +
        '" alt="" onerror="this.style.display=\'none\'">'
      : "") +
    '<span class="company-tier-badge ' +
    tierCls +
    '">' +
    tierLabel +
    "</span>" +
    '<span class="cp-hero-name">' +
    escHtml(c.name) +
    "</span>" +
    "</div>" +
    (c.category
      ? '<div class="cp-hero-category">' + escHtml(c.category) + "</div>"
      : "") +
    (linksHtml ? '<div class="cp-hero-links">' + linksHtml + "</div>" : "") +
    "</div>" +
    buildAboutSection(c) +
    buildFitSection(c) +
    buildVacanciesSection(c) +
    mdHtml +
    atsHtml +
    catalogBtn +
    "</div>"
  );
}
