// =============================================================================
// archive.js — Archive view: read-only list of archived vacancies
// =============================================================================

import { archivedGroups } from "./state.js";
import { saveToServer } from "./api.js";
import {
  escHtml,
  jsAttr,
  relativeTime,
  safeUrl,
  qualityBand,
} from "./helpers.js";
import { T } from "./i18n.js";

// Archived rows are NOT in state.js's groupsById (that map is built from the
// live `groups` array only) - so they get their own archivedGroupsById index,
// which the vacancy route now consults (state.js / vacancy.js). A row click
// opens that read-only detail page exactly like a Browse row does; a per-row
// Restore writes the pre-archive-less default ('unseen') back through the same
// /api/save channel every status write uses.

let archiveInited = false;

// The ordered id queue for the currently-rendered archive rows - what a row
// click hands the vacancy route as its ←/→ browse queue (mirrors catalog.js's
// _browseQueue). Recomputed on every renderArchive.
let _archiveQueue = [];

export function initArchive() {
  if (!archiveInited) {
    const sel = document.getElementById("archiveOrgFilter");
    if (sel) {
      const orgs = [...new Set(archivedGroups.map((g) => g.org))].sort();
      sel.innerHTML = '<option value="">All companies</option>';
      orgs.forEach((org) => {
        const opt = document.createElement("option");
        opt.value = org;
        opt.textContent = org;
        sel.appendChild(opt);
      });
      // Open on "All companies" explicitly. The browser can restore a stale
      // <select> value across a reload/bfcache (the option is added by JS, so
      // restoration can land on the alphabetically-first org), which would
      // silently start the archive filtered to one company. autocomplete="off"
      // on the element blocks that; this makes the intent deterministic too.
      sel.value = "";
    }
    archiveInited = true;
  }
  renderArchive();
}

export function renderArchive() {
  const grid = document.getElementById("archiveGrid");
  if (!grid) return;
  const query = (
    document.getElementById("archiveSearch")?.value || ""
  ).toLowerCase();
  const orgFilter = document.getElementById("archiveOrgFilter")?.value || "";

  const filtered = archivedGroups.filter((g) => {
    if (orgFilter && g.org !== orgFilter) return false;
    if (query) {
      const searchable = (
        g.title +
        " " +
        g.org +
        " " +
        (g.locations || []).map((l) => l.location).join(" ")
      ).toLowerCase();
      if (!searchable.includes(query)) return false;
    }
    return true;
  });

  // The row-click browse queue for this filtered view (parity with Browse).
  _archiveQueue = filtered.map((g) => g.id);

  const countEl = document.getElementById("archiveResultsCount");
  if (countEl) {
    countEl.textContent =
      filtered.length + " of " + archivedGroups.length + " vacancies";
  }

  if (filtered.length === 0) {
    grid.innerHTML =
      '<div class="catalog-empty"><div class="catalog-empty-icon">🗂</div>' +
      (query || orgFilter
        ? T("archive_no_match", "Nothing matches the filters")
        : T("archive_empty", "The archive is empty")) +
      "</div>";
    return;
  }

  // Per-row isolation: one malformed archived row must never blank the whole
  // grid. The count above is already set, so an unguarded `.map(...).join("")`
  // that throws mid-row would leave the grid empty while the header still says
  // "N of M vacancies" — the exact "count computes but no cards" symptom. A row
  // that throws falls back to a minimal card so the rendered count always
  // matches the header.
  grid.innerHTML = filtered
    .map((g) => {
      try {
        return buildArchiveRow(g);
      } catch (err) {
        console.error("archive: minimal row for malformed row", g && g.id, err);
        return buildArchiveFallbackRow(g);
      }
    })
    .join("");
}

// First location's display text, "+N" suffixed when there are more, with a
// title tooltip listing them all — same reduction Browse's dense rows use
// (catalog.js's primaryLocationInfo) so a flat list stays scannable.
function primaryLocationInfo(g) {
  const locs = (g.locations || []).filter((l) => l.location);
  if (!locs.length) return null;
  const extra = locs.length - 1;
  return {
    text: locs[0].location + (extra > 0 ? " +" + extra : ""),
    title: locs.map((l) => l.location).join(", "),
  };
}

