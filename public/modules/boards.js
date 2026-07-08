// =============================================================================
// boards.js — Boards section: the board catalogue + each board's enabled state.
//
// Baked-first: the catalogue (id, name, neutral `audience`, source strategy,
// tier, ttl_days, enabled state, last_fetched) is baked into the payload
// (window.VACANCY_DATA.boards_catalog) by the generator, so this section renders
// its full read-only catalogue — including the freshness column, computed from
// the baked ttl_days/last_fetched — in simple mode too, no /api required. When
// the live /api/board-statuses endpoint IS reachable (full mode) it MERGES the
// vacancy counts on top. When an /api is reachable (full OR local mode) the
// enabled dot is an accessible toggle that writes board.enabled via
// /api/board-toggle; in simple mode (static export, API_BASE "") it stays a
// read-only dot with the CLI hint, mirroring how the other write actions
// (saveToServer / saveCompanyReview) no-op offline.
//
// The per-board YIELD funnel (scored → fit → liked) is DERIVED in the browser
// from the raw shipped roles (each carries source_board + llm_score) + live
// statuses, via derive.boardYield — never baked (STRATEGY guardrail 9, DHA-360).
// So it renders in simple mode too and reacts to a like/pass with no run. A
// board with no scored roles shows an explicit "no data yet", never 0/0/0 read
// as a verdict.
// =============================================================================

import { API_BASE, groups, getGroupStatus, STATUS_BASKET } from "./state.js";
import {
  escHtml,
  jsAttr,
  relativeTime,
  safeUrl,
  tierClass,
  isVacancyExpired,
} from "./helpers.js";
import { boardYield } from "./derive.js";
import { T } from "./i18n.js";

// The GitHub issue-form for proposing a new board (URL / who it serves / what
// feeds it). External link — opens the pre-filled form, never phones home
// (STRATEGY: not a hosted service). Kept here so the Boards section and README
// point at the same place.
const SUGGEST_BOARD_URL =
  "https://github.com/ncalavera/llm-job-pipeline/issues/new?template=suggest-a-board.yml";

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

// Optimistic enable/disable. Writes ONLY board.enabled — the same field the CLI
// (sources.py enable-board / disable-board) sets — via /api/board-toggle; the
// next pipeline run unions the enabled set in. Mirrors company-review's
// optimistic pattern: flip the baked catalogue entry in place + re-render, then
// revert + re-render if the write fails. No-op in simple mode (button isn't
// rendered there, but the guard keeps a stray call harmless).
function _postToggle(boardId, enabled) {
  return fetch(API_BASE + "/api/board-toggle", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ board_id: boardId, enabled: enabled }),
  })
    .then((r) => r.ok)
    .catch(() => false);
}

