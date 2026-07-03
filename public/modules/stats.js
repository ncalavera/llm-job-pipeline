// =============================================================================
// stats.js — Geo mode: vacancy distribution by city + country
// Aggregation re-runs on every render (live updates, ~1-2ms for ~1000 vacancies).
// =============================================================================

import {
  state,
  groups,
  STATUS_BASKET,
  getGroupStatus,
  isGroupCompanyApproved,
} from "./state.js";
import {
  escHtml,
  getFlagForChip,
  isVacancyExpired,
  qualityClass,
} from "./helpers.js";
import { T } from "./i18n.js";
import { geoBuckets, VISIBLE_MIN_SCORE } from "./derive.js";

const HELP_TEXT =
  "A vacancy with N locations is counted N times — the table shows role availability by city.";
const REMOTE_LABEL = T("geo_remote_unknown", "Remote / Unknown");
const COUNTRY_ONLY_LABEL = "(whole country)";
const EMPTY_LABEL = "🏢 No data";

// The shared visibility filter (approved + score floor) and the expiry
// re-bucketing behind the "liked" column live in derive.js — geoBuckets reads
// exactly the visible set the Catalog does, so the Geo numbers track live
// likes/passes and today's expiry with no run (DHA-376). The score floor honours
// the SAME state.catalogShowAll toggle the Catalog uses, so with "show all" on,
// Geo and Catalog agree on the sub-threshold roles instead of Geo hiding rows
// the Catalog shows (DHA-376 Nit B).
function visOpts() {
  return {
    isApproved: isGroupCompanyApproved,
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
    basketMap: STATUS_BASKET,
    minScore: state.catalogShowAll ? null : VISIBLE_MIN_SCORE,
  };
}

// ---------------------------------------------------------------------------
// Sort
// ---------------------------------------------------------------------------

function compareRows(a, b, col, asc) {
  // Pin Remote/Unknown to the bottom regardless of sort direction
  if (a.isRemote && !b.isRemote) return 1;
  if (b.isRemote && !a.isRemote) return -1;
  let va, vb;
  switch (col) {
    case "city":
      va = a.city.toLowerCase();
      vb = b.city.toLowerCase();
      return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    case "country":
      va = a.country.toLowerCase();
      vb = b.country.toLowerCase();
      return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    case "count":
      return asc ? a.count - b.count : b.count - a.count;
    case "liked":
      return asc ? a.liked - b.liked : b.liked - a.liked;
    case "score":
      // null sorts last in both directions
      if (a.meanScore === null && b.meanScore === null) return 0;
      if (a.meanScore === null) return 1;
      if (b.meanScore === null) return -1;
      return asc ? a.meanScore - b.meanScore : b.meanScore - a.meanScore;
    default:
      return 0;
  }
}

export function sortStatsTable(col) {
  if (state.statsSortCol === col) {
    state.statsSortAsc = !state.statsSortAsc;
  } else {
    state.statsSortCol = col;
    // Strings default ASC, numbers default DESC
    state.statsSortAsc = col === "city" || col === "country";
  }
  renderStats();
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

const COLUMNS = [
  { key: "city", label: T("geo_city", "City") },
  { key: "country", label: T("geo_country", "Country") },
  { key: "count", label: T("geo_count", "Count") },
  { key: "liked", label: T("geo_liked", "Liked") },
  { key: "score", label: T("geo_mean_score", "Mean score") },
];

function thHtml(col, label, sortCol, sortAsc) {
  const active = col === sortCol;
  const arrow = active ? (sortAsc ? " ↑" : " ↓") : "";
  const cls = `ct-th ct-col-${col}${active ? " ct-th-active" : ""}`;
  return `<th class="${cls}" onclick="sortStatsTable('${col}')">${escHtml(label)}${arrow}</th>`;
}

function rowHtml(r) {
  const flag = getFlagForChip({ text: r.country });
  const countryCell = flag
    ? `${flag} ${escHtml(r.country)}`
    : escHtml(r.country);
  const meanCell =
    r.meanScore == null
      ? '<span class="geo-score-dash">—</span>'
      : `<span class="${qualityClass(r.meanScore)}">${r.meanScore.toFixed(1)}</span>`;
  const likedCell =
    r.liked > 0
      ? `<span class="ct-liked-nonzero">${r.liked}</span>`
      : `<span class="ct-liked-zero">0</span>`;
  const rowCls = r.isRemote ? "ct-row ct-row--remote" : "ct-row";
  return (
    `<tr class="${rowCls}">` +
    `<td class="ct-td ct-col-name"><span class="ct-name-text">${escHtml(r.city)}</span></td>` +
    `<td class="ct-td ct-col-country">${countryCell}</td>` +
    `<td class="ct-td ct-col-vac">${r.count}</td>` +
    `<td class="ct-td ct-col-liked">${likedCell}</td>` +
    `<td class="ct-td ct-col-fit">${meanCell}</td>` +
    `</tr>`
  );
}

function buildHtml(rows, sortCol, sortAsc) {
  const help = `<div class="stats-help">${escHtml(HELP_TEXT)}</div>`;
  if (rows.length === 0) {
    return help + `<div class="company-empty">${EMPTY_LABEL}</div>`;
  }
  const thead =
    '<thead><tr class="ct-header-row">' +
    COLUMNS.map((c) => thHtml(c.key, c.label, sortCol, sortAsc)).join("") +
    "</tr></thead>";
  const tbody = "<tbody>" + rows.map(rowHtml).join("") + "</tbody>";
  return (
    help +
    // Scroll-guard (AE4): the table scrolls inside this wrapper at narrow
    // widths instead of crushing the city column — see style.css U10 block.
    `<div class="geo-table-wrap"><table class="company-table">${thead}${tbody}</table></div>`
  );
}

export function renderStats() {
  const container = document.getElementById("statsSection");
  if (!container) return;
  const rows = geoBuckets(groups, visOpts());
  // geoBuckets keeps raw city/country (empty = whole-country or remote); apply
  // the display labels + flags here so the derivation stays i18n-free.
  for (const r of rows) {
    if (r.isRemote) {
      r.city = REMOTE_LABEL;
      r.country = "—";
    } else {
      r.city = r.city || COUNTRY_ONLY_LABEL;
      r.country = r.country || "—";
    }
  }
  const sortCol = state.statsSortCol || "count";
  const sortAsc = state.statsSortAsc ?? false;
  rows.sort((a, b) => compareRows(a, b, sortCol, sortAsc));
  container.innerHTML = buildHtml(rows, sortCol, sortAsc);
}

export function initStats() {
  renderStats();
}

// Expose for inline onclick in dynamically rendered <th>s
window.sortStatsTable = sortStatsTable;
