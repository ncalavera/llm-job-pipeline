// =============================================================================
// catalog.js — Browse: dense vacancy rows, search, filters, baskets (U5,
// DHA-389). The accordion/expand card is retired — its content (full
// description, model reasoning, hard requirements, US-eligibility warning)
// now lives on the routed vacancy detail page (U6, vacancy.js); a row click
// opens it.
// =============================================================================

import {
  state,
  groups,
  stats,
  STATUS_BASKET,
  getGroupStatus,
  isGroupCompanyApproved,
  updateStatus,
} from "./state.js";
import {
  escHtml,
  jsAttr,
  formatDeadlineHtml,
  relativeTime,
  isVacancyExpired,
  qualityBand,
  tierClass,
} from "./helpers.js";
import { T, dateLocale } from "./i18n.js";
import { VISIBLE_MIN_SCORE, basketCounts, groupsInBasket } from "./derive.js";

// The shared visibility options the basket badge AND the basket list both read,
// so a count can never disagree with its list (DHA-374). The score floor is
// VISIBLE_MIN_SCORE unless "show all" (state.catalogShowAll, shared with Geo)
// lifts it.
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
// Basket tabs
// ---------------------------------------------------------------------------

export function updateBasketCounts() {
  // Same visibility filter + expiry re-bucketing the basket LIST uses, so the
  // badge is always the count of the rows the list renders (DHA-374).
  const counts = basketCounts(groups, visOpts());
  document.getElementById("countLiked").textContent = counts.liked;
  document.getElementById("countUnseen").textContent = counts.unseen;
  document.getElementById("countPassed").textContent = counts.passed;
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
      groups.filter((g) => isGroupCompanyApproved(g)).map((g) => g.org),
    ),
  ].sort();
  sel.innerHTML = '<option value="">All companies</option>';
  orgs.forEach((org) => {
    const opt = document.createElement("option");
    opt.value = org;
    opt.textContent = org;
    sel.appendChild(opt);
  });
  updateBasketCounts();
  renderCatalog();
}

// ---------------------------------------------------------------------------
// Render catalog table
// ---------------------------------------------------------------------------

// Persistent note above the catalog: roles from not-yet-approved (candidate)
// companies are excluded from the list until the company is approved on the
// Companies tab. Same message as the Companies → Pending banner.
function _renderCatalogHiddenNote(grid) {
  const existing = document.getElementById("catalogHiddenNote");
  if (existing) existing.remove();
  if (!grid || !grid.parentNode) return;

  const hidden = groups.filter((g) => !isGroupCompanyApproved(g));
  if (hidden.length === 0) return;

  const orgs = new Set();
  let vacs = 0;
  for (const g of hidden) {
    orgs.add(g.org);
    vacs += g.member_ids && g.member_ids.length ? g.member_ids.length : 1;
  }

  const tpl = T(
    "catalog_hidden_pending",
    "ℹ️ {vacs} vacancies from {orgs} not-yet-approved companies are hidden here — approve the company on the Companies tab to see its roles.",
  );
  const note = document.createElement("div");
  note.id = "catalogHiddenNote";
  note.className = "ces-pending-hidden";
  note.textContent = tpl.replace("{vacs}", vacs).replace("{orgs}", orgs.size);
  grid.parentNode.insertBefore(note, grid);
}

// The ordered id queue for the currently rendered rows — what a row click
// hands the U4 router as the "browse" context (F3's auto-advance walks this
// same order). Read by openCatalogRow's thin DOM shell below.
let _browseQueue = [];

export function renderCatalog() {
  const query = (
    document.getElementById("catalogSearch").value || ""
  ).toLowerCase();
  const orgFilter = document.getElementById("catalogOrgFilter").value;
  const grid = document.getElementById("catalogGrid");

  _renderCatalogHiddenNote(grid);

  // The visible rows in the current basket — the SAME set the badge counts, so
  // the "N of M" denominator always matches the badge (DHA-374). The score
  // floor + expiry re-bucketing live in the shared filter; only the org/
  // location/search refinements below are catalog-specific.
  const inBasket = groupsInBasket(groups, state.currentBasket, visOpts());
  const filtered = inBasket.filter((g) => {
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

  const countTpl = T("browse_results_count", "{shown} of {total} vacancies");
  document.getElementById("catalogResultsCount").textContent = countTpl
    .replace("{shown}", filtered.length)
    .replace("{total}", inBasket.length);

  if (filtered.length === 0) {
    _browseQueue = [];
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
      liked: T("basket_liked", "Liked"),
      unseen: T("basket_unreviewed", "Unreviewed"),
      passed: T("basket_passed", "Passed"),
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
  const rowOpts = { t: T, locale: dateLocale() };
  grid.innerHTML = filtered
    .map((g) => catalogRowHtml(g, getGroupStatus(g), rowOpts))
    .join("");
}

// ---------------------------------------------------------------------------
// Row assembly — pure (KTD2): no DOM/state reads beyond the arguments given,
// so the click contract (row → vacancy id, action-button gating, escaping) is
// directly unit-testable. `basket` is the group's current status bucket
// (unseen/liked/passed), matching getGroupStatus(g)'s three basket values.
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

  const subText = g.llm_summary || g.snippet || "";
  const subHtml = subText
    ? '<div class="catalog-row-sub">' + escHtml(subText) + "</div>"
    : "";

  const mids = JSON.stringify(g.member_ids).replace(/"/g, "&quot;");
  const likeLabel = escHtml(t("vac_like", "Like"));
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

  return (
    '<div class="catalog-row" data-id="' +
    escHtml(g.id) +
    '" onclick="openCatalogRow(\'' +
    idAttr +
    "')\">" +
    '<div class="catalog-row-score ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</div>" +
    '<div class="catalog-row-role">' +
    '<div class="catalog-row-title-line">' +
    '<span class="catalog-row-title">' +
    escHtml(g.title) +
    "</span>" +
    deadlineHtml +
    "</div>" +
    subHtml +
    "</div>" +
    '<div class="catalog-row-company">' +
    '<span class="catalog-row-org">' +
    escHtml(g.company_name || g.org) +
    "</span>" +
    tierHtml +
    "</div>" +
    '<div class="catalog-row-loc">' +
    locHtml +
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
