// =============================================================================
// catalog.js — the daily review screen (Direction B, docs/2026-09-09-ux-build-spec.md).
//
// One list. Four controls in the command bar (search, score band, the deadline
// chip, the drawer), batch sections ordered by nearest deadline, and 56px rows
// carrying seven fields plus three decisions.
//
// The one rule that matters: EVERY decision — button or key — goes through
// screen.js's bulkSet, so it lands in the same undo history and gets the same
// receipt, optimistic-concurrency check and retry the bulk path has. The old
// fire-and-forget updateStatus path is gone from this surface.
// =============================================================================

import {
  state,
  config,
  groups,
  groupsById,
  stats,
  STATUS_BASKET,
  getGroupStatus,
} from "./state.js";
import {
  escHtml,
  jsAttr,
  isVacancyExpired,
  qualityBand,
} from "./helpers.js";
import { basketCounts, screenMatches, groupsInBasket } from "./derive.js";
import { reviewDrawerHtml, DRAWER_KEYS } from "./review-filters.js";
import {
  reviewSections,
  topConflict,
  requirementFacts,
  nearestDeadline,
  daysToDeadline,
} from "./review-batches.js";
import { bulkSet, undoLast, decisionState, retryDecision } from "./screen.js";
import { createCursor } from "./keys.js";

// How many list items (section headers count as one) paint per pass. The rest
// arrive as the sentinel scrolls into view, so an 866-row inbox never builds
// 866 nodes at once.
// ponytail: the window only grows — it paints REVIEW_WINDOW items at a time as
// the sentinel scrolls in, and never discards what is behind the reader. That
// keeps the FIRST paint bounded, which is the cost that hurt (866 rows and a
// 53,000px page on load). A reader who scrolls the whole 1288 still ends with
// them all in the DOM. Recycling the top needs a second sentinel and a spacer
// on both sides; add it if the deep-scroll case is ever measured as slow.
export const REVIEW_WINDOW = 60;

// The three decisions this screen can take, and the status each writes.
// The statuses server.js's SCREENING_STATUSES accepts as expected_status. A row
// already in the application funnel is moved on its own detail page, not here.
export const DECIDABLE = new Set([
  "unseen",
  "liked",
  "passed",
  "skipped",
  "unsure",
  "expiring",
]);

const DECISIONS = {
  like: {
    status: "liked",
    label: "Like",
    word: "Like",
    glyph: "✓",
    cls: "like",
  },
  unsure: {
    status: "unsure",
    label: "Unsure, back tomorrow",
    word: "Unsure",
    glyph: "?",
    cls: "unsure",
  },
  pass: {
    status: "passed",
    label: "Pass",
    word: "Pass",
    glyph: "✕",
    cls: "pass",
  },
};

// ---------------------------------------------------------------------------
// Screen state
// ---------------------------------------------------------------------------

// Drawer values, keyed exactly as derive.js's screenMatches reads them.
const filters = {};
// "" | "40" | "60" — the command bar's score band. "" lifts the floor.
let scoreBand = "";
let deadlineSoon = false;
let sortBy = "score-desc";
let orgFilter = "";
// The row whose requirement facts are open, or null. One at a time.
let expandedId = null;
let busy = false;
let notice = "";
// Decisions run one at a time, in the order they were made.
let _queue = Promise.resolve();

// The flat render list (headers and rows in order) and how much of it is
// painted. Rebuilt by renderCatalog, grown by the sentinel.
let _items = [];
let _shown = 0;
// The basket size behind the filters, or 0 when no filter is narrowing it.
let _filteredTotal = 0;
let _browseQueue = [];
const _browseCursor = createCursor();

// The shared visibility options the basket badge AND the basket list both read,
// so a count can never disagree with its list (DHA-374). The score band is the
// floor: "All scores" lifts it, 40 and 60 set it.
export function catalogVisibility() {
  return {
    isApproved: () => true,
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
    basketMap: STATUS_BASKET,
    minScore: scoreBand ? Number(scoreBand) : null,
  };
}

// ---------------------------------------------------------------------------
// Basket tabs
// ---------------------------------------------------------------------------

export function updateBasketCounts() {
  const counts = basketCounts(groups, catalogVisibility());
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  set("countLiked", counts.liked);
  set("countUnseen", counts.unseen);
  set("countPassed", counts.passed);
  set("countDeferred", counts.deferred || 0);
  set("navCountVacancies", counts.unseen);
}

