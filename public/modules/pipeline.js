// =============================================================================
// pipeline.js — Pipeline/triage view: kanban board, funnel, cards
// =============================================================================

import {
  state,
  groups,
  groupsById,
  stats,
  triageReviews,
  TRIAGE_COLUMNS,
  STATUS_PRI,
  STATUS_BASKET,
  getGroupStatus,
  isGroupCompanyApproved,
} from "./state.js";
import {
  escHtml,
  normalizeDedupeText,
  getTriageDedupeKey,
  isVacancyExpired,
} from "./helpers.js";
import { T } from "./i18n.js";

// ---------------------------------------------------------------------------
// Triage helpers
// ---------------------------------------------------------------------------

// Grouping mode for the triage board: "grouped" collapses a company's roles
// into one card per column; "ungrouped" keeps one card per role (legacy view).
const GROUP_MODE_KEY = "triageGroupMode";
let groupMode = (function () {
  try {
    return localStorage.getItem(GROUP_MODE_KEY) === "ungrouped"
      ? "ungrouped"
      : "grouped";
  } catch (e) {
    return "grouped";
  }
})();

function setGroupMode(mode) {
  if (mode !== "grouped" && mode !== "ungrouped") return;
  if (mode === groupMode) return;
  groupMode = mode;
  try {
    localStorage.setItem(GROUP_MODE_KEY, mode);
  } catch (e) {
    /* localStorage unavailable — keep in-memory only */
  }
  renderPipeline();
}

function companyKey(g) {
  return g.company_slug || normalizeDedupeText(g.org);
}

function getReviewForGroup(g, reviewByVid) {
  if (reviewByVid[g.id]) return reviewByVid[g.id];
  const memberIds = Array.isArray(g.member_ids) ? g.member_ids : [];
  for (const mid of memberIds) {
    if (reviewByVid[mid]) return reviewByVid[mid];
  }
  return null;
}

function scrollToTriageColumn(colKey) {
  const el = document.getElementById("triageCol-" + colKey);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "start", inline: "nearest" });
}

// ---------------------------------------------------------------------------
// Funnel visualization
// ---------------------------------------------------------------------------

function renderTriageFunnel(funnelEl, metrics) {
  if (!funnelEl) return;

  const stages = [
    {
      key: "base_total",
      label: T("funnel_in_database", "In database"),
      count: metrics.base_total,
      hint: "all vacancies",
      scrollTo: "",
    },
    {
      key: "liked_queue",
      label: T("funnel_liked", "Liked"),
      count: metrics.liked_queue,
      hint: "queued for triage",
      scrollTo: "liked",
    },
    {
      key: "triaged_total",
      label: T("funnel_triaged", "Triaged"),
      count: metrics.triaged_total,
      hint: "review done",
      scrollTo: "to_apply",
    },
    {
      key: "in_work",
      label: T("funnel_in_progress", "In progress"),
      count: metrics.in_work,
      hint: "apply + research + network",
      scrollTo: "to_apply",
    },
    {
      key: "applied_total",
      label: T("funnel_applied", "Applied"),
      count: metrics.applied_total,
      hint: "submitted applications",
      scrollTo: "applied",
    },
    {
      key: "rejected_total",
      label: T("funnel_passed", "Passed"),
      count: metrics.rejected_total,
      hint: "catalog + triage",
      scrollTo: "",
    },
  ];

  funnelEl.innerHTML =
    '<div class="triage-funnel-track">' +
    stages
      .map(function (stage, idx) {
        const isAction = !!stage.scrollTo;
        const tag = isAction ? "button" : "div";
        const cls =
          "triage-stage" +
          (isAction ? " triage-stage-btn" : "") +
          " stage-" +
          stage.key;
        const attrs = isAction
          ? ' type="button" data-scroll-col="' + stage.scrollTo + '"'
          : "";
        return (
          "<" +
          tag +
          ' class="' +
          cls +
          '"' +
          attrs +
          ">" +
          '<span class="triage-stage-count">' +
          stage.count +
          "</span>" +
          '<span class="triage-stage-label">' +
          stage.label +
          "</span>" +
          '<span class="triage-stage-hint">' +
          stage.hint +
          "</span>" +
          "</" +
          tag +
          ">" +
          (idx < stages.length - 1
            ? '<span class="triage-stage-arrow">\u2192</span>'
            : "")
        );
      })
      .join("") +
    "</div>";

  funnelEl.querySelectorAll("[data-scroll-col]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      scrollToTriageColumn(btn.getAttribute("data-scroll-col"));
    });
  });
}

