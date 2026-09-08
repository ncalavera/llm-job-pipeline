// =============================================================================
// catalog.js — Browse: dense vacancy rows, search, filters, baskets (U5,
// DHA-389). The accordion/expand card is retired — its content (full
// description, model reasoning, hard requirements, US-eligibility warning)
// now lives on the routed vacancy detail page (U6, vacancy.js); a row click
// opens it.
// =============================================================================

import {
  state,
  config,
  scheduleRender,
  groups,
  groupsById,
  stats,
  STATUS_BASKET,
  getGroupStatus,
  updateStatus,
} from "./state.js";
import {
  escHtml,
  safeUrl,
  jsAttr,
  formatDeadlineHtml,
  relativeTime,
  isVacancyExpired,
  qualityBand,
  tierClass,
} from "./helpers.js";
import { T, dateLocale } from "./i18n.js";
import {
  VISIBLE_MIN_SCORE,
  basketCounts,
  screenDateFacts,
  groupsInBasket,
} from "./derive.js";
import { REASON_GROUPS, reasonBatch } from "./reason-batches.js";
import { bulkSet, undoLast, decisionState, retryDecision } from "./screen.js";
import { createCursor, actionsFor } from "./keys.js";

// The shared visibility options the basket badge AND the basket list both read,
// so a count can never disagree with its list (DHA-374). The score floor is
// VISIBLE_MIN_SCORE unless "show all" (state.catalogShowAll, shared with Geo)
// lifts it.
export function catalogVisibility() {
  return {
    isApproved: () => true,
    getStatus: getGroupStatus,
    isExpired: isVacancyExpired,
    basketMap: STATUS_BASKET,
    minScore: state.catalogShowAll ? null : VISIBLE_MIN_SCORE,
  };
}

// ---------------------------------------------------------------------------
// Basket tabs
// ---------------------------------------------------------------------------

export function updateBasketCounts() {
  // Same visibility filter + expiry re-bucketing the basket LIST uses, so the
  // badge is always the count of the rows the list renders (DHA-374).
  const counts = basketCounts(groups, catalogVisibility());
  document.getElementById("countLiked").textContent = counts.liked;
  document.getElementById("countUnseen").textContent = counts.unseen;
  document.getElementById("countPassed").textContent = counts.passed;
  const navCount = document.getElementById("navCountVacancies");
  if (navCount) navCount.textContent = counts.unseen;
}

export function switchBasket(btn) {
  document
    .querySelectorAll(".basket-tab")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.currentBasket = btn.dataset.basket;
  renderCatalog();
}

export function toggleCatalogLoc(btn) {
  const loc = btn.dataset.cloc;
  if (state.activeCatalogLocs.has(loc)) {
    state.activeCatalogLocs.delete(loc);
    btn.classList.remove("active");
  } else {
    state.activeCatalogLocs.add(loc);
    btn.classList.add("active");
  }
  renderCatalog();
}

export function toggleCatalogSort(btn) {
  state.catalogSortDesc = !state.catalogSortDesc;
  btn.textContent = state.catalogSortDesc
    ? T("sort_score", "Score") + " ↓"
    : T("sort_score", "Score") + " ↑";
  renderCatalog();
}

// Lift / restore the default score floor (VISIBLE_MIN_SCORE). UI state only.
// Toggling the floor changes the visible set, so the badges refresh with it.
export function toggleCatalogShowAll(btn) {
  state.catalogShowAll = !state.catalogShowAll;
  btn.classList.toggle("active", state.catalogShowAll);
  btn.textContent = state.catalogShowAll
    ? T("catalog_show_top", "Top only")
    : T("catalog_show_all", "Show all");
  updateBasketCounts();
  renderCatalog();
}

// ---------------------------------------------------------------------------
// Catalog initialization
// ---------------------------------------------------------------------------

export function initCatalog() {
  const sel = document.getElementById("catalogOrgFilter");
  const orgs = [
    ...new Set(
      groups.map((g) => g.org).filter(Boolean),
    ),
  ].sort();
  sel.innerHTML = '<option value="">All companies</option>';
  orgs.forEach((org) => {
    const opt = document.createElement("option");
    opt.value = org;
    opt.textContent = org;
    sel.appendChild(opt);
  });
  // Sync the show-all toggle button to the current state (it defaults ON), so
  // its label + active class match before the user ever clicks it.
  const showAllBtn = document.querySelector(".browse-showall-btn");
  if (showAllBtn) {
    showAllBtn.classList.toggle("active", state.catalogShowAll);
    showAllBtn.textContent = state.catalogShowAll
      ? T("catalog_show_top", "Top only")
      : T("catalog_show_all", "Show all");
  }
  updateBasketCounts();
  renderCatalog();
}

