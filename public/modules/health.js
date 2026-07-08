// =============================================================================
// health.js — the Health tab: does the pipeline work, what waits on you, did
// the learning loop move.
//
// Read-only view of GET /api/health-detail (four blocks: boards, companies,
// waiting, learning) plus the architecture diagram. Live-only: unlike the
// Boards/Companies tabs it has no baked fallback (this data is never baked), so
// when no /api is reachable it shows an honest "needs the live API" message
// rather than a broken empty grid — mirroring how boards.js treats an absent
// live augmentation as normal, not an error.
//
// The architecture diagram is rendered with Mermaid lazy-loaded from a CDN the
// first time the tab opens (the dashboard has no CSP restriction; STRATEGY: not
// a hosted service, so an external script only loads on explicit navigation
// here, never at boot).
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, relativeTime } from "./helpers.js";
import { T } from "./i18n.js";
import {
  ARCHITECTURE_OVERVIEW,
  ARCHITECTURE_DETAILS,
} from "./architecture-diagram.js";

let healthData = null; // last successful /api/health-detail payload
let healthState = "idle"; // idle | loading | ok | error
let diagramRendered = false;

const MERMAID_CDN =
  "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";

export function initHealth() {
  _injectStylesOnce();
  renderHealth();
  loadHealth();
  renderDiagram(); // lazy — no-op after the first successful render
}

