// =============================================================================
// reports.js — Reports section: the research behind the search, readable on
// the dashboard.
//
// Every report written for this search is a markdown file in a private repo:
// sector research, grant write-ups, company dossiers, the research done for one
// application. Stored that way the work is only reachable from the laptop it
// was written on. The dashboard already holds the roles and the applications;
// the reading behind them belongs next to what it was written for, and readable
// from a phone.
//
// Two views, one section: a list grouped by kind, and one report rendered from
// its markdown. Live-only — reports come from /api/reports, never from the baked
// payload, because a research library is not part of the vacancy snapshot and
// baking it would put every word of it into a static file.
//
// The pure half (grouping, ordering, markup) is exported and unit-tested; only
// initReports / renderReports touch the DOM or the network.
// =============================================================================

import { API_BASE } from "./state.js";
import { escHtml, jsAttr, mdToHtml, pluralForm } from "./helpers.js";
import { T, dateLocale } from "./i18n.js";

// The kinds, in the order the list shows them: most specific reading first,
// "other" last because it is the bucket, not a category. Twin of
// statuses.REPORT_KINDS and the SQL CHECK on report.kind.
export const REPORT_KIND_ORDER = [
  "research",
  "sector",
  "company",
  "grant",
  "other",
];

// Plain-word group headings. No category codes on screen.
export const REPORT_KIND_LABELS = {
  research: "Research",
  sector: "Sectors",
  company: "Companies",
  grant: "Grants",
  other: "Other",
};

// ---------------------------------------------------------------------------
// Pure logic
// ---------------------------------------------------------------------------

/** Newest-first ordering key for a report. Falls back to created_at, then to
 *  the empty string — an undated report sorts last rather than first, so it
 *  never pushes real, dated reports off the top of a group. */
function orderKey(report) {
  return String((report && (report.updated_at || report.created_at)) || "");
}

/**
 * Group reports by kind, newest first within each group.
 *
 * Returns [{ kind, label, reports }] in REPORT_KIND_ORDER, with empty groups
 * dropped — a heading over nothing reads as a loading failure. A report whose
 * kind is not in the vocabulary (an older row, a hand-edited one) lands in
 * "other" rather than vanishing: showing it in the wrong group is recoverable,
 * silently hiding it is not.
 */
export function groupReports(reports) {
  const byKind = new Map(REPORT_KIND_ORDER.map((k) => [k, []]));
  for (const report of reports || []) {
    const kind = byKind.has(report && report.kind) ? report.kind : "other";
    byKind.get(kind).push(report);
  }
  const groups = [];
  for (const kind of REPORT_KIND_ORDER) {
    const list = byKind.get(kind);
    if (!list.length) continue;
    list.sort((a, b) => {
      const ka = orderKey(a);
      const kb = orderKey(b);
      return kb < ka ? -1 : kb > ka ? 1 : 0;
    });
    groups.push({ kind, label: REPORT_KIND_LABELS[kind], reports: list });
  }
  return groups;
}

/** A date as "17 Aug 2026" — a report's age is read in months, so the year
 *  matters here in a way it does not in the applications table. */
export function formatReportDate(value, locale) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const month = d.toLocaleDateString(locale || "en-GB", {
    month: "short",
    timeZone: "UTC",
  });
  return d.getUTCDate() + " " + month + " " + d.getUTCFullYear();
}

/** One row in the list. Title, when it last changed, and the excerpt the API
 *  computed — enough to tell two reports apart without opening either. */
