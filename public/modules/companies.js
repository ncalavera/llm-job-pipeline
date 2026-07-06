// =============================================================================
// companies.js — Companies table + company profile page
// =============================================================================

import {
  state,
  groups,
  getCompanies,
  getCompanyBySlug,
  groupsById,
  STATUS_BASKET,
  STATUS_PRI,
  getGroupStatus,
  getCompanyStatusCounts,
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
  qualityClass,
  tierClass,
  safeUrl,
  isVacancyExpired,
} from "./helpers.js";
import { companyRollup } from "./derive.js";
import { saveCompanyReview, showSyncStatus } from "./api.js";
import { T } from "./i18n.js";
// statusChipLabel is U6's vacancy-status pill vocabulary (Liked/Passed/To
// apply/…) — reused here so an open role's status reads identically whether
// you're on its own detail page or in this company's role list (U7).
import { statusChipLabel } from "./vacancy.js";

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
// Per-company rollups — derived live from the raw roles (DHA-407/408)
// ---------------------------------------------------------------------------

// A company's raw roles: its role ids mapped through the group index, dropping
// ids not present in the shipped roles (unscored/archived). This is the SAME
// join the profile's getCompanyRoles uses, so the table numbers and the roles
// listed under a company can never disagree.
function _companyRoles(c) {
  var roles = [];
  var ids = c.vacancy_ids || [];
  for (var i = 0; i < ids.length; i++) {
    var g = groupsById.get(ids[i]);
    if (g) roles.push(g);
  }
  return roles;
}

// "deadline DD.MM" when the hottest role's deadline is within a week, else "".
// Mirrors _deadline_soon_label in scripts/report/data_prep.py — kept plain (not
// i18n), exactly as the pipeline baked it.
var HOT_DEADLINE_SOON_DAYS = 7;
function _hotDeadlineLabel(deadlineIso) {
  if (!deadlineIso) return "";
  var dl = new Date(String(deadlineIso).slice(0, 10));
  if (isNaN(dl.getTime())) return "";
  var today = new Date(new Date().toISOString().slice(0, 10));
  var days = Math.round((dl - today) / 86400000);
  if (days < 0 || days > HOT_DEADLINE_SOON_DAYS) return "";
  var dd = String(dl.getUTCDate()).padStart(2, "0");
  var mm = String(dl.getUTCMonth() + 1).padStart(2, "0");
  return "deadline " + dd + "." + mm;
}

// Decorate a company row with LIVE rollups derived from its raw roles, replacing
// the numbers the pipeline used to bake (DHA-407): vacancy_count, applyable_count,
// avg_llm_score and the hot-vacancy signal. Idempotent — safe to call per render.
function _decorateRollups(c) {
  var r = companyRollup(_companyRoles(c), {
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
  });
  c.vacancy_count = r.vacancy_count;
  c.applyable_count = r.applyable_count;
  c.avg_llm_score = r.avg_llm_score;
  c.hot_vacancy = r.hot
    ? { score: r.hot.score, deadline_label: _hotDeadlineLabel(r.hot.deadline) }
    : null;
  return c;
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
  renderCompanies();
}

// ---------------------------------------------------------------------------
// Sub-tab switching
// ---------------------------------------------------------------------------

export function switchCompanySubTab(tab) {
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
    // approved — WANT score descending (DHA-409), same default as pending.
    state.companySortCol = "fit";
    state.companySortAsc = false;
  }
  // Update tab button active state
  document.querySelectorAll(".company-sub-tab").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.subtab === tab);
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

