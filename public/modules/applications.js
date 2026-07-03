// =============================================================================
// applications.js — Applications section: everything you've applied to, as a
// flat grid table (fit · role · company · stage · sent). Data comes straight
// from the baked payload (works offline): each vacancy group carries an
// `application` projection (status + channel + date + artifact KEYS only —
// no private values; see scripts/report/data_prep.py _project_application).
// Archived vacancies carry it too, so a role you applied to that later left
// the active list is not lost here — it just renders without a link (its
// vacancy page would 404, same reasoning as archive.js).
// =============================================================================

import { state, groups, archivedGroups, groupsById } from "./state.js";
import { escHtml, relativeTime, qualityBand, jsAttr } from "./helpers.js";
import { T } from "./i18n.js";

// Lifecycle statuses an application row can carry (applications.py
// VALID_STATUSES). Ordered for the filter chips + status ordering.
const STATUS_ORDER = [
  "applied",
  "interview",
  "offer",
  "rejected",
  "draft",
  "withdrawn",
];

// ---------------------------------------------------------------------------
// Pure: collect applications from vacancy groups, grouped by company.
// Deduplicated by vacancy id (a role may appear in both live + archived sets).
// Returns [{ key, org, company_slug, org_color, apps:[{...}] }] sorted with the
// most-recently-applied company first; apps within a company newest-first.
// Kept grouped-by-company (rather than flattened) so its existing tests and
// shape stay untouched; renderApplications flattens it for the grid table.
// ---------------------------------------------------------------------------
export function collectApplications(groupsArr, archivedArr) {
  const byCompany = new Map();
  const seen = new Set();

  const consume = (g) => {
    if (!g || !g.application || !g.id) return;
    if (seen.has(g.id)) return;
    seen.add(g.id);
    const key = g.company_slug || g.org || "—";
    if (!byCompany.has(key)) {
      byCompany.set(key, {
        key,
        org: g.org || "—",
        company_slug: g.company_slug || null,
        org_color: g.org_color || ["#F97316", "#FFF7ED"],
        apps: [],
      });
    }
    const a = g.application;
    byCompany.get(key).apps.push({
      id: g.id,
      title: g.title || "",
      status: a.status || "",
      channel: a.channel || "",
      applied_at: a.applied_at || "",
      artifacts: a.artifacts ? Object.keys(a.artifacts) : [],
      score: g.llm_score,
    });
  };

  (groupsArr || []).forEach(consume);
  (archivedArr || []).forEach(consume);

  const companies = Array.from(byCompany.values());
  const recency = (c) =>
    c.apps.reduce((m, a) => (a.applied_at > m ? a.applied_at : m), "");
  companies.forEach((c) =>
    c.apps.sort((a, b) =>
      (b.applied_at || "").localeCompare(a.applied_at || ""),
    ),
  );
  companies.sort((a, b) => recency(b).localeCompare(recency(a)));
  return companies;
}

/** Pure: total application count and a {status: count} breakdown. */
export function summarizeApplications(companies) {
  const byStatus = {};
  let total = 0;
  for (const c of companies) {
    for (const a of c.apps) {
      total += 1;
      byStatus[a.status] = (byStatus[a.status] || 0) + 1;
    }
  }
  return { total, byStatus };
}

// ---------------------------------------------------------------------------
// Render — grid table (U13, DHA-397, design-protocol.md #6).
// ---------------------------------------------------------------------------

function _statusLabel(status, t) {
  const tf = t || T;
  return tf("app_status_" + status, status || "—");
}

// Stage → chip color (design-protocol.md #1: colour = meaning). The lifecycle
// has 6 statuses but only 4 genuinely distinct states worth a color:
//   offer                -> good (a positive terminal outcome)
//   applied / interview  -> moderate (in play, awaiting a response — the same
//                           ochre the sidebar's sync-status already uses for
//                           "pending/not-yet-resolved", not a quality score)
//   rejected / withdrawn -> weak (a negative terminal outcome)
//   draft                -> neutral (not sent yet, no outcome to signal)
// None of this touches cobalt — cobalt stays reserved for interaction/selection.
const STAGE_CHIP_CLASS = {
  offer: "apl-stage-good",
  applied: "apl-stage-moderate",
  interview: "apl-stage-moderate",
  rejected: "apl-stage-weak",
  withdrawn: "apl-stage-weak",
  draft: "apl-stage-neutral",
};

