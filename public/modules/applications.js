// =============================================================================
// applications.js — Applications section: everything you've applied to, grouped
// by company. Data comes straight from the baked payload (works offline): each
// vacancy group carries an `application` projection (status + channel + date +
// artifact KEYS only — no private values; see scripts/report/data_prep.py
// _project_application). Archived vacancies carry it too, so a role you applied
// to that later left the active list is not lost here.
// =============================================================================

import { state, groups, archivedGroups } from "./state.js";
import { escHtml, relativeTime } from "./helpers.js";
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

function _vacUrl(g) {
  const loc = (g.locations || []).find((l) => l && l.url);
  return (loc && loc.url) || g.org_url || "";
}

// ---------------------------------------------------------------------------
// Pure: collect applications from vacancy groups, grouped by company.
// Deduplicated by vacancy id (a role may appear in both live + archived sets).
// Returns [{ key, org, company_slug, org_color, apps:[{...}] }] sorted with the
// most-recently-applied company first; apps within a company newest-first.
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
      url: _vacUrl(g),
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
// Render
// ---------------------------------------------------------------------------

function _statusLabel(status) {
  return T("app_status_" + status, status || "—");
}

function _filterChips(byStatus, active) {
  const chips = [
    '<button class="apps-chip' +
      (active === "all" ? " active" : "") +
      '" data-app-status="all">' +
      escHtml(T("apps_filter_all", "All")) +
      "</button>",
  ];
  for (const s of STATUS_ORDER) {
    const n = byStatus[s] || 0;
    if (!n) continue;
    chips.push(
      '<button class="apps-chip apps-chip-' +
        escHtml(s) +
        (active === s ? " active" : "") +
        '" data-app-status="' +
        escHtml(s) +
        '">' +
        escHtml(_statusLabel(s)) +
        ' <span class="apps-chip-count">' +
        n +
        "</span></button>",
    );
  }
  return '<div class="apps-filter">' + chips.join("") + "</div>";
}

function _appRow(a) {
  const roleHtml = a.url
    ? '<a href="' +
      escHtml(a.url) +
      '" target="_blank" rel="noopener">' +
      escHtml(a.title) +
      "</a>"
    : escHtml(a.title);
  const artifacts = a.artifacts.length
    ? '<span class="apps-artifacts">' +
      a.artifacts
        .map((k) => '<span class="apps-artifact">' + escHtml(k) + "</span>")
        .join("") +
      "</span>"
    : '<span class="apps-artifact-none">—</span>';
  const when = a.applied_at ? escHtml(relativeTime(a.applied_at)) : "—";
  return (
    '<tr class="apps-row">' +
    '<td class="apps-td apps-td-role">' +
    roleHtml +
    (a.score != null
      ? ' <span class="apps-score">' + a.score + "</span>"
      : "") +
    "</td>" +
    '<td class="apps-td"><span class="apps-status apps-status-' +
    escHtml(a.status) +
    '">' +
    escHtml(_statusLabel(a.status)) +
    "</span></td>" +
    '<td class="apps-td">' +
    (a.channel ? escHtml(a.channel) : "—") +
    "</td>" +
    '<td class="apps-td">' +
    when +
    "</td>" +
    '<td class="apps-td">' +
    artifacts +
    "</td>" +
    "</tr>"
  );
}

function _companyBlock(c) {
  const [fg, bg] = c.org_color || ["#F97316", "#FFF7ED"];
  const orgHtml = c.company_slug
    ? '<button type="button" class="apps-company-name apps-company-link" ' +
      'style="color:' +
      fg +
      ";background:" +
      bg +
      '" data-company-slug="' +
      escHtml(c.company_slug) +
      '" title="Open company card">' +
      escHtml(c.org) +
      "</button>"
    : '<span class="apps-company-name" style="color:' +
      fg +
      ";background:" +
      bg +
      '">' +
      escHtml(c.org) +
      "</span>";
  const head =
    "<thead><tr>" +
    "<th>" +
    escHtml(T("apps_col_role", "Role")) +
    "</th>" +
    "<th>" +
    escHtml(T("apps_col_status", "Status")) +
    "</th>" +
    "<th>" +
    escHtml(T("apps_col_channel", "Channel")) +
    "</th>" +
    "<th>" +
    escHtml(T("apps_col_date", "Applied")) +
    "</th>" +
    "<th>" +
    escHtml(T("apps_col_artifacts", "Artifacts")) +
    "</th>" +
    "</tr></thead>";
  return (
    '<section class="apps-company">' +
    '<div class="apps-company-head">' +
    orgHtml +
    '<span class="apps-company-count">' +
    c.apps.length +
    "</span></div>" +
    '<table class="apps-table">' +
    head +
    "<tbody>" +
    c.apps.map(_appRow).join("") +
    "</tbody></table>" +
    "</section>"
  );
}

export function renderApplications() {
  const root = document.getElementById("applicationsSection");
  if (!root) return;

  const companies = collectApplications(groups, archivedGroups);
  const { total, byStatus } = summarizeApplications(companies);
  const active = state.appStatusFilter || "all";

  const intro =
    '<div class="apps-intro">' +
    '<h2 class="apps-h2">✉️ <span>' +
    escHtml(T("apps_title", "Applications")) +
    "</span></h2>" +
    '<p class="apps-sub">' +
    escHtml(
      T(
        "apps_sub",
        "Roles you've applied to, grouped by company. Artifact keys only — no private content.",
      ),
    ) +
    "</p></div>";

  if (total === 0) {
    root.innerHTML =
      intro +
      '<div class="apps-empty"><div class="apps-empty-icon">✉️</div>' +
      escHtml(
        T(
          "apps_empty",
          "No applications yet. Mark a role applied in Triage or Today.",
        ),
      ) +
      "</div>";
    return;
  }

  // Apply the status filter (a pure copy — never mutate the collected data).
  let shown = companies;
  if (active !== "all") {
    shown = companies
      .map((c) => ({ ...c, apps: c.apps.filter((a) => a.status === active) }))
      .filter((c) => c.apps.length);
  }

  const body = shown.length
    ? shown.map(_companyBlock).join("")
    : '<div class="apps-empty">' +
      escHtml(T("apps_none_match", "Nothing matches this status.")) +
      "</div>";

  root.innerHTML =
    intro +
    _filterChips(byStatus, active) +
    '<div class="apps-list">' +
    body +
    "</div>";

  // Filter chips.
  root.querySelectorAll(".apps-chip[data-app-status]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.appStatusFilter = btn.getAttribute("data-app-status");
      renderApplications();
    });
  });

  // Company links open the profile page (same as elsewhere).
  root
    .querySelectorAll(".apps-company-link[data-company-slug]")
    .forEach(function (el) {
      el.addEventListener("click", function () {
        const slug = el.getAttribute("data-company-slug");
        if (slug && window.openCompanyProfile) window.openCompanyProfile(slug);
      });
    });
}

export function initApplications() {
  renderApplications();
}