// ---------------------------------------------------------------------------
// Render catalog table
// ---------------------------------------------------------------------------

// The ordered id queue for the currently rendered rows — what a row click
// hands the U4 router as the "browse" context (F3's auto-advance walks this
// same order). Read by openCatalogRow's thin DOM shell below, and the set the
// keyboard cursor (U15) steps through.
let _browseQueue = [];

// The keyboard-triage cursor (U15, DHA-399) — a single id-keyed cursor over the
// currently rendered rows. Pure logic in keys.js; this module is the thin DOM
// shell (highlight + scroll + the keydown listener at the bottom of the file).
const _browseCursor = createCursor();

export function renderCatalog() {
  const query = (
    document.getElementById("catalogSearch").value || ""
  ).toLowerCase();
  const orgFilter = document.getElementById("catalogOrgFilter").value;
  const grid = document.getElementById("catalogGrid");

  // The visible rows in the current basket — the SAME set the badge counts, so
  // the "N of M" denominator always matches the badge (DHA-374). The score
  // floor + expiry re-bucketing live in the shared filter; only the org/
  // location/search refinements below are catalog-specific.
  const inBasket = groupsInBasket(groups, state.currentBasket, catalogVisibility());
  let filtered = inBasket.filter((g) => {
    if (orgFilter && g.org !== orgFilter) return false;
    if (
      state.activeCatalogLocs.size > 0 &&
      !state.activeCatalogLocs.has(g.region)
    )
      return false;
    if (query) {
      const searchable = (
        g.title +
        " " +
        g.org +
        " " +
        g.locations.map((l) => l.location).join(" ")
      ).toLowerCase();
      if (!searchable.includes(query)) return false;
    }
    return true;
  });

  filtered = renderReasonReview(filtered);

  const countTpl = T("browse_results_count", "{shown} of {total} vacancies");
  document.getElementById("catalogResultsCount").textContent = countTpl
    .replace("{shown}", filtered.length)
    .replace("{total}", inBasket.length);

  if (filtered.length === 0) {
    _browseQueue = [];
    _browseCursor.reconcile(_browseQueue); // clears an active cursor; no rows to highlight
    const hasFilters = query || orgFilter || state.activeCatalogLocs.size > 0;
    // Fetched-but-unscored: the DB has vacancies, but none are scored yet, so the
    // dashboard (which only shows scored roles) looks empty. Tell the user to run
    // scoring next — distinct from the truly-empty "no vacancies at all" case.
    const unscored = (stats && stats.unscored_count) || 0;
    if (!hasFilters && groups.length === 0 && unscored > 0) {
      grid.innerHTML =
        '<div class="catalog-empty"><div class="catalog-empty-icon">⏳</div>' +
        "<strong>" +
        unscored +
        (unscored === 1 ? " vacancy" : " vacancies") +
        " fetched, none scored yet.</strong>" +
        '<div class="catalog-empty-hint">Run scoring next ' +
        "(<code>/jobs-score</code>) to rank them — scored roles appear here.</div>" +
        "</div>";
      return;
    }
    const basketLabels = {
      liked: T("basket_liked", "Liked & applications"),
      unseen: T("basket_unreviewed", "Undecided"),
      passed: T("basket_passed", "Passed & rejected"),
    };
    var basketEmpty =
      (basketLabels[state.currentBasket] || "") +
      " — " +
      T("catalog_basket_empty", "no vacancies");
    grid.innerHTML =
      '<div class="catalog-empty"><div class="catalog-empty-icon">🗂</div>' +
      (hasFilters
        ? T("catalog_no_match", "Nothing matches the filters")
        : groups.length === 0
          ? T("catalog_empty", "No vacancies yet. Fetch some first.")
          : basketEmpty) +
      "</div>";
    return;
  }

  if (state.catalogSortDesc) {
    filtered.sort((a, b) => (b.llm_score ?? -1) - (a.llm_score ?? -1));
  } else {
    filtered.sort((a, b) => (a.llm_score ?? 999) - (b.llm_score ?? 999));
  }

  _browseQueue = catalogQueueIds(filtered);
  // Data may have hot-swapped since the last render (a 60s poll can insert a
  // higher-scored row above the cursor, AE5); reconcile the id-keyed cursor to
  // the freshly-computed visible set BEFORE the rows rebuild, then re-apply its
  // highlight to the new DOM below.
  _browseCursor.reconcile(_browseQueue);
  const rowOpts = { t: T, locale: dateLocale() };
  grid.innerHTML = filtered
    .map((g) => catalogRowHtml(g, getGroupStatus(g), {...rowOpts, reason: state.currentBasket === "unseen" && reasonFilter ? reasonBatch(g, config.screening_prompt_fingerprint) : null}))
    .join("");
  applyCursorHighlight();
}