export function switchBasket(btn) {
  document
    .querySelectorAll(".basket-tab")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.currentBasket = btn.dataset.basket;
  expandedId = null;
  renderCatalog();
}

// ---------------------------------------------------------------------------
// Command bar
// ---------------------------------------------------------------------------

export function reviewSetBand(value) {
  scoreBand = value === "40" || value === "60" ? value : "";
  // Geo reads the same floor through state.catalogShowAll; keep them in step so
  // the two browse surfaces never disagree about what is visible.
  state.catalogShowAll = !scoreBand;
  updateBasketCounts();
  renderCatalog();
}

export function reviewToggleDeadline() {
  deadlineSoon = !deadlineSoon;
  const btn = document.getElementById("reviewDeadlineChip");
  if (btn) {
    btn.classList.toggle("active", deadlineSoon);
    btn.setAttribute("aria-pressed", String(deadlineSoon));
  }
  renderCatalog();
}

// A rebuild over 1288 rows costs more than a keystroke's worth of time, so the
// search waits for a pause rather than repainting per character.
let _searchTimer = null;
export function reviewSearchInput() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => {
    renderCatalog();
    syncDrawerCount();
  }, 160);
}

export function reviewToggleExpand(id) {
  const closing = expandedId;
  expandedId = expandedId === id ? null : id;
  if (closing && closing !== id) refreshRow(closing);
  refreshRow(id);
}

export function reviewOpenFilters() {
  const drawer = document.getElementById("reviewDrawer");
  if (drawer && typeof drawer.showModal === "function") {
    syncDrawerCount();
    drawer.showModal();
  }
}

/** How many drawer filters are set — the number on the "More filters" button. */
function activeFilterCount() {
  return DRAWER_KEYS.filter((k) => filters[k]).length + (orgFilter ? 1 : 0);
}

/** The drawer's footer says how many roles its current settings would show. */
function syncDrawerCount() {
  const el = document.getElementById("reviewDrawerCount");
  if (!el) return;
  const { rows, total } = visibleRows();
  el.textContent =
    rows.length === total
      ? total + (total === 1 ? " role" : " roles")
      : rows.length + " of " + total + " roles match";
}