function renderTriageControls(controlsEl, metrics) {
  if (!controlsEl) return;
  controlsEl.innerHTML =
    '<div class="triage-group-toggle">' +
    '<button type="button" class="triage-group-btn' +
    (groupMode === "grouped" ? " active" : "") +
    '" data-group-mode="grouped">' +
    T("triage_grouped", "Grouped") +
    "</button>" +
    '<button type="button" class="triage-group-btn' +
    (groupMode === "ungrouped" ? " active" : "") +
    '" data-group-mode="ungrouped">' +
    T("triage_ungrouped", "Ungrouped") +
    "</button>" +
    "</div>";
  controlsEl.querySelectorAll("[data-group-mode]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setGroupMode(btn.getAttribute("data-group-mode"));
    });
  });
}

// ---------------------------------------------------------------------------
// Triage card
// ---------------------------------------------------------------------------

function buildTriageCard(g, col, review) {
  const firstUrl =
    (g.locations || []).find(function (l) {
      return !!l.url;
    })?.url ||
    g.org_url ||
    "";
  const locs = (g.locations || [])
    .map(function (l) {
      return l.location;
    })
    .join(", ");
  const isCompact = !!col.compact;

  let meta = "";
  if (!isCompact && review) {
    if (review.deadline)
      meta +=
        '<div class="pipe-card-deadline">\u23F0 ' + review.deadline + "</div>";
    if (review.cv_notes)
      meta += '<div class="pipe-card-note">' + review.cv_notes + "</div>";
    if (review.research_question)
      meta +=
        '<div class="pipe-card-note">' + review.research_question + "</div>";
    if (review.network_contact)
      meta +=
        '<div class="pipe-card-note">\uD83D\uDC64 ' +
        review.network_contact +
        "</div>";
    if (review.skip_reason)
      meta +=
        '<div class="pipe-card-note muted">' + review.skip_reason + "</div>";
    if (review.github_issue)
      meta += '<div class="pipe-card-issue">#' + review.github_issue + "</div>";
  }

  const orgHtml = g.company_slug
    ? '<button type="button" class="pipe-card-org pipe-card-org-link" style="color:' +
      (g.org_color ? g.org_color[0] : "var(--coral)") +
      '" data-company-slug="' +
      escHtml(g.company_slug) +
      '" title="Open company card">' +
      escHtml(g.org) +
      "</button>"
    : '<div class="pipe-card-org" style="color:' +
      (g.org_color ? g.org_color[0] : "var(--coral)") +
      '">' +
      escHtml(g.org) +
      "</div>";

  const titleHtml = firstUrl
    ? '<a class="pipe-card-title-link" href="' +
      escHtml(firstUrl) +
      '" target="_blank" rel="noopener" title="Open external vacancy">' +
      escHtml(g.title) +
      "</a>"
    : escHtml(g.title);

  const openLinkHtml = firstUrl
    ? '<div class="pipe-card-actions"><a class="pipe-card-open-link" href="' +
      escHtml(firstUrl) +
      '" target="_blank" rel="noopener">Open \u2197</a></div>'
    : "";

  return (
    '<div class="pipe-card' +
    (isCompact ? " compact" : " expanded") +
    '">' +
    orgHtml +
    '<div class="pipe-card-title">' +
    titleHtml +
    "</div>" +
    (g.llm_summary
      ? '<div class="pipe-card-summary">' + escHtml(g.llm_summary) + "</div>"
      : "") +
    '<div class="pipe-card-loc">' +
    escHtml(locs || "\u2014") +
    "</div>" +
    (g.llm_score != null
      ? '<span class="pipe-card-score">' + g.llm_score + "</span>"
      : "") +
    openLinkHtml +
    meta +
    "</div>"
  );
}

