// =============================================================================
// pipeline.js — Pipeline/triage view: kanban board, funnel, cards
// =============================================================================

import {
  state,
  groups,
  groupsById,
  triageReviews,
  TRIAGE_COLUMNS,
  STATUS_PRI,
  STATUS_BASKET,
  getGroupStatus,
  isGroupCompanyApproved,
  updateStatus,
  getCompanies,
} from "./state.js";
import {
  escHtml,
  normalizeDedupeText,
  computeTriageFunnel,
  formatDeadlineHtml,
  isVacancyStale,
  sourceAgeDays,
  qualityClass,
  safeUrl,
  resolveVacancyCompany,
} from "./helpers.js";
import { T, dateLocale } from "./i18n.js";
import Sortable from "../vendor/sortable.esm.js";

// Live Sortable instances, one per column. Rebuilt on every render since the
// board's innerHTML is replaced wholesale; old instances are destroyed first
// to avoid leaking listeners on detached nodes.
let sortableInstances = [];

// ---------------------------------------------------------------------------
// Triage helpers
// ---------------------------------------------------------------------------

// Triage cards are always grouped by company within each status column: a
// company with 2+ roles in a column collapses into one card.
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

// Header strip (design-protocol.md #6 "Board (Triage)"): the "Triage" title
// plus an inline funnel track "in db \u2192 liked \u2192 triaged \u2192 in progress \u2192
// applied \u2192 passed", each actionable stage jumping to its column. Sits on the
// material \u2014 only .pipeline-board (the columns) is seated in the sheet.
function renderTriageFunnel(funnelEl, metrics) {
  if (!funnelEl) return;

  const stages = [
    {
      key: "base_total",
      label: T("funnel_in_database", "In database"),
      count: metrics.base_total,
      scrollTo: "",
    },
    {
      key: "liked_queue",
      label: T("funnel_liked", "Liked"),
      count: metrics.liked_queue,
      scrollTo: "liked",
    },
    {
      key: "triaged_total",
      label: T("funnel_triaged", "Triaged"),
      count: metrics.triaged_total,
      scrollTo: "to_apply",
    },
    {
      key: "in_work",
      label: T("funnel_in_progress", "In progress"),
      count: metrics.in_work,
      scrollTo: "to_apply",
    },
    {
      key: "applied_total",
      label: T("funnel_applied", "Applied"),
      count: metrics.applied_total,
      scrollTo: "applied",
    },
    {
      key: "rejected_total",
      label: T("funnel_passed", "Passed"),
      count: metrics.rejected_total,
      scrollTo: "",
    },
  ];

  const track = stages
    .map(function (stage, idx) {
      const isAction = !!stage.scrollTo;
      const tag = isAction ? "button" : "span";
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
        "</span> " +
        stage.label +
        "</" +
        tag +
        ">" +
        (idx < stages.length - 1
          ? '<span class="triage-stage-arrow">\u2192</span>'
          : "")
      );
    })
    .join("");

  funnelEl.innerHTML =
    '<span class="triage-title">' +
    escHtml(T("tab_triage", "Triage")) +
    "</span>" +
    '<div class="triage-funnel-track">' +
    track +
    "</div>" +
    '<span class="triage-hint">' +
    escHtml(T("triage_drag_hint", "drag cards between columns")) +
    "</span>";

  funnelEl.querySelectorAll("[data-scroll-col]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      scrollToTriageColumn(btn.getAttribute("data-scroll-col"));
    });
  });
}

function renderTriageControls(controlsEl, metrics) {
  if (!controlsEl) return;
  controlsEl.innerHTML = "";
}

// ---------------------------------------------------------------------------
// Triage card
// ---------------------------------------------------------------------------

const MOVE_LABELS = {
  liked: "Like",
  to_apply: "Apply",
  to_research: "Research",
  to_network: "Network",
  applied: "Applied ✓",
  skipped: "Skip",
};

// One compact, colour-coded button per column the card is NOT already in.
// Shared by single and grouped cards; the click handler reads data-canon-ids
// off the enclosing .pipe-card and moves every role to the target column.
function buildMoveBtns(curColKey) {
  return (
    '<div class="triage-move-btns">' +
    TRIAGE_COLUMNS.filter(function (c) {
      // Derived columns ('expired') aren't real statuses — no move-to button.
      return c.key !== curColKey && !c.derived;
    })
      .map(function (c) {
        const lbl = MOVE_LABELS[c.key] || c.label;
        return (
          '<button type="button" class="triage-move-btn" data-move-to="' +
          c.key +
          '" style="border-color:' +
          c.color +
          ";color:" +
          c.color +
          '" title="Move to ' +
          escHtml(lbl) +
          '">' +
          escHtml(lbl) +
          "</button>"
        );
      })
      .join("") +
    "</div>"
  );
}