// Companies belonging to `subTab` before any search/tier/card/chip filter —
// the basket size a sub-tab would show with every filter cleared. Shared by
// the "N shown" label and the empty-state flavor check below (R11).
function _subTabTotal(subTab) {
  var all = getCompanies();
  var n = 0;
  for (var i = 0; i < all.length; i++) {
    var rs = _getReviewStatus(all[i]);
    if (subTab === "approved" && rs === "approved") n++;
    else if (subTab === "pending" && rs === "pending") n++;
    else if (subTab === "archived" && rs === "rejected") n++;
  }
  return n;
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

  // Monitoring chip filter (approved tab only)
  if (subTab === "approved" && state.companyMonitorFilters.size > 0) {
    filtered = filtered.filter(function (c) {
      return state.companyMonitorFilters.has(_getMonitoringStatus(c).level);
    });
  }

  // Derive every per-company number (vacancy_count, applyable_count, avg score,
  // hot signal) live from its roles before sorting/rendering (DHA-407) — the
  // pipeline ships only the raw role-id list now, so the sort keys below and the
  // rendered cells read the same freshly-derived values.
  filtered.forEach(_decorateRollups);

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

// Retired (U8, DHA-392) the old 75/55/35 four-color scale in favor of the
// shared qualityBand/qualityClass helpers (70/50, three bands) — the same
// ones the company profile's WANT bars (U7) already use, so a dimension
// value never shows two colors depending on whether you're in the table or
// the profile.
export function _dimNumHtml(v) {
  if (v == null) return '<span class="ct-dim-empty">—</span>';
  return '<span class="ct-dim-num ' + qualityClass(v) + '">' + v + "</span>";
}

// First one or two initials of a company name, for the row monogram —
// mirrors vacancy.js's mini-card monogram (kept local: companies.js's file
// scope for this unit is style.css + itself, not vacancy.js).
function _initials(name) {
  return String(name || "")
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map(function (w) {
      return w[0] || "";
    })
    .join("")
    .toUpperCase();
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
    // "Open" surfaces applyable_count for the first time (U8) — the sort key
    // stays "applyable", already fully wired (this was previously the
    // approved tab's default sort with no visible header to click).
    {
      key: "applyable",
      label: T("col_open", "Open"),
      sortable: true,
      cls: "ct-col-open",
    },
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
      key: "offices",
      label: T("col_location", "Location"),
      sortable: false,
      cls: "ct-col-loc",
    },
    {
      key: "freshness",
      label: T("col_freshness", "Freshness"),
      sortable: true,
      cls: "ct-col-freshness",
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

// Approved tab shows no stat cards (post-ship fast fix: the maintainer
// dropped the Total/With new/Needs attention row — the counts it carried are
// not relocated anywhere else). Pending/Archived keep theirs.
function _renderStatsCards(filtered) {
  var statsEl = document.getElementById("companyEnrichmentStats");
  if (!statsEl) return;

  var subTab = state.companySubTab;

  if (subTab === "approved") {
    statsEl.innerHTML = "";
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
  // Get base list (before the monitoring-chip filter) for chip counts
  var savedMonitorFilters = state.companyMonitorFilters;
  state.companyMonitorFilters = new Set();
  var unfiltered = getFilteredSortedCompanies();
  state.companyMonitorFilters = savedMonitorFilters;
  // Get final filtered list (with the chip filter applied)
  var filtered = getFilteredSortedCompanies();

  _renderStatsCards(filtered);
  _renderPendingDisclaimer(unfiltered);
  _renderMonitoringChips(unfiltered);

  var shownEl = document.getElementById("companyShownCount");
  if (shownEl) {
    var hasChips = state.companyMonitorFilters.size > 0;
    if (hasChips) {
      shownEl.textContent = filtered.length + " shown";
    } else {
      var tabTotal = _subTabTotal(state.companySubTab);
      shownEl.textContent =
        filtered.length !== tabTotal ? filtered.length + " shown" : "";
    }
  }

  // Update sub-tab counts
  _updateSubTabCounts();

  if (filtered.length === 0) {
    var subTab = state.companySubTab;
    var query = (document.getElementById("companySearch").value || "").trim();
    var tierFilter = document.getElementById("companyTierFilter").value;
    var hasFilters = !!(
      query ||
      tierFilter ||
      (subTab === "approved" && state.companyMonitorFilters.size > 0)
    );
    var emptyMsg;
    if (getCompanies().length === 0) {
      // First-run-empty: no companies in the payload at all.
      emptyMsg = T("companies_empty", "No companies yet.");
    } else if (hasFilters && _subTabTotal(subTab) > 0) {
      // Filtered-to-zero: this tab has companies, but the active filters hide
      // all of them.
      emptyMsg = T("companies_no_match", "Nothing matches the filters");
    } else {
      // Basket-empty: this sub-tab (approved/pending/archived) has none,
      // regardless of filters — same "<tab> — no X" phrasing Browse uses.
      var subTabLabels = {
        approved: T("subtab_approved", "Approved"),
        pending: T("subtab_pending", "Pending Review"),
        archived: T("subtab_archived", "Archived"),
      };
      emptyMsg =
        (subTabLabels[subTab] || "") +
        " — " +
        T("companies_basket_empty", "no companies");
    }
    grid.innerHTML =
      '<div class="company-empty">\uD83C\uDFE2 ' + escHtml(emptyMsg) + "</div>";
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
      // tabindex + onkeydown make the header Tab-reachable and operable with
      // Enter/Space, not just a mouse click (R12: every interactive control
      // must be keyboard-reachable). aria-sort tells assistive tech the
      // current sort direction the visual arrow otherwise only shows sighted
      // users.
      var ariaSort = isActive
        ? state.companySortAsc
          ? "ascending"
          : "descending"
        : "none";
      thead +=
        '<th class="ct-th ' +
        c.cls +
        (isActive ? " ct-th-active" : "") +
        '"' +
        titleAttr +
        ' tabindex="0" aria-sort="' +
        ariaSort +
        '" onclick="sortCompanyTable(\'' +
        c.key +
        "')\" onkeydown=\"if(event.key==='Enter'||event.key===' '){event.preventDefault();sortCompanyTable('" +
        c.key +
        "')}\">" +
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

export function _buildRow(c) {
  var subTab = state.companySubTab;
  if (subTab === "pending") return _buildPendingRow(c);
  if (subTab === "archived") return _buildArchivedRow(c);
  return _buildApprovedRow(c);
}

// Row monogram (name+avatar per the mock): org-colored initials tile, same
// visual language as vacancy.js's company mini-card (U6).
function _monoHtml(c) {
  var fg = (c.org_color || ["#F97316"])[0];
  return (
    '<span class="ct-mono" style="background:' +
    escHtml(fg) +
    '">' +
    escHtml(_initials(c.name)) +
    "</span>"
  );
}

function _buildApprovedRow(c) {
  var counts = _counts(c);
  var tierHtml = c.calculated_tier
    ? '<span class="vac-tier ' +
      tierClass(c.calculated_tier) +
      '">' +
      escHtml(c.calculated_tier) +
      "</span>"
    : '<span class="ct-dim-empty">\u2014</span>';
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
    '<tr class="ct-row" tabindex="0" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\" onkeydown=\"if(event.key==='Enter'&&event.target===event.currentTarget){openCompanyProfile('" +
    jsAttr(c.slug) +
    "')}\">" +
    '<td class="ct-td ct-col-name"><span class="ct-name-wrap">' +
    _monoHtml(c) +
    '<span class="ct-name-text">' +
    escHtml(c.name) +
    "</span></span></td>" +
    '<td class="ct-td ct-col-tier">' +
    tierHtml +
    "</td>" +
    '<td class="ct-td ct-col-fit">' +
    fitBadge +
    "</td>" +
    _dimCellsHtml(c) +
    '<td class="ct-td ct-col-open">' +
    (c.applyable_count || 0) +
    "</td>" +
    '<td class="ct-td ct-col-liked">' +
    likedHtml +
    "</td>" +
    '<td class="ct-td ct-col-new">' +
    newHtml +
    "</td>" +
    '<td class="ct-td ct-col-loc"><span class="ct-location-text">' +
    locText +
    "</span></td>" +
    '<td class="ct-td ct-col-freshness">' +
    _freshnessHtml(c) +
    "</td>" +
    '<td class="ct-td ct-col-monitoring">' +
    _monitoringHtml(c) +
    "</td>" +
    '<td class="ct-td ct-col-review">' +
    '<button class="cr-btn cr-reject" onclick="event.stopPropagation();reviewCompany(\'' +
    jsAttr(c.company_id || "") +
    "','reject')\" title=\"Archive\">✗</button>" +
    "</td>" +
    "</tr>"
  );
}

function _buildPendingRow(c) {
  var fitBadge =
    c.alignment_score != null ? llmScoreBadge(c.alignment_score) : "\u2014";
  var locText = c.offices ? escHtml(c.offices) : "\u2014";
  var catText = c.category ? escHtml(c.category) : "\u2014";
  var sourceText = c.strategy ? escHtml(c.strategy) : "\u2014";

  var cid = jsAttr(c.company_id || "");
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
    '<tr class="ct-row" tabindex="0" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\" onkeydown=\"if(event.key==='Enter'&&event.target===event.currentTarget){openCompanyProfile('" +
    jsAttr(c.slug) +
    "')}\">" +
    '<td class="ct-td ct-col-name ct-col-name--pending"><span class="ct-name-wrap">' +
    _monoHtml(c) +
    '<span class="ct-name-text">' +
    escHtml(c.name) +
    "</span></span>" +
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
    '<tr class="ct-row ct-row--archived" tabindex="0" onclick="openCompanyProfile(\'' +
    jsAttr(c.slug) +
    "')\" onkeydown=\"if(event.key==='Enter'&&event.target===event.currentTarget){openCompanyProfile('" +
    jsAttr(c.slug) +
    "')}\">" +
    '<td class="ct-td ct-col-name ct-col-name--archived"><span class="ct-name-wrap">' +
    _monoHtml(c) +
    '<span class="ct-name-text">' +
    escHtml(c.name) +
    "</span></span></td>" +
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
    jsAttr(c.company_id || "") +
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

export function viewOrgInCatalog(orgName) {
  emit("switchToCatalog", { orgFilter: orgName });
}

// ---------------------------------------------------------------------------
// Company profile page
// ---------------------------------------------------------------------------

// Mirrors app.js's LEAF_SECTION_ID (not imported — no leaf module reaches back
// into app.js). Before the palette (U17) a company profile could only be
// opened from Browse or Companies, so renderProfileForSlug/hideProfile only
// ever needed to touch those two. The palette can open one from ANY section
// (Today, Boards, …), which exposed the gap: post-ship fast fix.
var LEAF_SECTION_ID = {
  today: "todaySection",
  catalog: "catalogSection",
  companies: "companiesSection",
  pipeline: "pipelineSection",
  stats: "statsSection",
  archive: "archiveSection",
  boards: "boardsSection",
  applications: "applicationsSection",
  settings: "settingsSection",
};

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

  Object.keys(LEAF_SECTION_ID).forEach(function (mode) {
    var el = document.getElementById(LEAF_SECTION_ID[mode]);
    if (el) el.classList.remove("active");
  });
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

  var restoreId = LEAF_SECTION_ID[state.currentMode];
  var restoreEl = restoreId && document.getElementById(restoreId);
  if (restoreEl) restoreEl.classList.add("active");
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
  return (
    '<div class="cp-block">' +
    '<div class="vac-section-label">' +
    escHtml(t("cp_want_breakdown", "Want breakdown")) +
    "</div>" +
    rows +
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
    '<div class="cp-role-row" role="button" tabindex="0" onclick="openVacancyRoute(\'' +
    jsAttr(r.id) +
    "',{context:'company'})\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openVacancyRoute('" +
    jsAttr(r.id) +
    "',{context:'company'})}\">" +
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
    '<span class="cp-view-all-link" role="button" tabindex="0" onclick="viewOrgInCatalog(\'' +
    jsAttr(c.name) +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();viewOrgInCatalog('" +
    jsAttr(c.name) +
    "')}\">" +
    escHtml(t("cp_view_all_browse", "View all in Browse")) +
    " →</span>" +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Rail — facts · status · monitoring (all pure).
// ---------------------------------------------------------------------------

function companyFactsHtml(c, t) {
  // Quiet 5-row facts block per the mock (DHA-412 #10): HQ, Size, Founded,
  // Funding, ATS. The deeper enrichment signals that had swollen this to ~12
  // rows (Offices, Sector, Glassdoor, LinkedIn, experience/interest ratings,
  // Website/Careers links) are dropped to keep the side rail quiet — the fit
  // ratings already surface in the reading column, and the ATS type also shows
  // under Monitoring, exactly as the mock has it.
  var facts = [];
  if (c.hq_location && c.hq_location !== "N/A")
    facts.push([t("cp_hq", "HQ"), escHtml(c.hq_location)]);
  if (c.employee_count && c.employee_count !== "N/A")
    facts.push([t("cp_size", "Size"), escHtml(c.employee_count)]);
  if (c.founded_year && c.founded_year !== "N/A")
    facts.push([t("cp_founded", "Founded"), escHtml(c.founded_year)]);
  if (c.funding_status && c.funding_status !== "N/A")
    facts.push([t("cp_funding", "Funding"), escHtml(c.funding_status)]);
  if (c.strategy) facts.push(["ATS", escHtml(c.strategy)]);
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
    // Approved is a settled state: its status shows once, in the rail pill
    // (companyStatusPillHtml) — no second full-width banner (DHA-412 #6, the
    // mock's single-source-of-truth placement). Archiving an approved company
    // stays available from the Companies list row's ✗ action. Only pending
    // (needs Approve/Reject) and rejected (needs Restore) keep a banner, since
    // those carry an action the rail pill can't.
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
  _decorateRollups(c); // avg score etc. derive live from the roles (DHA-407)
  var roles = getCompanyRoles(c);
  var counts = getCompanyStatusCounts(c.vacancy_ids);
  return companyProfileHtml(c, roles, {
    t: T,
    reviewStatus: _getReviewStatus(c),
    monStatus: _getMonitoringStatus(c),
    counts: counts,
  });
}