export function stageChipClass(status) {
  return STAGE_CHIP_CLASS[status] || "apl-stage-neutral";
}

function _filterChips(byStatus, active) {
  const chips = [
    '<button class="apl-chip' +
      (active === "all" ? " active" : "") +
      '" data-app-status="all">' +
      escHtml(T("apps_filter_all", "All")) +
      "</button>",
  ];
  for (const s of STATUS_ORDER) {
    const n = byStatus[s] || 0;
    if (!n) continue;
    chips.push(
      '<button class="apl-chip' +
        (active === s ? " active" : "") +
        '" data-app-status="' +
        escHtml(s) +
        '">' +
        escHtml(_statusLabel(s)) +
        ' <span class="apl-chip-count">' +
        n +
        "</span></button>",
    );
  }
  return '<div class="apl-filter">' + chips.join("") + "</div>";
}

// Quiet count badge for submitted artifacts (cv/cover_letter/research_urls —
// the KEYS only; the DAL redacts values before this ever reaches the public
// payload, see _project_application, so there is nothing to link to). Sits
// next to the role title, full key list in the title tooltip — same
// compact-badge/full-detail-on-hover tradeoff primaryLocationInfo already
// makes for "+N more" locations elsewhere in this codebase. Renders nothing
// when no artifacts were recorded (the common case for older/manual entries).
function _artifactsBadge(artifacts, t) {
  if (!artifacts || !artifacts.length) return "";
  return (
    ' <span class="apl-artifacts" title="' +
    escHtml(
      t("apps_col_artifacts", "Artifacts") + ": " + artifacts.join(", "),
    ) +
    '">📎' +
    artifacts.length +
    "</span>"
  );
}

// One grid row: tinted fit tile · role (+ artifact badge) · company (linked
// when the company has a slug) · stage chip · sent date (+ channel). Pure (no
// DOM read) so the escaping and linked/unlinked tests can assert on it
// directly, same shape as todayRowHtml/buildArchiveRow. `a.live` decides the
// row's own click — an application whose vacancy is no longer in the live
// payload renders with no route (its vacancy page would just 404, same call
// archive.js already made).
export function applicationRowHtml(a, opts) {
  const t = (opts && opts.t) || ((key, fallback) => fallback);
  const scoreCls =
    a.score == null ? "vac-score--none" : "q-" + qualityBand(a.score) + "-bg";
  const scoreTxt = a.score == null ? "—" : String(a.score);
  const stageLabel = _statusLabel(a.status, t);
  const companyHtml = a.company_slug
    ? '<button type="button" class="apl-company-link" onclick="event.stopPropagation();openCompanyProfile(\'' +
      jsAttr(a.company_slug) +
      '\')" title="' +
      escHtml(t("apps_open_company", "Open company card")) +
      '">' +
      escHtml(a.org) +
      "</button>"
    : escHtml(a.org);
  // Channel rides along with the date rather than a 6th column — the mock's
  // grid is a fixed 5 columns, and a dedicated column for one short word
  // would widen every row for a field that's sometimes blank ("" for older/
  // manually-added entries with no recorded channel).
  const sentText = a.applied_at ? escHtml(relativeTime(a.applied_at, t)) : "—";
  const channelText = a.channel ? " · " + escHtml(a.channel) : "";
  const rowCls = "apl-row" + (a.live ? "" : " apl-row-unlinked");
  // Only a live row opens anything — an unlinked one (its vacancy left the
  // live payload) has nothing to navigate to, so it stays a plain, non-
  // focusable row (R12: don't hand keyboard focus to a dead control).
  const rowClick = a.live
    ? ' role="button" tabindex="0" onclick="openApplicationRow(\'' +
      jsAttr(a.id) +
      "')\" onkeydown=\"if(event.key==='Enter'&&event.target===event.currentTarget){openApplicationRow('" +
      jsAttr(a.id) +
      "')}\""
    : "";
  return (
    '<div class="' +
    rowCls +
    '" data-id="' +
    escHtml(a.id) +
    '"' +
    rowClick +
    ">" +
    '<div class="apl-score ' +
    scoreCls +
    '">' +
    scoreTxt +
    "</div>" +
    '<div class="apl-role">' +
    escHtml(a.title) +
    _artifactsBadge(a.artifacts, t) +
    "</div>" +
    '<div class="apl-company">' +
    companyHtml +
    "</div>" +
    '<div><span class="apl-stage ' +
    stageChipClass(a.status) +
    '">' +
    escHtml(stageLabel) +
    "</span></div>" +
    '<div class="apl-sent">' +
    sentText +
    channelText +
    "</div>" +
    "</div>"
  );
}

