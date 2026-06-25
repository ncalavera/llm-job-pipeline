// =============================================================================
// boards.js — Boards view: live status of monitored job boards.
// Catalog + status come from /api/board-statuses (the `board` table). Boards are
// not companies, so they live here instead of polluting the Companies list.
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, relativeTime } from "./helpers.js";
import { T } from "./i18n.js";

let boardsInited = false;
let boardsData = null;

export function initBoards() {
  const section = document.getElementById("boardsSection");
  if (!section) return;
  if (!boardsInited) {
    boardsInited = true;
    loadBoards();
  } else {
    renderBoards();
  }
}

function loadBoards() {
  const grid = document.getElementById("boardsGrid");
  if (grid)
    grid.innerHTML =
      '<p class="boards-loading">' +
      escHtml(T("boards_loading", "Loading board status…")) +
      "</p>";
  if (!API_BASE) {
    if (grid)
      grid.innerHTML =
        '<p class="boards-empty">' +
        escHtml(
          T(
            "boards_needs_api",
            "Board status is only available on the live deployment (needs /api).",
          ),
        ) +
        "</p>";
    return;
  }
  fetch(API_BASE + "/api/board-statuses", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      boardsData = (payload && payload.boards) || [];
      renderBoards();
    })
    .catch((e) => {
      console.warn("Board status load failed:", e);
      if (grid)
        grid.innerHTML =
          '<p class="boards-empty">' +
          escHtml(T("boards_load_failed", "Could not load board status.")) +
          "</p>";
    });
}

export function renderBoards() {
  const grid = document.getElementById("boardsGrid");
  if (!grid || !boardsData) return;

  if (!boardsData.length) {
    grid.innerHTML =
      '<p class="boards-empty">' +
      escHtml(T("boards_none", "No boards configured.")) +
      "</p>";
    return;
  }

  // Order: never-fetched first, then overdue, then by recent volume desc.
  const rows = [...boardsData].sort((a, b) => {
    const an = a.last_fetched ? 1 : 0;
    const bn = b.last_fetched ? 1 : 0;
    if (an !== bn) return an - bn;
    if (a.overdue !== b.overdue) return a.overdue ? -1 : 1;
    return (b.vac_recent || 0) - (a.vac_recent || 0);
  });

  const head =
    "<thead><tr>" +
    "<th>" +
    escHtml(T("boards_col_board", "Board")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_strategy", "Source type")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_tier", "Tier")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_last", "Last fetch")) +
    "</th>" +
    "<th>" +
    escHtml(T("boards_col_status", "Status")) +
    "</th>" +
    '<th class="num">' +
    escHtml(T("boards_col_total", "Vacancies")) +
    "</th>" +
    '<th class="num">' +
    escHtml(T("boards_col_recent", "Fresh 14d")) +
    "</th>" +
    "</tr></thead>";

  const body = rows
    .map((b) => {
      let statusLabel, statusClass;
      if (!b.last_fetched) {
        statusLabel = T("boards_status_never", "Never fetched");
        statusClass = "board-status-never";
      } else if (b.overdue) {
        statusLabel = T("boards_status_overdue", "Overdue (TTL {n}d)").replace(
          "{n}",
          b.ttl_days,
        );
        statusClass = "board-status-overdue";
      } else {
        statusLabel = T("boards_status_ok", "Fresh");
        statusClass = "board-status-ok";
      }
      const last = b.last_fetched ? relativeTime(b.last_fetched) : "—";
      const nameCell = b.url
        ? '<a href="' +
          escHtml(b.url) +
          '" target="_blank" rel="noopener">' +
          escHtml(b.name) +
          "</a>"
        : escHtml(b.name);
      return (
        "<tr>" +
        "<td>" +
        nameCell +
        "</td>" +
        "<td><code>" +
        escHtml(b.strategy || "—") +
        "</code></td>" +
        "<td>" +
        escHtml(b.tier || "—") +
        "</td>" +
        "<td>" +
        escHtml(last) +
        "</td>" +
        '<td><span class="board-status ' +
        statusClass +
        '">' +
        escHtml(statusLabel) +
        "</span></td>" +
        '<td class="num">' +
        (b.vac_total || 0) +
        "</td>" +
        '<td class="num">' +
        (b.vac_recent || 0) +
        "</td>" +
        "</tr>"
      );
    })
    .join("");

  grid.innerHTML =
    '<table class="boards-table">' +
    head +
    "<tbody>" +
    body +
    "</tbody></table>";
}