function loadHealth() {
  if (!API_BASE) {
    healthState = "error";
    renderHealth();
    return;
  }
  healthState = "loading";
  renderHealth();
  fetch(API_BASE + "/api/health-detail", { credentials: "same-origin" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((payload) => {
      healthData = payload;
      healthState = "ok";
      renderHealth();
    })
    .catch((e) => {
      console.warn("Health detail load failed:", e);
      healthState = "error";
      renderHealth();
    });
}

// ---------------------------------------------------------------------------
// Block renderers
// ---------------------------------------------------------------------------

// A board that has never been fetched has no display name in the DB yet — show
// a humanised id ("consultants_for_impact" → "Consultants For Impact"), never
// the raw snake_case (design FINDING-004).
function _boardName(b) {
  if (b.name) return b.name;
  return String(b.id || "")
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

// Fetch-status codes are pipeline vocabulary; the user reads plain words
// (design FINDING-005). The raw code stays in the tooltip.
const FETCH_STATUS_LABELS = {
  render_ok_zero: "fetches fine, finds 0 roles",
  js_required: "site needs a browser to render",
  no_data: "source returns nothing",
  error: "fetch error",
  ok: "ok",
};

function _statusLabel(code) {
  return FETCH_STATUS_LABELS[code] || code || "—";
}

function _brokenBadge() {
  return (
    '<span class="health-badge health-badge--broken">' +
    escHtml(T("health_presumed_broken", "PRESUMED BROKEN")) +
    "</span>"
  );
}

function _boardsBlock(boards) {
  if (!boards || !boards.length) {
    return _emptyBlock(T("health_no_boards", "No enabled boards."));
  }
  const rows = boards
    .map((b) => {
      const badge = b.presumed_broken ? " " + _brokenBadge() : "";
      // relativeTime WITHOUT the T dictionary: the tab's copy is English, and
      // the shared dictionary localises only time strings — mixing "2 дн назад"
      // into English labels read as a bug (design FINDING-003).
      const fetched = b.last_fetched
        ? escHtml(relativeTime(b.last_fetched))
        : escHtml(T("health_never", "never"));
      const cf =
        b.consecutive_failures == null ? "—" : String(b.consecutive_failures);
      return (
        '<tr class="health-tr' +
        (b.presumed_broken ? " health-tr--broken" : "") +
        '">' +
        '<td class="health-td"><b>' +
        escHtml(_boardName(b)) +
        "</b>" +
        badge +
        "</td>" +
        '<td class="health-td num">' +
        (b.vacancy_count != null ? b.vacancy_count : "—") +
        "</td>" +
        '<td class="health-td">' +
        fetched +
        "</td>" +
        '<td class="health-td num">' +
        cf +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
  return (
    '<table class="health-table"><thead><tr>' +
    _th(T("health_col_board", "Board")) +
    _th(T("health_col_vacancies", "Vacancies"), true) +
    _th(T("health_col_fetched", "Last fetched")) +
    _th(T("health_col_fails", "Fail streak"), true) +
    "</tr></thead><tbody>" +
    rows +
    "</tbody></table>"
  );
}

function _companiesBlock(companies) {
  if (!companies) return _emptyBlock(T("health_no_data", "No data."));
  const failing = companies.failing || [];
  const manual = companies.manual_check || [];
  const failHtml = failing.length
    ? '<ul class="health-list">' +
      failing
        .map(
          (c) =>
            "<li>" +
            escHtml(c.name) +
            ' <span class="health-dim" title="' +
            escHtml(c.fetch_status || "") +
            '">(' +
            escHtml(_statusLabel(c.fetch_status)) +
            (c.consecutive_failures
              ? ", " + c.consecutive_failures + " fails"
              : "") +
            ")</span></li>",
        )
        .join("") +
      "</ul>"
    : '<p class="health-ok-line">' +
      escHtml(T("health_no_failing", "No company's direct fetch is failing.")) +
      "</p>";
  // The covered-elsewhere list is reassurance, not action — collapsed to one
  // line with a count, full list on click (design FINDING-006).
  const manualHtml = manual.length
    ? '<details class="health-manual"><summary class="health-dim">' +
      escHtml(
        T(
          "health_manual_summary",
          "{n} companies have no direct fetch but are covered by boards or tracked by hand — not broken",
        ).replace("{n}", manual.length),
      ) +
      '</summary><p class="health-dim health-manual-list">' +
      escHtml(manual.map((m) => m.name).join(", ")) +
      "</p></details>"
    : "";
  return (
    '<h4 class="health-subhead">' +
    escHtml(T("health_failing_head", "Direct fetch failing")) +
    ' <span class="health-count">' +
    failing.length +
    "</span></h4>" +
    failHtml +
    manualHtml
  );
}

function _waitingBlock(w) {
  if (!w) return _emptyBlock(T("health_no_data", "No data."));
  const oldest =
    w.oldest_unseen_age_days != null
      ? " " +
        escHtml(
          T("health_oldest", "(oldest {n}d)").replace(
            "{n}",
            w.oldest_unseen_age_days,
          ),
        )
      : "";
  return (
    '<ul class="health-stat-list">' +
    "<li><b>" +
    (w.candidates_pending != null ? w.candidates_pending : "—") +
    "</b> " +
    escHtml(T("health_candidates", "candidate companies pending review")) +
    "</li>" +
    "<li><b>" +
    (w.unseen_scored != null ? w.unseen_scored : "—") +
    "</b> " +
    escHtml(T("health_unseen", "scored roles awaiting your verdict")) +
    oldest +
    "</li>" +
    "</ul>"
  );
}

function _learningBlock(l) {
  if (!l || l.unavailable) {
    return _emptyBlock(
      T("health_learning_unavailable", "Learning log not available."),
    );
  }
  const last = l.last_review
    ? escHtml(relativeTime(l.last_review))
    : escHtml(T("health_never_reviewed", "never reviewed"));
  return (
    '<ul class="health-stat-list">' +
    "<li>" +
    escHtml(T("health_last_review", "Last review:")) +
    " <b>" +
    last +
    "</b></li>" +
    "<li><b>" +
    (l.applied_since != null ? l.applied_since : "—") +
    "</b> " +
    escHtml(T("health_applied_since", "changes applied since then")) +
    "</li>" +
    "<li><b>" +
    (l.verdicts_pending != null ? l.verdicts_pending : "—") +
    "</b> " +
    escHtml(T("health_verdicts_pending", "verdicts waiting to be reviewed")) +
    "</li>" +
    "</ul>"
  );
}

function _th(label, num) {
  return (
    '<th class="health-th' +
    (num ? " num" : "") +
    '">' +
    escHtml(label) +
    "</th>"
  );
}

function _emptyBlock(msg) {
  return '<p class="health-dim">' + escHtml(msg) + "</p>";
}

function _card(title, inner) {
  return (
    '<section class="health-card"><h3 class="health-card-title">' +
    escHtml(title) +
    "</h3>" +
    inner +
    "</section>"
  );
}

// ---------------------------------------------------------------------------
// Top-level render
// ---------------------------------------------------------------------------

export function renderHealth() {
  const grid = document.getElementById("healthGrid");
  if (!grid) return;

  if (healthState === "error") {
    grid.innerHTML =
      '<p class="health-offline">' +
      escHtml(
        T(
          "health_offline",
          "This view needs the live API (it reads current pipeline state, never baked). Open the hosted dashboard, or run the local server.",
        ),
      ) +
      "</p>";
    return;
  }
  if (healthState !== "ok" || !healthData) {
    grid.innerHTML =
      '<p class="health-dim">' +
      escHtml(T("health_loading", "Loading…")) +
      "</p>";
    return;
  }

  const d = healthData;
  grid.innerHTML =
    _card(T("health_boards_title", "Boards"), _boardsBlock(d.boards)) +
    _card(
      T("health_companies_title", "Companies"),
      _companiesBlock(d.companies),
    ) +
    _card(
      T("health_waiting_title", "Waiting on you"),
      _waitingBlock(d.waiting),
    ) +
    _card(
      T("health_learning_title", "Learning loop"),
      _learningBlock(d.learning),
    );
}

// ---------------------------------------------------------------------------
// Architecture diagram — Mermaid, lazy-loaded from CDN on first tab open.
// ---------------------------------------------------------------------------

let mermaidPromise = null;

function _mermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import(MERMAID_CDN).then((mod) => {
      const mermaid = mod.default;
      mermaid.initialize({ startOnLoad: false, theme: "dark" });
      return mermaid;
    });
  }
  return mermaidPromise;
}

function _renderInto(el, svgId, src, retryFlagSetter) {
  return _mermaid()
    .then((mermaid) => mermaid.render(svgId, src))
    .then(({ svg }) => {
      el.innerHTML = svg;
    })
    .catch((e) => {
      console.warn("Architecture diagram render failed:", e);
      if (retryFlagSetter) retryFlagSetter();
      el.innerHTML =
        '<p class="health-dim">' +
        escHtml(
          T(
            "health_diagram_offline",
            "Architecture diagram needs the Mermaid CDN (offline?). See docs/ARCHITECTURE.md.",
          ),
        ) +
        "</p>";
    });
}

// Layered rendering (FINDING-007): the small overview renders on tab open;
// each detail diagram sits behind a <details> and renders on first expand.
function renderDiagram() {
  const host = document.getElementById("healthDiagram");
  if (!host || diagramRendered) return;
  diagramRendered = true; // one attempt; a failure re-arms itself

  host.innerHTML =
    '<div id="healthArchOverview"></div>' +
    ARCHITECTURE_DETAILS.map(
      (d) =>
        '<details class="health-arch-details" data-arch="' +
        d.id +
        '"><summary>' +
        escHtml(d.title) +
        '</summary><div class="health-arch-host" id="' +
        d.id +
        '"></div></details>',
    ).join("");

  const overview = document.getElementById("healthArchOverview");
  _renderInto(overview, "healthArchSvg", ARCHITECTURE_OVERVIEW, () => {
    diagramRendered = false;
  });

  host.querySelectorAll("details.health-arch-details").forEach((det) => {
    det.addEventListener("toggle", () => {
      if (!det.open || det.dataset.rendered) return;
      det.dataset.rendered = "1";
      const d = ARCHITECTURE_DETAILS.find((x) => x.id === det.dataset.arch);
      const el = det.querySelector(".health-arch-host");
      if (!d || !el) return;
      _renderInto(el, d.id + "Svg", d.src, () => {
        delete det.dataset.rendered; // re-render on next open
      });
    });
  });
}

// Minimal scoped styles so the tab is self-contained (no edits to the shared
// stylesheet). Injected once; the dashboard is dark-themed, so tones match.
function _injectStylesOnce() {
  if (document.getElementById("healthStyles")) return;
  const style = document.createElement("style");
  style.id = "healthStyles";
  style.textContent = `
    .health-grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-bottom:20px}
    .health-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:14px 16px}
    .health-card-title{margin:0 0 10px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;opacity:.7}
    .health-table{width:100%;border-collapse:collapse;font-size:13px}
    .health-th{text-align:left;padding:4px 6px;opacity:.6;font-weight:600;border-bottom:1px solid rgba(255,255,255,.08)}
    .health-th.num,.health-td.num{text-align:right}
    .health-td{padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.05)}
    .health-tr--broken{background:rgba(127,29,29,.18)}
    .health-badge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:5px;margin-left:6px;vertical-align:middle}
    .health-badge--broken{background:#7F1D1D;color:#fff}
    .health-subhead{margin:0 0 8px;font-size:13px}
    .health-count{background:rgba(255,255,255,.1);border-radius:5px;padding:0 7px;font-size:12px}
    .health-list,.health-stat-list{margin:0;padding-left:18px;font-size:13px;line-height:1.7}
    .health-stat-list{list-style:none;padding-left:0}
    .health-dim{opacity:.6;font-size:12px}
    .health-manual{margin-top:10px}
    .health-manual summary{cursor:pointer;list-style-position:inside}
    .health-manual summary:hover{opacity:.85}
    .health-manual-list{margin:6px 0 0;line-height:1.6}
    .health-ok-line{font-size:13px;opacity:.8;margin:0}
    .health-offline{opacity:.7;font-size:14px;padding:20px;text-align:center}
    .health-diagram-card{margin-top:4px;overflow-x:auto}
    #healthDiagram svg{max-width:100%;height:auto}
    .health-arch-details{margin-top:10px;border-top:1px solid rgba(255,255,255,.08);padding-top:8px}
    .health-arch-details summary{cursor:pointer;font-size:13px;opacity:.75}
    .health-arch-details summary:hover{opacity:1}
    .health-arch-host{margin-top:8px;overflow-x:auto}
  `;
  document.head.appendChild(style);
}