// Deadline (urgency-coloured) or, when a role has no deadline but its source
// went quiet, a staleness line reusing the Catalog freshness badge look.
function buildTriageFreshness(g) {
  const dl = formatDeadlineHtml(g.deadline, "pipe-deadline", {
    t: T,
    locale: dateLocale(),
  });
  if (dl) return '<div class="pipe-card-fresh">' + dl + "</div>";
  if (isVacancyStale(g)) {
    const age = sourceAgeDays(g.last_seen);
    const text = T(
      "triage_stale_seen",
      "not seen at source for {n} days",
    ).replace("{n}", age);
    return (
      '<div class="pipe-card-fresh"><span class="card-freshness stale" title="' +
      escHtml(
        T(
          "freshness_stale_hint",
          "based on the last time the source confirmed the role; direct ATS is exact, aggregators approximate",
        ),
      ) +
      '">' +
      escHtml(text) +
      "</span></div>"
    );
  }
  return "";
}

// Exported for unit tests (pipeline.test.js) — the private triage fields it
// renders must stay HTML-escaped (DHA-373). Not part of the module's runtime
// surface; renderPipeline is the only caller in the app.
// `companies` is optional (existing tests call this with 3 args): omitting it
// just means resolveVacancyCompany finds no match, same as before this org
// link had a real resolver behind it (post-ship fast fix #6).
export function buildTriageCard(g, col, review, companies) {
  const firstUrl = safeUrl(
    (g.locations || []).find(function (l) {
      return !!l.url;
    })?.url ||
      g.org_url ||
      "",
  );
  const locs = (g.locations || [])
    .map(function (l) {
      return l.location;
    })
    .join(", ");
  const isCompact = !!col.compact;

  let meta = "";
  if (!isCompact && review) {
    // Private triage fields are free-text the user types; escape every one
    // before it reaches innerHTML (a cv_note like "<img onerror=...>" must
    // render inert, not execute). See DHA-373.
    if (review.deadline)
      meta +=
        '<div class="pipe-card-deadline">\u23F0 ' +
        escHtml(review.deadline) +
        "</div>";
    if (review.cv_notes)
      meta +=
        '<div class="pipe-card-note">' + escHtml(review.cv_notes) + "</div>";
    if (review.research_question)
      meta +=
        '<div class="pipe-card-note">' +
        escHtml(review.research_question) +
        "</div>";
    if (review.network_contact)
      meta +=
        '<div class="pipe-card-note">\uD83D\uDC64 ' +
        escHtml(review.network_contact) +
        "</div>";
    if (review.skip_reason)
      meta +=
        '<div class="pipe-card-note muted">' +
        escHtml(review.skip_reason) +
        "</div>";
    if (review.github_issue)
      meta +=
        '<div class="pipe-card-issue">#' +
        escHtml(review.github_issue) +
        "</div>";
  }

  const orgCompany = resolveVacancyCompany(g, companies);
  const orgHtml = orgCompany
    ? '<button type="button" class="pipe-card-org pipe-card-org-link" data-company-slug="' +
      escHtml(orgCompany.slug) +
      '" title="Open company card">' +
      escHtml(g.org) +
      "</button>"
    : '<div class="pipe-card-org">' + escHtml(g.org) + "</div>";

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
    '" data-canon-ids="' +
    escHtml(JSON.stringify([g.id])) +
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
      ? '<span class="pipe-card-score ' +
        qualityClass(g.llm_score) +
        '">' +
        g.llm_score +
        "</span>"
      : "") +
    buildTriageFreshness(g) +
    openLinkHtml +
    meta +
    buildMoveBtns(col.key) +
    "</div>"
  );
}

// One card for a company with several roles in the SAME column. The column
// already conveys the status, so no per-card status badge is needed.
function buildTriageGroupCard(entries, col, companies) {
  const head = entries[0];
  const headCompany = resolveVacancyCompany(head, companies);
  const orgHtml = headCompany
    ? '<button type="button" class="pipe-card-org pipe-card-org-link" data-company-slug="' +
      escHtml(headCompany.slug) +
      '" title="Open company card">' +
      escHtml(head.org) +
      "</button>"
    : '<div class="pipe-card-org">' + escHtml(head.org) + "</div>";

  const roles = entries.slice().sort(function (a, b) {
    return (b.llm_score || 0) - (a.llm_score || 0);
  });

  const rolesHtml = roles
    .map(function (g) {
      const firstUrl = safeUrl(
        (g.locations || []).find(function (l) {
          return !!l.url;
        })?.url ||
          g.org_url ||
          "",
      );
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
          ? '<span class="pipe-grp-role-score ' +
            qualityClass(g.llm_score) +
            '">' +
            g.llm_score +
            "</span>"
          : "") +
        "</div>" +
        (locs
          ? '<div class="pipe-grp-role-loc">' + escHtml(locs) + "</div>"
          : "") +
        buildTriageFreshness(g) +
        "</li>"
      );
    })
    .join("");

  const canonIds = entries.map(function (g) {
    return g.id;
  });

  return (
    '<div class="pipe-card pipe-card-group' +
    (col.compact ? " compact" : " expanded") +
    '" data-canon-ids="' +
    escHtml(JSON.stringify(canonIds)) +
    '">' +
    orgHtml +
    '<span class="pipe-grp-count">' +
    entries.length +
    " roles</span>" +
    '<ul class="pipe-grp-roles">' +
    rolesHtml +
    "</ul>" +
    buildMoveBtns(col.key) +
    "</div>"
  );
}