function syncFilterCount() {
  const badge = document.getElementById("reviewMoreCount");
  if (!badge) return;
  const n = activeFilterCount();
  badge.textContent = n ? String(n) : "";
  badge.hidden = !n;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initCatalog() {
  const body = document.getElementById("reviewDrawerBody");
  if (body && !body.dataset.built) {
    const distinct = (fn) =>
      [...new Set(groups.map(fn).filter(Boolean))].sort();
    body.innerHTML = reviewDrawerHtml({
      orgs: distinct((g) => g.org),
      sources: distinct((g) => g.source_board),
      dates: distinct((g) => String(g.first_seen || "").slice(0, 10)).reverse(),
    });
    body.dataset.built = "1";
    wireDrawer(body);
  }
  wireGrid();
  syncStickyHeight();
  if (typeof ResizeObserver === "function") {
    const block = document.querySelector(".review-sticky");
    if (block) new ResizeObserver(syncStickyHeight).observe(block);
  }
  syncFilterCount();
  updateBasketCounts();
  renderCatalog();
}

function wireDrawer(body) {
  body.addEventListener("change", (e) => {
    const el = e.target;
    if (el.id === "catalogOrgFilter") orgFilter = el.value;
    else if (el.id === "reviewSort") sortBy = el.value;
    else if (el.dataset.filter) filters[el.dataset.filter] = el.value;
    else return;
    syncFilterCount();
    renderCatalog();
    syncDrawerCount();
  });
  body.addEventListener("input", (e) => {
    if (e.target.dataset.filter !== "requirementText") return;
    filters.requirementText = e.target.value;
    syncFilterCount();
    reviewSearchInput();
  });
  const clear = document.getElementById("reviewFiltersClear");
  if (clear) clear.addEventListener("click", reviewClearAll);
}

// ---------------------------------------------------------------------------
// The visible set
// ---------------------------------------------------------------------------

function visibleRows() {
  const search = (document.getElementById("catalogSearch")?.value || "").trim();
  const fingerprint = config.screening_prompt_fingerprint;
  const inBasket = groupsInBasket(
    groups,
    state.currentBasket,
    catalogVisibility(),
  );
  const rows = inBasket.filter((g) => {
    if (orgFilter && g.org !== orgFilter) return false;
    if (deadlineSoon) {
      const days = daysToDeadline(g);
      if (days == null || days < 0 || days > 7) return false;
    }
    return screenMatches(g, {
      ...filters,
      search,
      promptFingerprint: fingerprint,
    });
  });
  return { rows, total: inBasket.length };
}

const openDays = (g) => {
  const days = daysToDeadline(g);
  return days == null || days < 0 ? Infinity : days;
};

function sortRows(rows) {
  const by = {
    "score-desc": (a, b) => (b.llm_score ?? -1) - (a.llm_score ?? -1),
    "score-asc": (a, b) => (a.llm_score ?? 999) - (b.llm_score ?? 999),
    // A lapsed deadline is not urgent. It sorts with "no deadline", matching
    // review-batches.js nearestDeadline, so the row order and the section
    // order never disagree about which role is closest.
    deadline: (a, b) => openDays(a) - openDays(b),
  };
  return [...rows].sort(by[sortBy] || by["score-desc"]);
}

/** Headers and rows in render order — the list the window paints from. */
export function reviewItems(rows, fingerprint, sort = (r) => r) {
  const items = [];
  for (const section of reviewSections(rows, fingerprint)) {
    items.push({ type: "head", section });
    for (const g of sort(section.rows)) items.push({ type: "row", g, section });
  }
  return items;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

export function renderCatalog() {
  const grid = document.getElementById("catalogGrid");
  if (!grid) return;
  const { rows, total } = visibleRows();
  const count = document.getElementById("catalogResultsCount");
  _filteredTotal = rows.length === total ? 0 : total;
  if (count)
    count.textContent = _filteredTotal
      ? rows.length + " of " + total + " roles match the filters"
      : total + (total === 1 ? " role" : " roles");

  if (!rows.length) {
    _items = [];
    _shown = 0;
    _browseQueue = [];
    _browseCursor.reconcile(_browseQueue);
    grid.innerHTML = emptyStateHtml(total);
    renderStatusBar();
    return;
  }

  _items = reviewItems(rows, config.screening_prompt_fingerprint, sortRows);
  _browseQueue = _items.filter((i) => i.type === "row").map((i) => i.g.id);
  _browseCursor.reconcile(_browseQueue);
  _shown = 0;
  grid.innerHTML = "";
  growWindow();
  ensureCursorPainted();
  renderStatusBar();
}

function itemHtml(item) {
  return item.type === "head"
    ? sectionHeadHtml(item.section, {
        canAccept: state.currentBasket === "unseen",
      })
    : reviewRowHtml(item.g, getGroupStatus(item.g), {
        expanded: item.g.id === expandedId,
        fingerprint: config.screening_prompt_fingerprint,
      });
}

/** Paint the next REVIEW_WINDOW items. The bottom sentinel calls this. */
export function growWindow() {
  const grid = document.getElementById("catalogGrid");
  if (!grid || _shown >= _items.length) return;
  const next = _items.slice(_shown, _shown + REVIEW_WINDOW);
  _shown += next.length;
  grid.insertAdjacentHTML("beforeend", next.map(itemHtml).join(""));
  applyCursorHighlight();
}

function emptyStateHtml(total) {
  const filtered =
    activeFilterCount() ||
    deadlineSoon ||
    scoreBand ||
    (document.getElementById("catalogSearch")?.value || "").trim();
  const unscored = (stats && stats.unscored_count) || 0;
  if (!filtered && !groups.length && unscored > 0)
    return (
      '<div class="catalog-empty"><div class="catalog-empty-icon">⏳</div><strong>' +
      unscored +
      (unscored === 1 ? " vacancy" : " vacancies") +
      " fetched, none scored yet.</strong>" +
      '<div class="catalog-empty-hint">Run scoring next, then they appear here.</div></div>'
    );
  const basketEmpty = {
    unseen: "Nothing left to review. Every role has a decision.",
    liked: "No liked roles yet. Like one from the Inbox and it lands here.",
    passed: "No passed roles yet.",
    deferred: "Nothing set aside. Press S on a role you cannot judge yet.",
  };
  return (
    '<div class="catalog-empty"><strong>' +
    (filtered
      ? "No role matches these filters."
      : basketEmpty[state.currentBasket] || "Nothing in this list.") +
    "</strong>" +
    (filtered
      ? '<div class="catalog-empty-hint">' +
        total +
        (total === 1 ? " role is" : " roles are") +
        " in this list behind the filters.</div>" +
        '<button type="button" class="review-undo" data-clear-all>Clear the search and every filter</button>'
      : "") +
    "</div>"
  );
}

/** Reset every control in the command bar and the drawer to its default. */
export function reviewClearAll() {
  for (const key of DRAWER_KEYS) delete filters[key];
  orgFilter = "";
  deadlineSoon = false;
  scoreBand = "";
  state.catalogShowAll = true;
  const search = document.getElementById("catalogSearch");
  if (search) search.value = "";
  const band = document.getElementById("reviewScoreBand");
  if (band) band.value = "";
  const chip = document.getElementById("reviewDeadlineChip");
  if (chip) {
    chip.classList.remove("active");
    chip.setAttribute("aria-pressed", "false");
  }
  document
    .querySelectorAll("#reviewDrawerBody select, #reviewDrawerBody input")
    .forEach((el) => {
      if (el.id !== "reviewSort") el.value = "";
    });
  syncFilterCount();
  updateBasketCounts();
  renderCatalog();
  syncDrawerCount();
}

// ---------------------------------------------------------------------------
// Section header
// ---------------------------------------------------------------------------

export function sectionHeadHtml(section, opts = {}) {
  const days = nearestDeadline(section.rows);
  const when = !Number.isFinite(days)
    ? "no deadline ahead"
    : days === 0
      ? "nearest deadline today"
      : "nearest deadline in " + days + (days === 1 ? " day" : " days");
  const defaultChip = section.defaultStatus && opts.canAccept !== false
    ? '<span class="review-head-default">Default: <span class="review-pill review-pill--' +
      section.defaultStatus +
      '">' +
      (section.defaultStatus === "passed" ? "Pass" : "Like") +
      "</span>" +
      (section.note ? " " + escHtml(section.note) : "") +
      "</span>"
    : "";
  const accept =
    section.defaultStatus && opts.canAccept !== false
      ? '<button type="button" class="review-accept" data-accept="' +
        escHtml(section.key) +
        '">' +
        (section.defaultStatus === "passed" ? "Pass all " : "Like all ") +
        section.rows.length +
        "</button>"
      : "";
  return (
    '<div class="review-section" data-section="' +
    escHtml(section.key) +
    '"><span class="review-section-title">' +
    escHtml(section.title) +
    '</span><span class="review-section-meta">' +
    section.rows.length +
    (section.rows.length === 1 ? " role · " : " roles · ") +
    when +
    "</span>" +
    defaultChip +
    '<span class="review-section-spacer"></span>' +
    accept +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Row assembly — pure: no DOM or module-state reads beyond the arguments.
// ---------------------------------------------------------------------------

/** How long the role has been in the inbox, in words. */
export function ageText(firstSeen) {
  const day = String(firstSeen || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return "—";
  const days = Math.round(
    (Date.parse(new Date().toISOString().slice(0, 10)) - Date.parse(day)) /
      86400000,
  );
  if (!Number.isFinite(days) || days < 0) return "—";
  if (days === 0) return "today";
  if (days === 1) return "1 day";
  if (days < 60) return days + " days";
  return Math.round(days / 30) + " months";
}

export function reviewRowHtml(g, basketStatus, opts = {}) {
  const id = escHtml(g.id);
  const idJs = jsAttr(g.id);
  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";

  const days = daysToDeadline(g);
  const deadlineHtml =
    days == null
      ? '<span class="review-deadline review-deadline--none">no deadline</span>'
      : '<span class="review-deadline' +
        (days < 0
          ? " review-deadline--past"
          : days <= 7
            ? " review-deadline--soon"
            : "") +
        '">' +
        (days < 0
          ? "passed"
          : days === 0
            ? "today"
            : "in " + days + (days === 1 ? " day" : " days")) +
        "</span>";

  const conflict = topConflict(g, opts.fingerprint);
  const conflictHtml = conflict
    ? '<span class="review-conflict" title="' +
      escHtml(conflict.quote || conflict.text) +
      '">' +
      escHtml(conflict.text) +
      "</span>"
    : '<span class="review-conflict review-conflict--none">No conflict found</span>';

  // The durable write path only accepts these as a precondition, so offering a
  // button on any other status renders a control that can only fail.
  const actions = (DECIDABLE.has(basketStatus) ? Object.entries(DECISIONS) : [])
    .filter(([, d]) => d.status !== basketStatus)
    .map(
      ([key, d]) =>
        '<button type="button" class="review-btn review-btn--' +
        d.cls +
        '" data-decide="' +
        key +
        '" data-id="' +
        id +
        '" title="' +
        escHtml(d.label) +
        '" aria-label="' +
        escHtml(d.label) +
        '"><span aria-hidden="true">' +
        d.glyph +
        '</span><span class="review-btn-word">' +
        escHtml(d.word) +
        "</span></button>",
    )
    .join("");

  const facts = opts.expanded ? requirementFacts(g) : [];
  const expansion = opts.expanded
    ? '<div class="review-facts">' +
      (facts.length
        ? facts
            .map(
              (f) =>
                '<p class="review-fact"><strong>' +
                escHtml(f.strength) +
                " · " +
                escHtml(f.value || f.kind) +
                "</strong>" +
                (f.note ? " — " + escHtml(f.note) : "") +
                "</p><blockquote>" +
                escHtml(f.quote) +
                "</blockquote>",
            )
            .join("")
        : "<p>No quoted requirements were prepared for this role.</p>") +
      '<button type="button" class="review-open" data-open="' +
      id +
      '">Open the full role page</button>' +
      "</div>"
    : "";

  return (
    '<div class="review-row" data-id="' +
    id +
    '" role="button" tabindex="0" onclick="if(!event.target.closest(\'button,input,a,label,summary,details\'))openCatalogRow(\'' +
    idJs +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();event.stopPropagation();reviewToggleExpand('" +
    idJs +
    "')}\">" +
    '<div class="review-cell review-cell--role"><span class="review-title">' +
    escHtml(g.title) +
    '</span><span class="review-org">' +
    escHtml(g.company_name || g.org || "—") +
    "</span></div>" +
    '<div class="review-cell review-cell--score"><span class="review-score ' +
    scoreCls +
    '" aria-label="Fit score ' +
    (score == null ? "not scored" : String(score)) +
    ' out of 100"><span class="review-score-word">score</span>' +
    (score == null ? "—" : String(score)) +
    "</span></div>" +
    '<div class="review-meta">' +
    '<div class="review-cell review-cell--deadline">' +
    deadlineHtml +
    "</div>" +
    '<div class="review-cell review-cell--conflict">' +
    conflictHtml +
    "</div>" +
    '<div class="review-cell review-cell--source">' +
    escHtml(g.source_board || "careers page") +
    "</div>" +
    '<div class="review-cell review-cell--age">' +
    escHtml(ageText(g.first_seen)) +
    "</div>" +
    "</div>" +
    '<div class="review-cell review-cell--actions">' +
    actions +
    "</div>" +
    expansion +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Decisions — one path for the buttons AND the keys.
// ---------------------------------------------------------------------------

function wireGrid() {
  const grid = document.getElementById("catalogGrid");
  if (!grid || grid.dataset.wired) return;
  grid.dataset.wired = "1";
  grid.addEventListener("click", (e) => {
    const decide = e.target.closest("[data-decide]");
    if (decide) {
      e.stopPropagation();
      applyDecision(decide.dataset.id, decide.dataset.decide);
      return;
    }
    if (e.target.closest("[data-clear-all]")) {
      e.stopPropagation();
      reviewClearAll();
      return;
    }
    const open = e.target.closest("[data-open]");
    if (open) {
      e.stopPropagation();
      openCatalogRow(open.dataset.open);
      return;
    }
    const accept = e.target.closest("[data-accept]");
    if (accept) {
      e.stopPropagation();
      acceptSection(accept.dataset.accept);
    }
  });
  const sentinel = document.getElementById("reviewSentinel");
  if (sentinel && typeof IntersectionObserver === "function")
    new IntersectionObserver((entries) => {
      if (entries.some((x) => x.isIntersecting)) growWindow();
    }).observe(sentinel);
  const drawer = document.getElementById("reviewDrawer");
  if (drawer) {
    drawer.addEventListener("close", renderCatalog);
    // A click on the backdrop lands on the dialog itself, never on its form.
    drawer.addEventListener("click", (e) => {
      if (e.target === drawer) drawer.close();
    });
  }
}

/**
 * One decision on one role. Goes through bulkSet, so it joins the same undo
 * history, receipt and revision check the bulk path uses.
 */
export function applyDecision(id, action) {
  const decision = DECISIONS[action];
  if (!decision || !groupsById.has(id)) return Promise.resolve();
  // Serialise instead of dropping. Typed faster than the network, ten decisions
  // used to collapse into one save while the cursor walked past all ten rows.
  _queue = _queue.then(() => writeDecision(id, decision));
  return _queue;
}

async function writeDecision(id, decision) {
  // An unsaved receipt belongs to another row; sending a second decision would
  // replay that receipt against this one. Make the reader retry it first.
  if (decisionState().pending) {
    notice = "One decision is still unsaved. Retry it first.";
    renderStatusBar();
    return;
  }
  busy = true;
  renderStatusBar();
  try {
    const result = await bulkSet([id], decision.status);
    notice =
      result && result.saved ? "" : "Could not save that decision. Retry.";
  } catch {
    notice = "Could not save that decision. Retry.";
  }
  busy = false;
  refreshRow(id);
  updateBasketCounts();
  renderStatusBar();
}

/** Apply a section's proposed default to every row still undecided in it. */
async function acceptSection(key) {
  if (busy) return;
  const section = _items.find(
    (i) => i.type === "head" && i.section.key === key,
  )?.section;
  if (!section || !section.defaultStatus) return;
  const verb = section.defaultStatus === "passed" ? "Pass" : "Like";
  if (
    typeof confirm === "function" &&
    !confirm(
      verb +
        " all " +
        section.rows.length +
        " roles in “" +
        section.title +
        "”? Undo reverses the whole batch.",
    )
  )
    return;
  busy = true;
  renderStatusBar();
  try {
    const result = await bulkSet(
      section.rows.map((g) => g.id),
      section.defaultStatus,
      undefined,
      true,
    );
    notice = result
      ? result.saved + " of " + result.total + " saved"
      : "Could not save. Retry.";
  } catch {
    notice = "Could not save. Retry.";
  }
  busy = false;
  updateBasketCounts();
  renderCatalog();
}

export async function undoDecision() {
  if (busy) return;
  busy = true;
  renderStatusBar();
  try {
    const result = await undoLast();
    notice = result ? result.restored + " restored" : "Nothing to undo.";
  } catch {
    notice = "Could not undo. Retry.";
  }
  busy = false;
  updateBasketCounts();
  renderCatalog();
}

async function retryPending() {
  if (busy) return;
  busy = true;
  renderStatusBar();
  try {
    await retryDecision();
    notice = "";
  } catch {
    notice = "Still could not save. Retry.";
  }
  busy = false;
  updateBasketCounts();
  renderCatalog();
}

/**
 * Re-render exactly one row, or drop it when the decision moved it out of the
 * current basket. Never rebuilds the list.
 */
export function refreshRow(id) {
  const grid = document.getElementById("catalogGrid");
  if (!grid) return;
  const el = Array.from(grid.querySelectorAll(".review-row")).find(
    (row) => row.dataset.id === id,
  );
  const g = groupsById.get(id);
  const status = g ? getGroupStatus(g) : null;
  const section = _items.find(
    (i) => i.type === "row" && i.g.id === id,
  )?.section;
  if (!g || STATUS_BASKET[status] !== state.currentBasket) {
    el?.remove();
    const at = _items.findIndex((i) => i.type === "row" && i.g.id === id);
    if (at >= 0) {
      _items.splice(at, 1);
      // The painted count must shrink with the list, or growWindow's next
      // slice starts one item too far and an undecided role is never painted.
      if (at < _shown) _shown -= 1;
    }
    if (section) {
      const left = section.rows.filter((r) => r.id !== id);
      section.rows.length = 0;
      section.rows.push(...left);
      refreshSectionHead(section);
    }
    _browseQueue = _browseQueue.filter((qid) => qid !== id);
    _browseCursor.reconcile(_browseQueue);
    if (!_browseQueue.length) {
      renderCatalog();
      return;
    }
    applyCursorHighlight();
    refreshCount();
    return;
  }
  if (!el) return;
  el.outerHTML = reviewRowHtml(g, status, {
    expanded: id === expandedId,
    fingerprint: config.screening_prompt_fingerprint,
  });
  applyCursorHighlight();
  refreshCount();
}

/** Repaint one section header in place, so its count never goes stale. */
function refreshSectionHead(section) {
  const grid = document.getElementById("catalogGrid");
  const el = grid?.querySelector(
    '.review-section[data-section="' + CSS.escape(section.key) + '"]',
  );
  if (!el) return;
  if (!section.rows.length) {
    el.remove();
    const at = _items.findIndex(
      (i) => i.type === "head" && i.section.key === section.key,
    );
    if (at >= 0) {
      _items.splice(at, 1);
      if (at < _shown) _shown -= 1;
    }
    return;
  }
  el.outerHTML = sectionHeadHtml(section, {
    canAccept: state.currentBasket === "unseen",
  });
}

/** Re-count without rebuilding the list — one decision changes one number. */
function refreshCount() {
  const count = document.getElementById("catalogResultsCount");
  if (!count) return;
  const shown = _items.filter((i) => i.type === "row").length;
  // Keep the sentence the render wrote. A filtered "74 of 1288 roles match the
  // filters" that silently becomes "73 roles" changes a number's meaning
  // mid-session, which is the most expensive kind of number on a screen.
  count.textContent = _filteredTotal
    ? shown + " of " + _filteredTotal + " roles match the filters"
    : shown + (shown === 1 ? " role" : " roles");
}

/** How many roles the reviewer set aside today — they sit in no basket. */
function deferredToday() {
  let n = 0;
  for (const g of groups) if (getGroupStatus(g) === "unsure") n += 1;
  return n;
}

/**
 * The recovery strip, directly under the command bar. Undo, Retry, the save
 * notice and the count of roles deferred today. It used to render below the
 * whole list, where a lost decision reported itself 4,000px out of sight.
 */
function renderStatusBar() {
  const bar = document.getElementById("reviewStatusbar");
  if (!bar) return;
  const pending = decisionState();
  const deferred = deferredToday();
  bar.innerHTML =
    (pending.canUndo
      ? '<button type="button" class="review-undo" id="reviewUndo"' +
        (busy ? " disabled" : "") +
        ">Undo last decision (U)</button>"
      : "") +
    (pending.pending
      ? '<button type="button" class="review-undo review-undo--alert" id="reviewRetry"' +
        (busy ? " disabled" : "") +
        ">Retry the unsaved decision</button>"
      : "") +
    (busy || notice
      ? '<span role="status" class="review-notice">' +
        escHtml(busy ? "Saving…" : notice) +
        "</span>"
      : "") +
    (deferred
      ? '<span class="review-deferred">' +
        deferred +
        (deferred === 1 ? " role" : " roles") +
        " set aside until tomorrow</span>"
      : "");
  const undo = document.getElementById("reviewUndo");
  if (undo) undo.onclick = undoDecision;
  const retry = document.getElementById("reviewRetry");
  if (retry) retry.onclick = retryPending;
}

// ---------------------------------------------------------------------------
// Keyboard — j/k move, L like, P pass, S unsure, U undo, Enter expand.
// ---------------------------------------------------------------------------

function cursorRowEl() {
  const grid = document.getElementById("catalogGrid");
  if (!grid || _browseCursor.id == null) return null;
  return (
    Array.from(grid.querySelectorAll(".review-row")).find(
      (el) => el.dataset.id === _browseCursor.id,
    ) || null
  );
}

function applyCursorHighlight() {
  const grid = document.getElementById("catalogGrid");
  if (!grid) return;
  const id = _browseCursor.id;
  grid
    .querySelectorAll(".review-row")
    .forEach((el) =>
      el.classList.toggle(
        "review-row--cursor",
        id != null && el.dataset.id === id,
      ),
    );
}

/**
 * Paint forward until the cursor's row exists. Without this, j past the last
 * painted row silently selects a role the reader cannot see — and the next
 * L/P/S decides it. It only ever grows: an earlier version recycled the top
 * into a spacer, and scrolling back up then showed an empty list.
 */
function ensureCursorPainted() {
  if (_browseCursor.id == null || cursorRowEl()) return;
  const at = _items.findIndex(
    (i) => i.type === "row" && i.g.id === _browseCursor.id,
  );
  if (at < 0) return;
  while (_shown <= at && _shown < _items.length) growWindow();
}

/** Publish the pinned block's height, so a row scrolls clear of it. */
function syncStickyHeight() {
  const block = document.querySelector(".review-sticky");
  if (!block) return;
  const h = Math.round(block.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--review-sticky-h", h + "px");
}

function scrollCursorIntoView() {
  syncStickyHeight();
  const row = cursorRowEl();
  if (!row || typeof row.getBoundingClientRect !== "function") return;
  const box = row.getBoundingClientRect();
  const top = document
    .querySelector(".review-sticky")
    ?.getBoundingClientRect().bottom;
  const bottom = document
    .querySelector(".review-keyhint")
    ?.getBoundingClientRect().top;
  const ceiling = (top > 0 ? top : 0) + 8;
  const floor = (bottom > 0 ? bottom : window.innerHeight) - 8;
  // "nearest" counts a row as visible while any of it is in the scrollport,
  // even the part painted over by the pinned block. Move by the overlap.
  if (box.top < ceiling) window.scrollBy(0, box.top - ceiling);
  else if (box.bottom > floor) window.scrollBy(0, box.bottom - floor);
}

/** Which key means what. Exported so the binding is testable without a DOM. */
export const REVIEW_KEYS = {
  l: "like",
  L: "like",
  p: "pass",
  P: "pass",
  x: "pass",
  s: "unsure",
  S: "unsure",
};

function browseKeydown(e) {
  if (e.isComposing) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const active = document.activeElement;
  if (active) {
    const tag = active.tagName;
    if (
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      active.isContentEditable
    )
      return;
  }
  const cat = document.getElementById("catalogSection");
  if (!cat || !cat.classList.contains("active")) return;
  const drawer = document.getElementById("reviewDrawer");
  if (drawer && drawer.open) return;

  const key = e.key;

  if (key === "j" || key === "k") {
    e.preventDefault();
    _browseCursor.move(key === "j" ? 1 : -1, _browseQueue);
    ensureCursorPainted();
    applyCursorHighlight();
    scrollCursorIntoView();
    return;
  }

  if (key === "u" || key === "U") {
    e.preventDefault();
    undoDecision();
    return;
  }

  if (key === "Escape") {
    if (_browseCursor.id == null) return;
    _browseCursor.clear();
    applyCursorHighlight();
    return;
  }

  if (_browseCursor.id == null) return;

  if (key === "Enter") {
    e.preventDefault();
    reviewToggleExpand(_browseCursor.id);
    scrollCursorIntoView();
    return;
  }

  const action = REVIEW_KEYS[key];
  if (!action) return;
  const g = groupsById.get(_browseCursor.id);
  if (!g) return;
  if (DECISIONS[action].status === getGroupStatus(g)) return;
  e.preventDefault();
  // A cursor can sit on a row the window has not painted (a filter change
  // reconciles it by remembered index). Show that row first; the next press
  // decides a role the reader has actually seen.
  if (!cursorRowEl()) {
    ensureCursorPainted();
    scrollCursorIntoView();
    return;
  }
  applyDecision(g.id, action);
  _browseCursor.move(1, _browseQueue);
  ensureCursorPainted();
  applyCursorHighlight();
  scrollCursorIntoView();
}

if (typeof document !== "undefined")
  document.addEventListener("keydown", browseKeydown);

// ---------------------------------------------------------------------------
// Row click → vacancy detail route
// ---------------------------------------------------------------------------

export function openCatalogRow(id, queue) {
  window.openVacancyRoute(id, {
    context: "browse",
    queue: queue || _browseQueue,
  });
}

// The old fire-and-forget thumb path. Kept as a thin alias so the vacancy
// detail page's existing calls still land on the durable write path.
export function catalogThumbAction(canonId, _memberIds, action) {
  return applyDecision(canonId, action === "like" ? "like" : "pass");
}