export function buildReportRow(report, opts) {
  const options = opts || {};
  const locale = options.locale || "en-GB";
  const slugAttr = jsAttr(report.slug);
  const when = formatReportDate(report.updated_at, locale);
  const excerpt = report.excerpt || "";

  return (
    '<div class="report-row" data-slug="' +
    escHtml(report.slug) +
    '" role="button" tabindex="0" onclick="openReport(\'' +
    slugAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openReport('" +
    slugAttr +
    "')}\">" +
    '<div class="report-row-main">' +
    '<div class="report-row-title">' +
    escHtml(report.title || report.slug) +
    "</div>" +
    (excerpt
      ? '<div class="report-row-excerpt">' + escHtml(excerpt) + "</div>"
      : "") +
    "</div>" +
    '<div class="report-row-date">' +
    escHtml(when) +
    "</div>" +
    "</div>"
  );
}

/** The whole list: a count line, then one block per kind. */
export function buildReportsList(reports, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const groups = groupReports(reports);

  if (!groups.length) {
    return (
      '<div class="catalog-empty"><div class="catalog-empty-icon">📚</div>' +
      escHtml(
        translate(
          "reports_empty",
          "No reports stored yet. Add one with `vac report add <path>`.",
        ),
      ) +
      "</div>"
    );
  }

  const total = (reports || []).length;
  // Some languages take three plural forms, chosen by the count's last digit,
  // so `total === 1` is not enough to pick the right word.
  const countWord = translate(
    "reports_count_" + pluralForm(total),
    total === 1 ? "report" : "reports",
  );
  const count =
    '<div class="reports-count-strip">' +
    escHtml(total + " " + countWord) +
    "</div>";

  const blocks = groups
    .map(
      (g) =>
        '<section class="report-group">' +
        '<h2 class="report-group-title">' +
        escHtml(translate("reports_kind_" + g.kind, g.label)) +
        '<span class="report-group-count">' +
        g.reports.length +
        "</span></h2>" +
        g.reports.map((r) => buildReportRow(r, options)).join("") +
        "</section>",
    )
    .join("");

  return count + blocks;
}

/** One report, rendered from its markdown with anchored headings. */
export function buildReportDetail(report, opts) {
  const options = opts || {};
  const translate = options.t || ((k, fb) => fb);
  const locale = options.locale || "en-GB";
  const meta = [
    translate(
      "reports_kind_" + report.kind,
      REPORT_KIND_LABELS[report.kind] || report.kind,
    ),
    formatReportDate(report.updated_at, locale),
    report.source_path || "",
  ].filter(Boolean);

  return (
    '<div class="report-detail">' +
    '<button type="button" class="report-back" onclick="closeReport()">' +
    escHtml(translate("reports_back", "← All reports")) +
    "</button>" +
    '<div class="report-detail-meta">' +
    escHtml(meta.join(" · ")) +
    "</div>" +
    // mdToHtml escapes its input before building any tag, so a report's own
    // text can never inject markup. `anchors` is on here and nowhere else:
    // a long report is linked section by section, while the same renderer
    // drawing short job descriptions must not emit duplicate ids.
    '<article class="report-body md-content">' +
    mdToHtml(report.body_md, { anchors: true }) +
    "</article>" +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// DOM shell
// ---------------------------------------------------------------------------

// The list, once loaded. Kept so returning from a report is instant and does
// not re-fetch; a fresh load happens when the section is opened again.
let _reports = null;
let _openSlug = null;
let _loadError = "";

function host() {
  return document.getElementById("reportsSection");
}

async function fetchJson(path) {
  const base = API_BASE || "";
  const res = await fetch(base + path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

export async function initReports() {
  _openSlug = null;
  _loadError = "";
  render();
  try {
    const data = await fetchJson("/api/reports");
    _reports = Array.isArray(data.reports) ? data.reports : [];
  } catch (err) {
    // Simple mode has no API at all, so this is the expected path there —
    // say what is missing rather than showing an empty library, which would
    // read as "you have written nothing".
    console.warn("reports: could not load the list", err);
    _reports = null;
    _loadError = T(
      "reports_unavailable",
      "Reports need the dashboard server — they are not part of the offline snapshot.",
    );
  }
  render();
}

export function renderReports() {
  render();
}

function render() {
  const el = host();
  if (!el) return;

  if (_openSlug && _reports) {
    const open = _reports.find((r) => r.slug === _openSlug);
    if (open && open.body_md) {
      el.innerHTML = buildReportDetail(open, { t: T, locale: dateLocale() });
      return;
    }
    el.innerHTML =
      '<div class="reports-loading">' +
      escHtml(T("reports_loading", "Loading…")) +
      "</div>";
    return;
  }

  if (_loadError) {
    el.innerHTML =
      '<div class="catalog-empty"><div class="catalog-empty-icon">📚</div>' +
      escHtml(_loadError) +
      "</div>";
    return;
  }
  if (_reports === null) {
    el.innerHTML =
      '<div class="reports-loading">' +
      escHtml(T("reports_loading", "Loading…")) +
      "</div>";
    return;
  }
  el.innerHTML = buildReportsList(_reports, { t: T, locale: dateLocale() });
}

/** Open one report. The list carries only an excerpt, so the body is fetched
 *  on demand and cached onto the list row — reopening it costs nothing. */
export async function openReport(slug) {
  _openSlug = slug;
  render();
  const row = (_reports || []).find((r) => r.slug === slug);
  if (row && row.body_md) return;
  try {
    const data = await fetchJson("/api/reports/" + encodeURIComponent(slug));
    if (row && data.report) Object.assign(row, data.report);
  } catch (err) {
    console.warn("reports: could not load", slug, err);
    if (row) {
      row.body_md = T(
        "reports_body_unavailable",
        "This report could not be loaded.",
      );
    }
  }
  render();
}

export function closeReport() {
  _openSlug = null;
  render();
}

if (typeof window !== "undefined") {
  window.openReport = openReport;
  window.closeReport = closeReport;
}