function buildArchiveRow(g) {
  const score = g.llm_score;
  const scoreCls =
    score == null ? "vac-score--none" : "q-" + qualityBand(score) + "-bg";
  const scoreTxt = score == null ? "—" : String(score);

  const firstUrl = safeUrl(
    (g.locations || []).find((l) => l.url)?.url || g.org_url || "",
  );
  // stopPropagation: the whole row now opens the vacancy page, so the title's
  // outbound link must not also trigger that navigation on click.
  const titleHtml = firstUrl
    ? '<a href="' +
      escHtml(firstUrl) +
      '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' +
      escHtml(g.title) +
      "</a>"
    : escHtml(g.title);

  const loc = primaryLocationInfo(g);
  const subParts = [escHtml(g.company_name || g.org)];
  if (loc) {
    subParts.push(
      '<span title="' +
        escHtml(loc.title) +
        '">' +
        escHtml(loc.text) +
        "</span>",
    );
  }

  const seenText = g.first_seen ? relativeTime(g.first_seen, T) : "—";
  const idAttr = jsAttr(g.id);

  return (
    '<div class="archive-row" data-id="' +
    escHtml(g.id) +
    '" role="button" tabindex="0" onclick="openArchiveRow(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openArchiveRow('" +
    idAttr +
    "')}\">" +
    '<div class="archive-row-score ' +
    scoreCls +
    '">' +
    scoreTxt +
    "</div>" +
    '<div class="archive-row-role">' +
    '<div class="archive-row-title">' +
    titleHtml +
    "</div>" +
    '<div class="archive-row-sub">' +
    subParts.join(" · ") +
    "</div>" +
    "</div>" +
    '<span class="archive-row-seen">' +
    escHtml(seenText) +
    "</span>" +
    restoreBtnHtml(idAttr) +
    "</div>"
  );
}

// Per-row un-archive control. stopPropagation so it never also opens the
// vacancy page; the write + optimistic removal live in restoreArchivedVacancy.
function restoreBtnHtml(idAttr) {
  const restoreLabel = escHtml(T("archive_restore", "Restore"));
  return (
    '<button class="archive-row-restore" onclick="event.stopPropagation();restoreArchivedVacancy(\'' +
    idAttr +
    '\')" title="' +
    restoreLabel +
    '" aria-label="' +
    restoreLabel +
    '">↩</button>'
  );
}

// Rendered in place of a row that made buildArchiveRow throw. Reads each
// scalar field through a guard (a field could be the very thing that threw),
// so the fallback itself can never throw — the rendered row count stays
// consistent with the header and the archive degrades to a bare row, never a
// blank grid.
function safeField(read, dflt) {
  try {
    const v = read();
    return v == null ? dflt : v;
  } catch {
    return dflt;
  }
}

function buildArchiveFallbackRow(g) {
  const org = escHtml(
    safeField(() => g.company_name || g.org, "") || "Unknown company",
  );
  const title = escHtml(safeField(() => g.title, "") || "Untitled role");
  const rawId = String(safeField(() => g.id, ""));
  const id = escHtml(rawId);
  const idAttr = jsAttr(rawId);
  return (
    '<div class="archive-row" data-id="' +
    id +
    '" role="button" tabindex="0" onclick="openArchiveRow(\'' +
    idAttr +
    "')\" onkeydown=\"if((event.key==='Enter'||event.key===' ')&&event.target===event.currentTarget){event.preventDefault();openArchiveRow('" +
    idAttr +
    "')}\">" +
    '<div class="archive-row-score vac-score--none">—</div>' +
    '<div class="archive-row-role">' +
    '<div class="archive-row-title">' +
    title +
    "</div>" +
    '<div class="archive-row-sub">' +
    org +
    "</div>" +
    "</div>" +
    '<span class="archive-row-seen">—</span>' +
    restoreBtnHtml(idAttr) +
    "</div>"
  );
}

// ---------------------------------------------------------------------------
// Row actions
// ---------------------------------------------------------------------------

// Open an archived role's read-only detail page (mirrors catalog.js's
// openCatalogRow): hands the vacancy route the current filtered id queue so
// ←/→ walk the archive list. window.openVacancyRoute is wired by app.js.
export function openArchiveRow(id) {
  if (typeof window !== "undefined" && window.openVacancyRoute) {
    window.openVacancyRoute(id, { context: "archive", queue: _archiveQueue });
  }
}

// Un-archive: optimistically drop the row, then persist 'unseen' via the same
// /api/save channel every status write uses. There is no stored pre-archive
// status (archiving overwrites `status` in the DB), so restore lands on
// 'unseen' - the vacancy re-enters the normal review flow on the next fetch.
// Revert-on-error mirrors companies.js's reviewCompany (saveCompanyReview).
export function restoreArchivedVacancy(id) {
  const idx = archivedGroups.findIndex((g) => g && g.id === id);
  if (idx === -1) return;
  const [removed] = archivedGroups.splice(idx, 1);
  renderArchive();
  Promise.resolve(saveToServer(id, "unseen")).then((ok) => {
    if (!ok) {
      archivedGroups.splice(idx, 0, removed);
      renderArchive();
    }
  });
}

if (typeof window !== "undefined") {
  window.openArchiveRow = openArchiveRow;
  window.restoreArchivedVacancy = restoreArchivedVacancy;
}
