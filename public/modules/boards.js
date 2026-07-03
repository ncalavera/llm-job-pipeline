// =============================================================================
// boards.js — Boards section: the board catalogue + each board's enabled state.
//
// Baked-first: the catalogue (id, name, neutral `audience`, source strategy,
// tier, ttl_days, enabled state, last_fetched) is baked into the payload
// (window.VACANCY_DATA.boards_catalog) by the generator, so this section renders
// its full read-only catalogue — including the freshness column, computed from
// the baked ttl_days/last_fetched — in simple mode too, no /api required. When
// the live /api/board-statuses endpoint IS reachable (full mode) it MERGES the
// vacancy counts on top. Changing a board's enabled state is a CLI action
// (shown as a hint) — there is no write endpoint here.
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, relativeTime, safeUrl, tierClass } from "./helpers.js";
import { T } from "./i18n.js";

let boardsInited = false;
let liveByKey = null; // { id/name: liveRow } once /api/board-statuses answers

function _bakedCatalog() {
  return (window.VACANCY_DATA && window.VACANCY_DATA.boards_catalog) || [];
}

export function initBoards() {
  renderBoards();
  if (!boardsInited) {
    boardsInited = true;
    loadLiveBoards();
  }
}

// Best-effort live augmentation. Absence is normal (simple/local mode) — the
// baked catalogue (including freshness) already rendered, so a failure just
// leaves the vacancy-count columns blank instead of showing an error.
function loadLiveBoards() {
  if (!API_BASE) return;
  fetch(API_BASE + "/api/board-statuses", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      const rows = (payload && payload.boards) || [];
      liveByKey = {};
      for (const b of rows) {
        if (b.id) liveByKey[b.id] = b;
        if (b.name) liveByKey[b.name] = b;
      }
      renderBoards();
    })
    .catch((e) => {
      console.warn("Board status load failed (using baked catalogue):", e);
    });
}

// Freshness — computed baked-first from ttl_days + last_fetched (mirrors the
// server's own overdue math in api/board-statuses.js) so the column works even
// without the live API; the live row's last_fetched (if fresher) wins once it
// answers.
function _freshness(b) {
  if (!b.last_fetched) {
    return {
      dotCls: "freshness-never",
      text: T("boards_status_never", "Never fetched"),
    };
  }
  const ageDays = (Date.now() - new Date(b.last_fetched).getTime()) / 86400000;
  const overdue = b.ttl_days != null ? ageDays >= b.ttl_days : true;
  return {
    dotCls: overdue ? "freshness-amber" : "freshness-green",
    text: relativeTime(b.last_fetched, T),
  };
}

function _freshnessCell(b) {
  const f = _freshness(b);
  return (
    '<span class="freshness-cell"><span class="freshness-dot ' +
    f.dotCls +
    '"></span>' +
    escHtml(f.text) +
    "</span>"
  );
}

function _ttlCell(b) {
  if (b.ttl_days === null || b.ttl_days === undefined)
    return '<span class="ct-dim-empty">—</span>';
  return "<code>" + escHtml(String(b.ttl_days)) + "d</code>";
}

function _nameCell(b) {
  const boardUrl = safeUrl(b.url);
  const nameText = boardUrl
    ? '<a href="' +
      escHtml(boardUrl) +
      '" target="_blank" rel="noopener">' +
      escHtml(b.name) +
      "</a>"
    : escHtml(b.name);
  const dotTitle = escHtml(
    b.enabled ? T("boards_enabled_yes", "On") : T("boards_enabled_no", "Off"),
  );
  return (
    '<div class="brd-name-wrap">' +
    '<span class="brd-dot ' +
    (b.enabled ? "brd-dot-on" : "brd-dot-off") +
    '" title="' +
    dotTitle +
    '"></span>' +
    '<div class="brd-name-col">' +
    '<span class="brd-name-text">' +
    nameText +
    "</span>" +
    '<span class="brd-name-id"><code>' +
    escHtml(b.id) +
    "</code></span>" +
    "</div>" +
    "</div>"
  );
}