// Thin DOM shell: forwards a row click to the U4 router with the "applied"
// context, so vacancyMoveToApply confirms in place instead of auto-advancing
// (F3's auto-advance is Browse-only) — Applied has no unreviewed queue to
// advance through. Exposed on window (app.js) for the row's onclick.
export function openApplicationRow(id) {
  window.openVacancyRoute(id, { context: "applied" });
}

// Built fresh per render (not a static data-i18n element) since renderApplications
// re-runs on every poll/filter change and applyI18n() only sweeps the DOM once
// at startup — matching how the rest of this file resolves strings via T().
function _tableHeadHtml() {
  return (
    '<div class="apl-row apl-row-head">' +
    "<div>" +
    escHtml(T("apps_col_fit", "Fit")) +
    "</div>" +
    "<div>" +
    escHtml(T("apps_col_role", "Role")) +
    "</div>" +
    "<div>" +
    escHtml(T("apps_col_company", "Company")) +
    "</div>" +
    "<div>" +
    escHtml(T("apps_col_stage", "Stage")) +
    "</div>" +
    "<div>" +
    escHtml(T("apps_col_sent", "Sent")) +
    "</div>" +
    "</div>"
  );
}

// Pure page states, exported so the empty/first-run view is testable without
// a DOM — same convention as vacancy.js's vacancyNotFoundHtml.
export function applicationsHeaderHtml(opts) {
  const t = (opts && opts.t) || T;
  return (
    '<div class="apl-header">' +
    '<span class="apl-title">' +
    escHtml(t("apps_title", "Applications")) +
    "</span>" +
    '<span class="apl-hint">' +
    escHtml(t("apps_sub", "Sent applications and where each one stands.")) +
    "</span></div>"
  );
}

export function applicationsEmptyHtml(opts) {
  const t = (opts && opts.t) || T;
  return (
    '<div class="apl-sheet apl-empty-sheet"><div class="apl-empty">' +
    '<div class="apl-empty-icon">✉️</div>' +
    escHtml(
      t(
        "apps_empty",
        "No applications yet. Mark a role applied in Triage or Today.",
      ),
    ) +
    "</div></div>"
  );
}

export function renderApplications() {
  const root = document.getElementById("applicationsSection");
  if (!root) return;

  const companies = collectApplications(groups, archivedGroups);
  const { total, byStatus } = summarizeApplications(companies);
  const active = state.appStatusFilter || "all";

  const header = applicationsHeaderHtml({ t: T });

  if (total === 0) {
    root.innerHTML = header + applicationsEmptyHtml({ t: T });
    return;
  }

  // Flatten to one row per application — company is a grid column now, not a
  // section header — and sort by send date globally. collectApplications's
  // per-company clustering (built for the old grouped view) would otherwise
  // interleave an old app from a "recent" company ahead of a newer app from
  // another, which reads as out of order in a single flat table.
  const flat = companies
    .flatMap((c) =>
      c.apps.map((a) => ({
        ...a,
        org: c.org,
        company_slug: c.company_slug,
        live: groupsById.has(a.id),
      })),
    )
    .sort((a, b) => (b.applied_at || "").localeCompare(a.applied_at || ""));

  const shown =
    active === "all" ? flat : flat.filter((a) => a.status === active);

  const rowsHtml = shown.length
    ? shown.map((a) => applicationRowHtml(a, { t: T })).join("")
    : '<div class="apl-empty">' +
      escHtml(T("apps_none_match", "Nothing matches this status.")) +
      "</div>";

  root.innerHTML =
    header +
    _filterChips(byStatus, active) +
    '<div class="apl-sheet"><div class="apl-table">' +
    _tableHeadHtml() +
    rowsHtml +
    "</div></div>";

  root.querySelectorAll(".apl-chip[data-app-status]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.appStatusFilter = btn.getAttribute("data-app-status");
      renderApplications();
    });
  });
}

export function initApplications() {
  renderApplications();
}