// One card for a company with several roles in the SAME column. The column
// already conveys the status, so no per-card status badge is needed.
function buildTriageGroupCard(entries, col) {
  const head = entries[0];
  const orgColor = head.org_color ? head.org_color[0] : "var(--coral)";
  const orgHtml = head.company_slug
    ? '<button type="button" class="pipe-card-org pipe-card-org-link" style="color:' +
      orgColor +
      '" data-company-slug="' +
      escHtml(head.company_slug) +
      '" title="Open company card">' +
      escHtml(head.org) +
      "</button>"
    : '<div class="pipe-card-org" style="color:' +
      orgColor +
      '">' +
      escHtml(head.org) +
      "</div>";

  const roles = entries.slice().sort(function (a, b) {
    return (b.llm_score || 0) - (a.llm_score || 0);
  });

  const rolesHtml = roles
    .map(function (g) {
      const firstUrl =
        (g.locations || []).find(function (l) {
          return !!l.url;
        })?.url ||
        g.org_url ||
        "";
      const locs = (g.locations || [])
        .map(function (l) {
          return l.location;
        })
        .join(", ");
      const titleHtml = firstUrl
        ? '<a class="pipe-grp-role-title" href="' +
          escHtml(firstUrl) +
          '" target="_blank" rel="noopener" title="Open external vacancy">' +
          escHtml(g.title) +
          "</a>"
        : '<span class="pipe-grp-role-title">' + escHtml(g.title) + "</span>";
      return (
        '<li class="pipe-grp-role">' +
        '<div class="pipe-grp-role-head">' +
        titleHtml +
        (g.llm_score != null
          ? '<span class="pipe-grp-role-score">' + g.llm_score + "</span>"
          : "") +
        "</div>" +
        (locs
          ? '<div class="pipe-grp-role-loc">' + escHtml(locs) + "</div>"
          : "") +
        "</li>"
      );
    })
    .join("");

  return (
    '<div class="pipe-card pipe-card-group' +
    (col.compact ? " compact" : " expanded") +
    '">' +
    orgHtml +
    '<span class="pipe-grp-count">' +
    entries.length +
    " roles</span>" +
    '<ul class="pipe-grp-roles">' +
    rolesHtml +
    "</ul>" +
    "</div>"
  );
}

// Build one column's cards. Ungrouped: one card per role. Grouped: a company
// with 2+ roles in this column collapses into a single grouped card.
function buildColumnCards(entries, col) {
  const sorted = entries.slice().sort(function (a, b) {
    return (b.llm_score || 0) - (a.llm_score || 0);
  });
  if (groupMode !== "grouped") {
    return sorted
      .map(function (g) {
        return buildTriageCard(g, col, g._review);
      })
      .join("");
  }
  const byOrg = new Map();
  sorted.forEach(function (g) {
    const k = companyKey(g);
    if (!byOrg.has(k)) byOrg.set(k, []);
    byOrg.get(k).push(g);
  });
  return Array.from(byOrg.values())
    .map(function (grp) {
      return grp.length > 1
        ? buildTriageGroupCard(grp, col)
        : buildTriageCard(grp[0], col, grp[0]._review);
    })
    .join("");
}

// ---------------------------------------------------------------------------
// Render full pipeline
// ---------------------------------------------------------------------------

