// =============================================================================
// boards.js — Boards section: the board catalogue + each board's enabled state.
//
// Baked-first: the catalogue (id, name, neutral `audience`, source strategy,
// tier, TTL, enabled state, last_fetched) is baked into the payload
// (window.VACANCY_DATA.boards_catalog) by the generator, so this section renders
// its full read-only catalogue in simple mode too — no /api required. When the
// live /api/board-statuses endpoint IS reachable (full mode) it MERGES the
// vacancy counts + freshness on top. Changing a board's enabled state is a CLI
// action (shown as a hint) — there is no write endpoint here.
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, relativeTime, safeUrl } from "./helpers.js";
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
// baked catalogue already rendered, so a failure just leaves the count columns
// blank instead of showing an error.
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

function _statusCell(live) {
  if (!live) return "";
  let label, cls;
  if (!live.last_fetched) {
    label = T("boards_status_never", "Never fetched");
    cls = "board-status-never";
  } else if (live.overdue) {
    label = T("boards_status_overdue", "Overdue (TTL {n}d)").replace(
      "{n}",
      live.ttl_days,
    );
    cls = "board-status-overdue";
  } else {
    label = T("boards_status_ok", "Fresh");
    cls = "board-status-ok";
  }
  return '<span class="board-status ' + cls + '">' + escHtml(label) + "</span>";
}

export function renderBoards() {
  const grid = document.getElementById("boardsGrid");
  if (!grid) return;

  const catalog = _bakedCatalog();
  const hasLive = !!liveByKey;

  if (!catalog.length) {
    grid.innerHTML =
      '<p class="boards-empty">' +
      escHtml(T("boards_none", "No boards configured.")) +
      "</p>";
    return;
  }

  // Merge the live row (by id, then name) onto each baked catalogue entry.
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
    "<th>" +
    escHtml(T("boards_col_board", "Board")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_audience", "Audience")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_strategy", "Source type")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_tier", "Tier")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_enabled", "Enabled")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_last", "Last fetch")) +
    "</th>" +
    (hasLive
      ? "<th>" +
        escHtml(T("boards_col_status", "Status")) +
        "</th>" +
        '<th class="num">' +
        escHtml(T("boards_col_total", "Vacancies")) +
        "</th>" +
        '<th class="num">' +
        escHtml(T("boards_col_recent", "Fresh 14d")) +
        "</th>"
      : "") +
    "</tr></thead>";

  const body = rows
    .map((b) => {
      const boardUrl = safeUrl(b.url);
      const nameCell = boardUrl
        ? '<a href="' +
          escHtml(boardUrl) +
          '" target="_blank" rel="noopener">' +
          escHtml(b.name) +
          "</a>"
        : escHtml(b.name);
      const idCell =
        '<span class="board-id"><code>' + escHtml(b.id) + "</code></span>";
      const enabledCell = b.enabled
        ? '<span class="board-enabled board-enabled-on">' +
          escHtml(T("boards_enabled_yes", "On")) +
          "</span>"
        : '<span class="board-enabled board-enabled-off">' +
          escHtml(T("boards_enabled_no", "Off")) +
          "</span>";
      const last = b.last_fetched ? relativeTime(b.last_fetched) : "—";
      const liveCells = hasLive
        ? "<td>" +
          _statusCell(b._live) +
          "</td>" +
          '<td class="num">' +
          ((b._live && b._live.vac_total) || 0) +
          "</td>" +
          '<td class="num">' +
          ((b._live && b._live.vac_recent) || 0) +
          "</td>"
        : "";
      return (
        "<tr>" +
        "<td>" +
        nameCell +
        " " +
        idCell +
        "</td>" +
        '<td class="board-audience">' +
        escHtml(b.audience || "—") +
        "</td>" +
        "<td><code>" +
        escHtml(b.strategy || "—") +
        "</code></td>" +
        "<td>" +
        escHtml(b.tier || "—") +
        "</td>" +
        "<td>" +
        enabledCell +
        "</td>" +
        "<td>" +
        escHtml(last) +
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
    cliHint +
    liveNote +
    '<table class="boards-table">' +
    head +
    "<tbody>" +
    body +
    "</tbody></table>";
}