// Build one column's cards. Ungrouped: one card per role. Grouped: a company
// with 2+ roles in this column collapses into a single grouped card.
function buildColumnCards(entries, col, companies) {
  const sorted = entries.slice().sort(function (a, b) {
    return (b.llm_score || 0) - (a.llm_score || 0);
  });
  const byOrg = new Map();
  sorted.forEach(function (g) {
    const k = companyKey(g);
    if (!byOrg.has(k)) byOrg.set(k, []);
    byOrg.get(k).push(g);
  });
  return Array.from(byOrg.values())
    .map(function (grp) {
      return grp.length > 1
        ? buildTriageGroupCard(grp, col, companies)
        : buildTriageCard(grp[0], col, grp[0]._review, companies);
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
  // For resolving each card's org-name link (post-ship fast fix #6) —
  // computed once per render, not per card.
  const companies = getCompanies();

  // Tag every group with the two state.js-dependent facts the pure funnel
  // helper needs (status, company approval) plus its private review, then
  // hand the whole thing to computeTriageFunnel — the SAME dedupe/column
  // reduction backs both the header strip and the board below, so they can
  // never disagree (DHA-396, U12; pipeline.js can't be imported under
  // `node --test`, so the derivation itself lives in helpers.js).
  const entries = groups.map(function (g) {
    var entry = Object.assign({}, g);
    entry._status = getGroupStatus(g);
    entry._approved = isGroupCompanyApproved(g);
    entry._review = getReviewForGroup(g, reviewByVid);
    return entry;
  });
  const columnKeys = new Set(TRIAGE_COLUMNS.map((c) => c.key));
  const { buckets, metrics } = computeTriageFunnel(entries, {
    statusPri: STATUS_PRI,
    statusBasket: STATUS_BASKET,
    columnKeys: columnKeys,
  });

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
    var cards = buildColumnCards(buckets[col.key], col, companies);

    return (
      '<div class="pipe-col" id="triageCol-' +
      col.key +
      '">' +
      '<div class="pipe-col-header">' +
      '<span class="pipe-col-dot" style="background:' +
      col.color +
      '"></span>' +
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

  // Move every role on a card (one for a single card, all for a grouped card)
  // to the target column. updateStatus emits "statusChanged"; app.js handles
  // save + re-render. Shared by the move-buttons and drag-and-drop.
  function moveCardToColumn(card, target) {
    if (!card || !target) return;
    let ids;
    try {
      ids = JSON.parse(card.getAttribute("data-canon-ids"));
    } catch (_) {
      return;
    }
    (ids || []).forEach(function (id) {
      const g = groupsById.get(id);
      if (g) updateStatus(g.id, g.member_ids || [], target);
    });
  }

  // Bind move buttons.
  board
    .querySelectorAll(".triage-move-btn[data-move-to]")
    .forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        moveCardToColumn(
          btn.closest(".pipe-card[data-canon-ids]"),
          btn.getAttribute("data-move-to"),
        );
      });
    });

  // Drag-and-drop: each column's card list is a Sortable connected to the
  // shared "triage" group, so cards drag between columns. Dropping into a
  // different column moves the role(s) to that column's status. Derived columns
  // ('expired') accept no drops — cards drag OUT to a decision but never IN.
  sortableInstances.forEach(function (s) {
    s.destroy();
  });
  sortableInstances = [];
  const derivedKeys = new Set(
    TRIAGE_COLUMNS.filter((c) => c.derived).map((c) => c.key),
  );
  board.querySelectorAll(".pipe-col-cards").forEach(function (listEl) {
    const colEl = listEl.closest(".pipe-col");
    const colKey =
      colEl && colEl.id.startsWith("triageCol-")
        ? colEl.id.slice("triageCol-".length)
        : "";
    sortableInstances.push(
      Sortable.create(listEl, {
        group: { name: "triage", pull: true, put: !derivedKeys.has(colKey) },
        animation: 150,
        draggable: ".pipe-card",
        ghostClass: "pipe-card-ghost",
        dragClass: "pipe-card-dragging",
        // Clicks on links/buttons must not start a drag — let them through.
        filter: "a, button",
        preventOnFilter: false,
        onEnd: function (evt) {
          if (evt.from === evt.to) return; // reorder within a column — no status change
          const col = evt.to.closest(".pipe-col");
          if (!col || !col.id.startsWith("triageCol-")) return;
          moveCardToColumn(evt.item, col.id.slice("triageCol-".length));
        },
      }),
    );
  });
}