// ---------------------------------------------------------------------------
// Keyboard triage (U15, DHA-399) — thin DOM shell over keys.js's pure cursor.
// ---------------------------------------------------------------------------

// The currently-highlighted row element, or null when the cursor is dormant.
function cursorRowEl() {
  const grid = document.getElementById("catalogGrid");
  if (!grid || _browseCursor.id == null) return null;
  return (
    Array.from(grid.querySelectorAll(".catalog-row")).find(
      (el) => el.dataset.id === _browseCursor.id,
    ) || null
  );
}

// Paint the persistent cobalt selection class onto the cursor row and strip it
// from every other row. Compares dataset.id (not a CSS selector) so ids with
// quotes/markup can't break the query. Called after every renderCatalog (rows
// rebuild) and after each cursor move.
function applyCursorHighlight() {
  const grid = document.getElementById("catalogGrid");
  if (!grid) return;
  const id = _browseCursor.id;
  grid.querySelectorAll(".catalog-row").forEach((el) => {
    el.classList.toggle(
      "catalog-row--cursor",
      id != null && el.dataset.id === id,
    );
  });
}

// Instant (never smooth) so it honours prefers-reduced-motion by construction —
// scrollIntoView's default behavior is not animated.
function scrollCursorIntoView() {
  const row = cursorRowEl();
  if (row && typeof row.scrollIntoView === "function") {
    row.scrollIntoView({ block: "nearest" });
  }
}