export function renderBoards() {
  const grid = document.getElementById("boardsGrid");
  if (!grid) return;

  const catalog = _bakedCatalog();
  const hasLive = !!liveByKey;

  if (!catalog.length) {
    grid.innerHTML =
      '<p class="brd-empty">' +
      escHtml(T("boards_none", "No boards configured.")) +
      "</p>";
    return;
  }

  // Merge the live row (by id, then name) onto each baked catalogue entry —
  // its last_fetched (if present) is fresher than the baked snapshot.
  const rows = catalog.map((b) => {
    const live = (liveByKey && (liveByKey[b.id] || liveByKey[b.name])) || null;
    const last_fetched = (live && live.last_fetched) || b.last_fetched || "";
    return { ...b, _live: live, last_fetched };
  });

  // Enabled first, then by recent volume (live) / name.
  rows.sort((a, b) => {
    if (a.enabled !== b.enabled) return a.enabled ? -1 : 1;
    const av = (a._live && a._live.vac_recent) || 0;
    const bv = (b._live && b._live.vac_recent) || 0;
    if (av !== bv) return bv - av;
    return String(a.name).localeCompare(String(b.name));
  });

  const head =
    "<thead><tr>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_board", "Board")) +
    "</th>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_audience", "Audience")) +
    "</th>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_strategy", "Source type")) +
    "</th>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_tier", "Tier")) +
    "</th>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_ttl", "TTL")) +
    "</th>" +
    '<th class="brd-th">' +
    escHtml(T("boards_col_status", "Status")) +
    "</th>" +
    (hasLive
      ? '<th class="brd-th num">' +
        escHtml(T("boards_col_total", "Vacancies")) +
        "</th>" +
        '<th class="brd-th num">' +
        escHtml(T("boards_col_recent", "Fresh 14d")) +
        "</th>"
      : "") +
    "</tr></thead>";

  const body = rows
    .map((b) => {
      const tierHtml = b.tier
        ? '<span class="vac-tier ' +
          tierClass(b.tier) +
          '">' +
          escHtml(b.tier) +
          "</span>"
        : '<span class="ct-dim-empty">—</span>';
      const liveCells = hasLive
        ? '<td class="brd-td num">' +
          ((b._live && b._live.vac_total) || 0) +
          "</td>" +
          '<td class="brd-td num">' +
          ((b._live && b._live.vac_recent) || 0) +
          "</td>"
        : "";
      return (
        '<tr class="brd-row' +
        (b.enabled ? "" : " brd-row--off") +
        '">' +
        '<td class="brd-td">' +
        _nameCell(b) +
        "</td>" +
        '<td class="brd-td brd-audience" title="' +
        escHtml(b.audience || "") +
        '">' +
        escHtml(b.audience || "—") +
        "</td>" +
        '<td class="brd-td"><code>' +
        escHtml(b.strategy || "—") +
        "</code></td>" +
        '<td class="brd-td">' +
        tierHtml +
        "</td>" +
        '<td class="brd-td">' +
        _ttlCell(b) +
        "</td>" +
        '<td class="brd-td">' +
        _freshnessCell(b) +
        "</td>" +
        liveCells +
        "</tr>"
      );
    })
    .join("");

  const cliHint =
    '<p class="boards-cli-hint"><code>' +
    escHtml(
      T(
        "boards_cli_hint",
        "Read-only. Enable a board across runs: python3 scripts/sources.py enable-board <id>",
      ),
    ) +
    "</code></p>";
  const liveNote = hasLive
    ? ""
    : '<p class="boards-live-note">' +
      escHtml(
        T(
          "boards_live_note",
          "Vacancy counts and freshness come from the live API.",
        ),
      ) +
      "</p>";

  grid.innerHTML =
    '<div class="boards-table-wrap"><table class="boards-table">' +
    head +
    "<tbody>" +
    body +
    "</tbody></table></div>" +
    cliHint +
    liveNote;
}
