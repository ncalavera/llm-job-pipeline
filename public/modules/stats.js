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
import { escHtml, getFlagForChip } from "./helpers.js";

const REMOTE_KEY = "__remote_unknown";
const HELP_TEXT =
  "A vacancy with N locations is counted N times — the table shows role availability by city.";
const REMOTE_LABEL = "Remote / Unknown";
const COUNTRY_ONLY_LABEL = "(whole country)";
const EMPTY_LABEL = "🏢 No data";

// ---------------------------------------------------------------------------
// Pure aggregator — easy to unit-test if a JS framework is added later
// ---------------------------------------------------------------------------

export function aggregateGeoStats(
  groupsArr,
  isApprovedFn,
  getStatusFn,
  basketMap,
) {
  const buckets = new Map();
  const visible = groupsArr.filter((g) => isApprovedFn(g));

  for (const g of visible) {
    const status = getStatusFn(g);
    const isLiked = basketMap[status] === "liked";
    const score =
      typeof g.llm_score === "number" && g.llm_score >= 0 ? g.llm_score : null;

    const locs = Array.isArray(g.locations) ? g.locations : [];
    const places = [];
    for (const loc of locs) {
      const city = (loc.city || "").trim();
      const country = (loc.country || "").trim();
      if (!city && !country) continue;
      places.push({
        city: city || COUNTRY_ONLY_LABEL,
        country: country || "—",
      });
    }
    if (places.length === 0) places.push({ key: REMOTE_KEY });

    for (const p of places) {
      const key = p.key || `${p.country}::${p.city}`;
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = {
          key,
          city: p.city || REMOTE_LABEL,
          country: p.country || "—",
          count: 0,
          liked: 0,
          scoreSum: 0,
          scoreN: 0,
          isRemote: !!p.key,
        };
        buckets.set(key, bucket);
      }
      bucket.count += 1;
      if (isLiked) bucket.liked += 1;
      if (score !== null) {
        bucket.scoreSum += score;
        bucket.scoreN += 1;
      }
    }
  }

  for (const b of buckets.values()) {
    b.meanScore = b.scoreN > 0 ? +(b.scoreSum / b.scoreN).toFixed(1) : null;
  }
  return Array.from(buckets.values());
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
  { key: "city", label: "City" },
  { key: "country", label: "Country" },
  { key: "count", label: "Count" },
  { key: "liked", label: "Liked" },
  { key: "score", label: "Mean score" },
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
  const meanCell = r.meanScore == null ? "—" : r.meanScore.toFixed(1);
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
  return help + `<table class="company-table">${thead}${tbody}</table>`;
}

export function renderStats() {
  const container = document.getElementById("statsSection");
  if (!container) return;
  const rows = aggregateGeoStats(
    groups,
    isGroupCompanyApproved,
    getGroupStatus,
    STATUS_BASKET,
  );
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