// One document-level keydown listener (registered once at module load below).
// Returns early unless Browse is the active, in-focus surface with no detail
// overlay open — the catalogSection loses `.active` when a vacancy/company
// overlay shows or another section is active, so that one check covers all
// three. j/k move the cursor; l/x apply the SAME status path the row thumb
// buttons use (badge==list holds); Enter opens via the SAME router entry a row
// click uses; Escape clears the cursor.
function browseKeydown(e) {
  // Mid-IME-composition keystrokes belong to the composer, not triage.
  if (e.isComposing) return;
  // Never hijack browser/OS combos (⌘L address bar, ⌘K palette, etc.).
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  // Ignore while typing in a field or focused on a form control.
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

  // Only when Browse is the visible section (no overlay, not another mode).
  const cat = document.getElementById("catalogSection");
  if (!cat || !cat.classList.contains("active")) return;

  const key = e.key;

  if (key === "j" || key === "k") {
    e.preventDefault();
    _browseCursor.move(key === "j" ? 1 : -1, _browseQueue);
    applyCursorHighlight();
    scrollCursorIntoView();
    return;
  }

  if (key === "Escape") {
    if (_browseCursor.id == null) return;
    _browseCursor.clear();
    applyCursorHighlight();
    return;
  }

  // l / x / Enter act on the selection — a no-op until j/k picks a row.
  if (_browseCursor.id == null) return;

  if (key === "Enter") {
    e.preventDefault();
    openCatalogRow(_browseCursor.id);
    return;
  }

  if (key === "l" || key === "x") {
    const g = groupsById.get(_browseCursor.id);
    if (!g) return;
    const action = key === "l" ? "like" : "pass";
    // Gate on the cursor row's OWN status, mirroring catalogRowHtml's button
    // rendering exactly (keys.js:actionsFor): a status that shows no thumb
    // button (to_apply, applied, expiring, …) makes l/x a no-op here too — the
    // 3-value basket tab this row sits in isn't a faithful proxy for that.
    if (!actionsFor(getGroupStatus(g))[action]) return;
    e.preventDefault();
    catalogThumbAction(g.id, g.member_ids || [], action);
    // Advance the cursor optimistically so a rapid second l/x lands on the NEXT
    // row, not the one just actioned — catalogThumbAction defers the status
    // write ~200ms, so without this both presses hit the same vacancy. A single
    // press lands identically to the post-render reconcile, so this only
    // changes fast repeats.
    _browseCursor.move(1, _browseQueue);
    applyCursorHighlight();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("keydown", browseKeydown);
}

// ---------------------------------------------------------------------------
// Row assembly — pure (KTD2): no DOM/state reads beyond the arguments given,
// so the click contract (row → vacancy id, action-button gating, escaping) is
// directly unit-testable. `basket` is the group's RAW status (getGroupStatus(g),
// 9 values) — only unseen/liked/passed render thumb buttons; every other status
// shows none. keys.js:actionsFor mirrors this exact gating for the keyboard.
// ---------------------------------------------------------------------------

// First location's text plus a "+N" hint when there are more; the full list
// is one click away on the vacancy detail page's facts rail.
function primaryLocationInfo(g) {
  const locs = (g.locations || []).filter((l) => l && l.location);
  if (!locs.length) return null;
  const extra = locs.length - 1;
  return {
    text: locs[0].location + (extra > 0 ? " +" + extra : ""),
    title: locs.map((l) => l.location).join(", "),
  };
}

export function catalogQueueIds(rows) {
  return rows.map((g) => g.id);
}

export function catalogRowHtml(g, basket, opts) {
  const o = opts || {};
  const t = o.t || ((k, fb) => fb);
  const locale = o.locale || "en-US";

  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  const scoreTxt = score == null ? "—" : String(score);

  const idAttr = jsAttr(g.id);

  const deadlineHtml = g.deadline
    ? formatDeadlineHtml(g.deadline, "card-deadline", { t, locale })
    : "";

  const tierHtml = g.calculated_tier
    ? '<span class="catalog-row-tier ' +
      tierClass(g.calculated_tier) +
      '">' +
      escHtml(g.calculated_tier) +
      "</span>"
    : "";

  const loc = primaryLocationInfo(g);
  const locHtml = loc
    ? '<span title="' +
      escHtml(loc.title) +
      '">' +
      escHtml(loc.text) +
      "</span>"
    : "—";

  const compText = g.compensation ? escHtml(g.compensation) : "—";
  const seenText = g.first_seen ? escHtml(relativeTime(g.first_seen, t)) : "—";

  const subText = g.llm_summary || g.screening?.posting_facts?.duties || g.snippet || "";
  const subHtml = subText
    ? '<div class="catalog-row-sub">' + escHtml(subText) + "</div>"
    : "";

  const mids = jsAttr(JSON.stringify(g.member_ids));
  const likeLabel = escHtml(t("vac_like", "Keep"));
  const passLabel = escHtml(t("vac_pass", "Pass"));
  const likeBtn =
    '<button class="catalog-row-btn like" onclick="event.stopPropagation();catalogThumbAction(\'' +
    idAttr +
    "'," +
    mids +
    ",'like')\" title=\"" +
    likeLabel +
    '" aria-label="' +
    likeLabel +
    '">✓</button>';
  const passBtn =
    '<button class="catalog-row-btn pass" onclick="event.stopPropagation();catalogThumbAction(\'' +
    idAttr +
    "'," +
    mids +
    ",'pass')\" title=\"" +
    passLabel +
    '" aria-label="' +
    passLabel +
    '">✕</button>';
  let actionsHtml = "";
  if (basket === "liked") actionsHtml = passBtn;
  else if (basket === "unseen") actionsHtml = likeBtn + passBtn;
  else if (basket === "passed") actionsHtml = likeBtn;

  const dates = screenDateFacts(g);
  const url = safeUrl((g.locations || []).find(l => l?.url)?.url || "");
  const metadata = [
    dates.firstSeen && `<span class="scr-meta scr-meta--date">${escHtml(t("vac_first_seen","First seen"))}: ${dates.firstSeen}</span>`,
    dates.lastSeen && `<span class="scr-meta scr-meta--date">${escHtml(t("screen_last_seen","Last seen"))}: ${dates.lastSeen}</span>`,
    g.source_board && `<span class="scr-meta">${escHtml(g.source_board)}</span>`,
  ].filter(Boolean).join(" ");
  const progress = !["unseen","liked","passed","skipped","expiring"].includes(basket);
  const current = g.screening_state === "ready" && (!window.VACANCY_DATA.config.screening_prompt_fingerprint ||
    g.screening_fingerprint === `${g.posting_fingerprint}:${window.VACANCY_DATA.config.screening_prompt_fingerprint}`);
  const prepLabel = current ? "" : t(g.screening ? "inbox_older_facts" : "inbox_no_facts", g.screening ? "Facts need updating" : "Facts not prepared");
  if (o.review) {
    actionsHtml = progress ? '<span>' + escHtml(t("vac_status_" + basket,basket)) + '</span>' :
      [["liked","screen_keep","Like"],["passed","screen_put_aside","Pass"]].map(([status,key,label]) =>
        '<button class="catalog-row-btn" data-decision="' + status + '" data-vacancy="' + escHtml(g.id) + '"' +
        (o.disabled ? ' disabled' : '') + '>' + escHtml(t(key,label)) + '</button>').join('');
  }
  const selectHtml = o.review ? '<input type="checkbox" data-toggle="' + escHtml(g.id) + '" aria-label="' + escHtml(g.title) + '"' +
    (o.checked ? ' checked' : '') + (o.disabled || progress ? ' disabled' : '') + '>' : escHtml(scoreTxt);
  return (
    '<div class="catalog-row" data-id="' +
    escHtml(g.id) +
    '" role="button" tabindex="0" onclick="if(!event.target.closest(\'button,input,a,label,summary,details\'))openCatalogRow(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openCatalogRow('" +
    idAttr +
    "')}\">" +
    '<div class="catalog-row-score ' +
    scoreCls +
    '">' +
    selectHtml +
    "</div>" +
    '<div class="catalog-row-role">' +
    '<div class="catalog-row-title-line">' +
    '<span class="catalog-row-title">' +
    escHtml(g.title) +
    "</span>" +
    deadlineHtml +
    "</div>" +
    subHtml +
    (o.reason ? reasonDetails(g, o.reason) : "") +
    (o.review ? '<div class="scr-row-meta">' + metadata + '</div>' : "") +
    (o.review && prepLabel ? '<div class="scr-concern">' + escHtml(prepLabel) + '</div>' : '') +
    (o.review && url ? '<a class="scr-posting" href="' + escHtml(url) + '" target="_blank" rel="noopener noreferrer">' + escHtml(t("vac_open_posting","Open posting")) + ' ↗</a>' : '') +
    "</div>" +
    '<div class="catalog-row-company">' +
    '<span class="catalog-row-org">' +
    escHtml(g.company_name || g.org) +
    "</span>" +
    (o.review ? "" : tierHtml) +
    "</div>" +
    '<div class="catalog-row-loc">' +
    '<span class="scr-meta scr-meta--location">' + locHtml + "</span>" +
    "</div>" +
    '<div class="catalog-row-comp">' +
    compText +
    "</div>" +
    '<div class="catalog-row-seen">' +
    seenText +
    "</div>" +
    '<div class="catalog-row-actions">' +
    actionsHtml +
    "</div>" +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Row actions
// ---------------------------------------------------------------------------

// Thin DOM shell (KTD2): forwards a row click to the U4 router with the
// "browse" context + the CURRENT sorted/filtered id queue, so U6's "Move to
// apply" can auto-advance to the next unreviewed row (F3). `queue` is
// overridable so the wiring itself is unit-testable without touching module
// state.
export function openCatalogRow(id, queue) {
  window.openVacancyRoute(id, {
    context: "browse",
    queue: queue || _browseQueue,
  });
}

export function catalogThumbAction(canonId, memberIds, action) {
  const targetStatus =
    action === "like" ? "liked" : action === "pass" ? "passed" : "unseen";
  const row = document.querySelector('.catalog-row[data-id="' + canonId + '"]');
  if (row) {
    row.classList.add("dismissing");
    setTimeout(function () {
      updateStatus(canonId, memberIds, targetStatus);
    }, 200);
  } else {
    updateStatus(canonId, memberIds, targetStatus);
  }
}

let reasonFilter = '';
const reasonSelected = new Set();
let reasonBusy = false;
let reasonNotice = '';

function reasonDetails(g, batch) {
  return '<div class="reason-row"><label><input type="checkbox" data-reason-id="' + escHtml(g.id) + '"' +
    (reasonSelected.has(g.id) ? ' checked' : '') + (reasonBusy ? ' disabled' : '') + '> ' +
    escHtml(T('reason_select', 'Select for Pass')) + '</label><details><summary>' +
    escHtml(batch.reasons[0].note) + '</summary>' + batch.reasons.map(r => '<p>' + escHtml(r.note) +
    '</p><blockquote>' + escHtml(r.quote) + '</blockquote>').join('') + '</details></div>';
}

function renderReasonReview(rows) {
  let host = document.getElementById('catalogReasonReview');
  if (!host) {
    host = document.createElement('div'); host.id = 'catalogReasonReview';
    document.querySelector('.browse-header').after(host);
    host.addEventListener('click', async e => {
      const button = e.target.closest('button');
      if (!button || reasonBusy) return;
      if (button.dataset.reason !== undefined) {
        reasonFilter = button.dataset.reason; reasonSelected.clear(); renderCatalog(); return;
      }
      if (button.dataset.reasonAction === 'select') {
        document.querySelectorAll('[data-reason-id]').forEach(el => reasonSelected.add(el.dataset.reasonId));
        renderCatalog(); return;
      }
      reasonBusy = true; renderCatalog();
      try {
        const result = button.dataset.reasonAction === 'undo' ? await undoLast() :
          button.dataset.reasonAction === 'retry' ? await retryDecision() :
          await bulkSet([...reasonSelected], 'passed', undefined, true);
        reasonNotice = result ? `${result.saved ?? result.restored ?? 0} / ${result.total} ` + T('screen_saved','saved') : T('screen_notes_failed','Could not save. Retry.');
        reasonSelected.clear();
      } catch { reasonNotice = T('screen_notes_failed','Could not save. Retry.'); }
      finally { reasonBusy = false; scheduleRender(); }
    });
    document.getElementById('catalogGrid').addEventListener('change', e => {
      const id = e.target.dataset.reasonId;
      if (!id || reasonBusy) return;
      if (e.target.checked) reasonSelected.add(id); else reasonSelected.delete(id);
      renderCatalog();
    });
  }
  const pending = decisionState();
  const candidates = state.currentBasket === 'unseen' ? rows : [];
  const classified = new Map(candidates.map(g => [g.id, reasonBatch(g,config.screening_prompt_fingerprint)]));
  const visible = reasonFilter && state.currentBasket === 'unseen' ? rows.filter(g => classified.get(g.id)?.key === reasonFilter) : rows;
  for (const id of reasonSelected) if (!visible.some(g => g.id === id)) reasonSelected.delete(id);
  const disabled = reasonBusy || !state.statusesLoaded || pending.pending;
  const button = (action,label,off) => '<button class="browse-sort-btn" data-reason-action="' + action + '"' + (off ? ' disabled' : '') + '>' + escHtml(label) + '</button>';
  host.innerHTML = (state.currentBasket === 'unseen' ? '<details' + (reasonFilter ? ' open' : '') + '><summary>' +
    escHtml(T('reason_review','Review low scores by reason')) + ' · &lt;' + VISIBLE_MIN_SCORE + '</summary><div class="reason-toolbar">' +
    [['',T('contacts_all_groups','All')],...REASON_GROUPS.map(([k,label])=>[k,T('reason_'+k,label)])].map(([key,label]) =>
      '<button class="browse-sort-btn" data-reason="' + key + '" aria-pressed="' + (reasonFilter === key) + '"' +
      (reasonBusy ? ' disabled' : '') + '>' + escHtml(label) + (key ? ' · ' + [...classified.values()].filter(b=>b?.key===key).length : '') + '</button>').join('') +
    '</div><p>' + escHtml(T('reason_hint','Possible conflicts with required posting conditions. Review the explanation, select the roles you agree to pass, and leave exceptions unchecked. Other roles remain in All.')) + '</p></details>' : '') +
    (reasonFilter && state.currentBasket === 'unseen' ? '<div class="reason-toolbar">' + button('select',T('screen_select_all','Select all'),disabled || !visible.length) +
      button('pass',T('reason_pass','Pass selected') + ' · ' + reasonSelected.size,disabled || !reasonSelected.size) + '</div>' : '') +
    (pending.pending ? button('retry',T('screen_retry','Retry'),reasonBusy) : '') +
    (pending.canUndo ? button('undo',T('screen_undo','Undo'),disabled) : '') +
    '<p role="status">' + escHtml(reasonNotice) + '</p>';
  return visible;
}