export function renderPipeline() {
  const board = document.getElementById("pipelineBoard");
  const funnelEl =
    document.getElementById("triageFunnel") ||
    document.getElementById("pipelineStats");
  const controlsEl = document.getElementById("triageBoardControls");
  if (!board) return;

  const reviews = triageReviews || [];
  const reviewByVid = {};
  reviews.forEach(function (r) {
    reviewByVid[r.vacancy_id] = r;
  });

  const buckets = {};
  TRIAGE_COLUMNS.forEach(function (col) {
    buckets[col.key] = [];
  });

  const deduped = new Map();
  let catalogRejectedTotal = 0;
  const visibleGroups = groups.filter((g) => isGroupCompanyApproved(g));
  visibleGroups.forEach(function (g) {
    const status = getGroupStatus(g);
    if ((STATUS_BASKET[status] || "unseen") === "passed") {
      catalogRejectedTotal += 1;
    }
    var entry = Object.assign({}, g);
    entry._status = status;
    entry._review = getReviewForGroup(g, reviewByVid);
    const key = getTriageDedupeKey(g);
    const prev = deduped.get(key);
    if (!prev) {
      deduped.set(key, entry);
      return;
    }
    const prevP = STATUS_PRI[prev._status] ?? 99;
    const nextP = STATUS_PRI[entry._status] ?? 99;
    if (nextP < prevP) {
      if (!entry._review && prev._review) entry._review = prev._review;
      deduped.set(key, entry);
    } else if (!prev._review && entry._review) {
      prev._review = entry._review;
    }
  });
  deduped.forEach(function (entry) {
    if (buckets[entry._status] !== undefined) {
      // Exclude expired vacancies from the liked column —
      // they are closed and should not appear in the triage queue
      if (entry._status === "liked" && isVacancyExpired(entry)) {
        return;
      }
      buckets[entry._status].push(entry);
    }
  });

  const metrics = {
    base_total: stats.total_roles || 0,
    liked_queue: buckets.liked.length,
    triaged_total:
      buckets.to_apply.length +
      buckets.to_research.length +
      buckets.to_network.length +
      buckets.skipped.length +
      buckets.applied.length,
    in_work:
      buckets.to_apply.length +
      buckets.to_research.length +
      buckets.to_network.length,
    applied_total: buckets.applied.length,
    skipped_total: buckets.skipped.length,
    rejected_total: catalogRejectedTotal,
  };

  renderTriageFunnel(funnelEl, metrics);
  renderTriageControls(controlsEl, metrics);

  var totalTracked = 0;
  TRIAGE_COLUMNS.forEach(function (col) {
    totalTracked += buckets[col.key].length;
  });

  if (totalTracked === 0) {
    board.innerHTML =
      '<div class="pipeline-empty">' +
      '<div class="pipeline-empty-icon">\uD83D\uDCCB</div>' +
      "<p>Triage is empty. Like vacancies in the Catalog and run <code>/triage</code>.</p>" +
      "</div>";
    return;
  }

  board.innerHTML = TRIAGE_COLUMNS.map(function (col) {
    var cards = buildColumnCards(buckets[col.key], col);

    return (
      '<div class="pipe-col" id="triageCol-' +
      col.key +
      '">' +
      '<div class="pipe-col-header" style="border-color:' +
      col.color +
      '">' +
      '<span class="pipe-col-title">' +
      col.label +
      "</span>" +
      '<span class="pipe-col-count">' +
      buckets[col.key].length +
      "</span></div>" +
      '<div class="pipe-col-cards">' +
      (cards || '<div class="pipe-col-empty">\u2014</div>') +
      "</div></div>"
    );
  }).join("");

  // Bind company profile openers (after DOM mount)
  board
    .querySelectorAll(".pipe-card-org-link[data-company-slug]")
    .forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        const slug = el.getAttribute("data-company-slug");
        if (slug) window.openCompanyProfile(slug);
      });
    });
}