export function toggleBoard(boardId, nextEnabled) {
  if (!API_BASE) return Promise.resolve(false);
  const entry = _bakedCatalog().find((b) => b.id === boardId);
  if (!entry) return Promise.resolve(false);
  const prev = !!entry.enabled;
  const next = !!nextEnabled;
  entry.enabled = next; // optimistic
  renderBoards();
  return _postToggle(boardId, next).then((ok) => {
    if (!ok) {
      entry.enabled = prev; // revert
      renderBoards();
    }
    return ok;
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

// The enabled indicator. In simple mode (no /api) it stays the read-only dot it
// has always been; when an /api is reachable it becomes an accessible toggle
// button (aria-pressed reflects state, native button = keyboard operable) whose
// click writes the SAME board.enabled flag the CLI sets.
function _enabledControl(b) {
  const onCls = b.enabled ? "brd-dot-on" : "brd-dot-off";
  const title = escHtml(
    b.enabled ? T("boards_enabled_yes", "On") : T("boards_enabled_no", "Off"),
  );
  if (!API_BASE) {
    return '<span class="brd-dot ' + onCls + '" title="' + title + '"></span>';
  }
  const aria = escHtml(
    T("boards_toggle_aria", "Toggle enabled for {name}").replace(
      "{name}",
      b.name || b.id,
    ),
  );
  return (
    '<button type="button" class="brd-toggle" aria-pressed="' +
    (b.enabled ? "true" : "false") +
    '" aria-label="' +
    aria +
    '" title="' +
    title +
    '" onclick="event.stopPropagation();toggleBoard(\'' +
    jsAttr(b.id) +
    "'," +
    !b.enabled +
    ')">' +
    '<span class="brd-dot ' +
    onCls +
    '"></span>' +
    "</button>"
  );
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
  return (
    '<div class="brd-name-wrap">' +
    _enabledControl(b) +
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

// The derived yield cell for one board. `y` is the board's funnel row from
// boardYield ({ scored, fit, liked, hasData }) or undefined. Empty/absent
// history renders an explicit "no data yet" rather than 0 → 0 → 0, which would
// read as a verdict against a board that simply hasn't produced a scored role
// yet (the ticket's honest-empty requirement). Always present — derived from
// the baked roles, so it works in simple mode with no /api.
function _yieldCell(y) {
  if (!y || !y.hasData) {
    return (
      '<td class="brd-td brd-yield"><span class="brd-yield-empty">' +
      escHtml(T("boards_yield_nodata", "no data yet")) +
      "</span></td>"
    );
  }
  const scoredLbl = T("boards_yield_scored", "scored");
  const fitLbl = T("boards_yield_fit", "fit");
  const likedLbl = T("boards_yield_liked", "liked");
  const title =
    y.scored +
    " " +
    scoredLbl +
    " → " +
    y.fit +
    " " +
    fitLbl +
    " (≥60) → " +
    y.liked +
    " " +
    likedLbl;
  const stat = (n, lbl, cls) =>
    '<span class="brd-yield-stat' +
    (cls ? " " + cls : "") +
    '"><b>' +
    n +
    "</b> <i>" +
    escHtml(lbl) +
    "</i></span>";
  return (
    '<td class="brd-td brd-yield" title="' +
    escHtml(title) +
    '">' +
    '<span class="brd-yield-funnel">' +
    stat(y.scored, scoredLbl) +
    '<span class="brd-yield-arrow">→</span>' +
    stat(y.fit, fitLbl) +
    '<span class="brd-yield-arrow">→</span>' +
    stat(y.liked, likedLbl, "brd-yield-liked") +
    "</span></td>"
  );
}

export function renderBoards() {
  const grid = document.getElementById("boardsGrid");
  if (!grid) return;

  const catalog = _bakedCatalog();
  const hasLive = !!liveByKey;

  // Per-board yield (scored → fit → liked), derived in the browser from the raw
  // shipped roles + live statuses — never baked (guardrail 9). Keyed by board
  // display name (== vacancy.source_board == board.name).
  const yieldByBoard = boardYield(groups, {
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
    basketMap: STATUS_BASKET,
  });

  if (!catalog.length) {
    grid.innerHTML =
      '<p class="brd-empty">' +
      escHtml(T("boards_none", "No boards configured.")) +
      "</p>";
    return;
  }

  // Merge the live row (by id, then name) onto each baked catalogue entry —
  // its last_fetched (if present) is fresher than the baked snapshot. A board
  // curated out of view (board.hidden, set via sources.py hide-board) is
  // dropped here: the live flag wins, falling back to a baked hidden flag, and
  // absent on both means visible (the historical default).
  const rows = catalog
    .map((b) => {
      const live =
        (liveByKey && (liveByKey[b.id] || liveByKey[b.name])) || null;
      const last_fetched = (live && live.last_fetched) || b.last_fetched || "";
      const hidden = live && live.hidden != null ? live.hidden : b.hidden;
      return { ...b, _live: live, last_fetched, hidden };
    })
    .filter((b) => !b.hidden);

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
    '<th class="brd-th brd-th-yield">' +
    escHtml(T("boards_col_yield", "Yield (your history)")) +
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
        _yieldCell(yieldByBoard[b.name]) +
        liveCells +
        "</tr>"
      );
    })
    .join("");

  // Simple mode (no /api): the flag can't be written here, so keep the CLI
  // hint. When an /api IS reachable the dots ARE toggles, so drop the
  // "Read-only" hint and instead state, honestly, what a toggle does — it
  // changes only the enabled flag; the change lands on the next run, not now.
  const cliHint = API_BASE
    ? '<p class="boards-toggle-note">' +
      escHtml(
        T(
          "boards_toggle_note",
          "Toggling a board changes only its enabled flag — the next run picks it up.",
        ),
      ) +
      "</p>"
    : '<p class="boards-cli-hint"><code>' +
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
          "Total and fresh-14d vacancy counts come from the live API.",
        ),
      ) +
      "</p>";
  const yieldNote =
    '<p class="boards-yield-note">' +
    escHtml(
      T(
        "boards_yield_note",
        "Yield is your own history: scored roles this board reached, how many clear the apply bar (≥60), and how many you liked.",
      ),
    ) +
    "</p>";
  const suggestLink =
    '<p class="boards-suggest"><a href="' +
    escHtml(SUGGEST_BOARD_URL) +
    '" target="_blank" rel="noopener">' +
    escHtml(T("boards_suggest", "Know a board worth adding? Suggest one →")) +
    "</a></p>";

  grid.innerHTML =
    '<div class="boards-table-wrap"><table class="boards-table">' +
    head +
    "<tbody>" +
    body +
    "</tbody></table></div>" +
    yieldNote +
    cliHint +
    liveNote +
    suggestLink;
}
