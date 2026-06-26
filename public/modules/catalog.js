// =============================================================================
// catalog.js — Catalog view: vacancy cards, search, filters, baskets, swipes
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
  parseLocationChips,
  renderLocationChips,
  llmScoreBadge,
  formatDeadlineHtml,
  relativeTime,
  isVacancyExpired,
  hardReqHtml,
} from "./helpers.js";
import { T } from "./i18n.js";

// ---------------------------------------------------------------------------
// Basket tabs
// ---------------------------------------------------------------------------

export function updateBasketCounts() {
  const counts = { liked: 0, unseen: 0, passed: 0 };
  groups.forEach((g) => {
    if (!isGroupCompanyApproved(g)) return;
    const s = getGroupStatus(g);
    const basket = STATUS_BASKET[s] || "unseen";
    // Expired liked vacancies count towards passed, not liked
    if (basket === "liked" && isVacancyExpired(g)) {
      counts.passed = (counts.passed || 0) + 1;
    } else {
      counts[basket] = (counts[basket] || 0) + 1;
    }
  });
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
    ? T("sort_score", "Score") + "\u00A0\u2193"
    : T("sort_score", "Score") + "\u00A0\u2191";
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
// Render catalog grid
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

export function renderCatalog() {
  const query = (
    document.getElementById("catalogSearch").value || ""
  ).toLowerCase();
  const orgFilter = document.getElementById("catalogOrgFilter").value;
  const grid = document.getElementById("catalogGrid");

  _renderCatalogHiddenNote(grid);

  const visible = groups.filter((g) => isGroupCompanyApproved(g));
  const inBasket = visible.filter((g) => {
    const basket = STATUS_BASKET[getGroupStatus(g)] || "unseen";
    // Expired liked vacancies are excluded from the liked basket
    // and shown in the passed basket instead
    if (basket === "liked" && isVacancyExpired(g)) {
      return state.currentBasket === "passed";
    }
    return basket === state.currentBasket;
  });
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

  document.getElementById("catalogResultsCount").textContent =
    filtered.length + " of " + inBasket.length + " vacancies";

  if (filtered.length === 0) {
    const hasFilters = query || orgFilter || state.activeCatalogLocs.size > 0;
    // Fetched-but-unscored: the DB has vacancies, but none are scored yet, so the
    // dashboard (which only shows scored roles) looks empty. Tell the user to run
    // scoring next \u2014 distinct from the truly-empty "no vacancies at all" case.
    const unscored = (stats && stats.unscored_count) || 0;
    if (!hasFilters && groups.length === 0 && unscored > 0) {
      grid.innerHTML =
        '<div class="catalog-empty"><div class="catalog-empty-icon">\u23F3</div>' +
        "<strong>" +
        unscored +
        (unscored === 1 ? " vacancy" : " vacancies") +
        " fetched, none scored yet.</strong>" +
        '<div class="catalog-empty-hint">Run scoring next ' +
        "(<code>/jobs-score</code>) to rank them \u2014 scored roles appear here.</div>" +
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
      " \u2014 " +
      T("catalog_basket_empty", "no vacancies");
    grid.innerHTML =
      '<div class="catalog-empty"><div class="catalog-empty-icon">\uD83D\uDDC2</div>' +
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

  grid.innerHTML = filtered.map((g) => buildCatalogCard(g)).join("");

  // Staggered fade-in via IntersectionObserver
  const cards = grid.querySelectorAll(".catalog-card");
  const observer = new IntersectionObserver(
    function (entries) {
      let batchIdx = 0;
      entries.forEach(function (entry) {
        if (
          entry.isIntersecting &&
          !entry.target.classList.contains("card-visible")
        ) {
          entry.target.style.setProperty(
            "--stagger-delay",
            batchIdx * 30 + "ms",
          );
          entry.target.classList.add("card-visible");
          batchIdx++;
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.1 },
  );
  cards.forEach(function (card) {
    observer.observe(card);
  });
}

// ---------------------------------------------------------------------------
// Build single catalog card HTML
// ---------------------------------------------------------------------------

function buildCatalogCard(g) {
  const [fg, bg] = g.org_color || ["#F97316", "#FFF7ED"];
  const firstUrl = g.locations.find((l) => l.url)?.url || g.org_url || "";
  const titleHtml = firstUrl
    ? '<a href="' +
      escHtml(firstUrl) +
      '" target="_blank" rel="noopener">' +
      escHtml(g.title) +
      "</a>"
    : escHtml(g.title);

  const catalogChips = g.locations.flatMap((l) => {
    const chips = parseLocationChips(l.location);
    chips.forEach((c) => {
      if (l.url) c.url = l.url;
    });
    return chips;
  });
  const seenCatalog = new Set();
  const uniqueCatalogChips = catalogChips.filter((c) => {
    const key = c.text.toLowerCase();
    if (seenCatalog.has(key)) return false;
    seenCatalog.add(key);
    return true;
  });
  const locChips = renderLocationChips(uniqueCatalogChips, {
    chipClass: "loc-chip",
    useRegionColor: false,
    maxVisible: 3,
  });

  let compHtml = "";
  if (g.compensation || g.deadline || g.first_seen) {
    const parts = [];
    if (g.compensation)
      parts.push(
        '<span class="card-comp">' + escHtml(g.compensation) + "</span>",
      );
    if (g.deadline) {
      const dlHtml = formatDeadlineHtml(g.deadline, "card-deadline");
      if (dlHtml) parts.push(dlHtml);
    }
    if (g.first_seen) {
      parts.push(
        '<span class="card-first-seen">' +
          relativeTime(g.first_seen) +
          "</span>",
      );
    }
    compHtml = '<div class="card-comp-row">' + parts.join("") + "</div>";
  }

  const regionLabels = {
    europe: T("region_europe", "Europe"),
    americas: T("region_americas", "Americas"),
    remote: T("region_remote", "Remote"),
    asia: T("region_asia", "Asia"),
    africa: T("region_africa", "Africa"),
  };
  const regionCls = {
    europe: "region-europe",
    americas: "region-us",
    remote: "region-remote",
    asia: "region-other",
    africa: "region-other",
  };
  const regionBadge = regionLabels[g.region]
    ? '<span class="badge ' +
      regionCls[g.region] +
      '">' +
      regionLabels[g.region] +
      "</span>"
    : "";

  const llmBadge = llmScoreBadge(g.llm_score);

  const catTierVal = g.calculated_tier || null;
  const catTierBadge = catTierVal
    ? '<span class="company-tier-badge ctier-' +
      catTierVal.toLowerCase() +
      '">' +
      catTierVal +
      "</span>"
    : "";

  const summaryText = g.llm_summary || g.snippet || "";
  const summaryHtml = summaryText
    ? '<p class="card-snippet">' + escHtml(summaryText) + "</p>"
    : "";

  const basket = getGroupStatus(g);
  const mids = JSON.stringify(g.member_ids).replace(/"/g, "&quot;");
  let thumbBtns = "";
  if (basket === "liked") {
    thumbBtns =
      '<button class="thumb-btn pass" onclick="event.stopPropagation();catalogThumbAction(\'' +
      g.id +
      "'," +
      mids +
      ',\'pass\')" title="Pass">\uD83D\uDC4E</button>';
  } else if (basket === "unseen") {
    thumbBtns =
      '<button class="thumb-btn like" onclick="event.stopPropagation();catalogThumbAction(\'' +
      g.id +
      "'," +
      mids +
      ',\'like\')" title="Like">\uD83D\uDC4D</button>' +
      '<button class="thumb-btn pass" onclick="event.stopPropagation();catalogThumbAction(\'' +
      g.id +
      "'," +
      mids +
      ',\'pass\')" title="Skip">\uD83D\uDC4E</button>';
  } else if (basket === "passed") {
    thumbBtns =
      '<button class="thumb-btn like" onclick="event.stopPropagation();catalogThumbAction(\'' +
      g.id +
      "'," +
      mids +
      ',\'like\')" title="Like">\uD83D\uDC4D</button>';
  }

  const hasExpandContent =
    (g.full_description &&
      g.full_description.length > summaryText.length + 50) ||
    g.llm_reasoning;
  const expandBtn = hasExpandContent
    ? '<button class="expand-btn" onclick="event.stopPropagation();toggleCatalogExpand(this)">' +
      escHtml(T("catalog_details", "Details")) +
      "</button>"
    : "";
  let expandParts = [];
  if (g.llm_reasoning) {
    expandParts.push(
      '<div class="expand-reasoning"><span class="expand-reasoning-label">LLM reasoning:</span> ' +
        escHtml(g.llm_reasoning) +
        "</div>",
    );
  }
  if (
    g.full_description &&
    g.full_description.length > summaryText.length + 50
  ) {
    expandParts.push(escHtml(g.full_description));
  }
  const fullDescHtml = hasExpandContent
    ? '<div class="catalog-full-desc">' + expandParts.join("") + "</div>"
    : "";

  const isFeatured = g.llm_score != null && g.llm_score >= 65;

  return (
    '<div class="catalog-card' +
    (isFeatured ? " card-featured" : "") +
    '" data-id="' +
    g.id +
    '">' +
    '<div class="catalog-card-header">' +
    '<div class="catalog-header-left">' +
    (g.company_slug
      ? '<span class="catalog-org catalog-org-link" style="color:' +
        fg +
        ";background:" +
        bg +
        '" onclick="event.stopPropagation();openCompanyProfile(\'' +
        escHtml(g.company_slug) +
        "')\">"
      : '<span class="catalog-org" style="color:' +
        fg +
        ";background:" +
        bg +
        '">') +
    escHtml(g.company_name || g.org) +
    "</span>" +
    catTierBadge +
    "</div>" +
    '<div class="catalog-header-right">' +
    llmBadge +
    '<div class="catalog-thumb-btns">' +
    thumbBtns +
    "</div></div>" +
    "</div>" +
    '<h3 class="catalog-title">' +
    titleHtml +
    "</h3>" +
    '<div class="catalog-loc-row">' +
    locChips +
    "</div>" +
    compHtml +
    '<div class="catalog-badges">' +
    regionBadge +
    "</div>" +
    hardReqHtml(g) +
    summaryHtml +
    expandBtn +
    fullDescHtml +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Card actions
// ---------------------------------------------------------------------------

export function catalogThumbAction(canonId, memberIds, action) {
  const targetStatus =
    action === "like" ? "liked" : action === "pass" ? "passed" : "unseen";
  const card = document.querySelector(
    '.catalog-card[data-id="' + canonId + '"]',
  );
  if (card) {
    card.classList.add("dismissing");
    setTimeout(function () {
      updateStatus(canonId, memberIds, targetStatus);
    }, 200);
  } else {
    updateStatus(canonId, memberIds, targetStatus);
  }
}

export function toggleCatalogExpand(btn) {
  const card = btn.closest(".catalog-card");
  const desc = card.querySelector(".catalog-full-desc");
  if (!desc) return;
  const expanded = desc.classList.toggle("expanded");
  btn.textContent = expanded
    ? T("catalog_collapse", "Collapse")
    : T("catalog_details", "Details");
}
