// =============================================================================
// archive.js — Archive view: read-only list of archived vacancies
// =============================================================================

import { archivedGroups } from "./state.js";
import {
  escHtml,
  parseLocationChips,
  renderLocationChips,
  llmScoreBadge,
  screenScoreBadge,
  relativeTime,
  safeUrl,
} from "./helpers.js";
import { T } from "./i18n.js";

let archiveInited = false;

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
        return buildArchiveCard(g);
      } catch (err) {
        console.error(
          "archive: minimal card for malformed row",
          g && g.id,
          err,
        );
        return buildArchiveFallbackCard(g);
      }
    })
    .join("");
}

function buildArchiveCard(g) {
  // org_color is a [fg, bg] pair in fresh data, but a legacy/corrupt snapshot
  // row could carry null or a non-array — destructuring that throws, so coerce
  // before unpacking.
  const [fg, bg] = Array.isArray(g.org_color)
    ? g.org_color
    : ["#F97316", "#FFF7ED"];
  const firstUrl = safeUrl(
    (g.locations || []).find((l) => l.url)?.url || g.org_url || "",
  );
  const titleHtml = firstUrl
    ? '<a href="' +
      escHtml(firstUrl) +
      '" target="_blank" rel="noopener">' +
      escHtml(g.title) +
      "</a>"
    : escHtml(g.title);

  const catalogChips = (g.locations || []).flatMap((l) => {
    const chips = parseLocationChips(l.location);
    chips.forEach((c) => {
      if (l.url) c.url = l.url;
    });
    return chips;
  });
  const seen = new Set();
  const uniqueChips = catalogChips.filter((c) => {
    const key = c.text.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const locChips = renderLocationChips(uniqueChips, {
    chipClass: "loc-chip",
    useRegionColor: false,
    maxVisible: 3,
  });

  const firstSeenHtml = g.first_seen
    ? '<div class="card-comp-row"><span class="card-first-seen">' +
      relativeTime(g.first_seen, T) +
      "</span></div>"
    : "";

  const summaryText = g.llm_summary || g.snippet || "";
  const summaryHtml = summaryText
    ? '<p class="card-snippet">' + escHtml(summaryText) + "</p>"
    : "";

  return (
    '<div class="catalog-card archive-card" data-id="' +
    g.id +
    '">' +
    '<div class="catalog-card-header">' +
    '<div class="catalog-header-left">' +
    '<span class="catalog-org" style="color:' +
    fg +
    ";background:" +
    bg +
    '">' +
    escHtml(g.company_name || g.org) +
    "</span>" +
    "</div>" +
    '<div class="catalog-header-right">' +
    llmScoreBadge(g.llm_score) +
    screenScoreBadge(g) +
    "</div>" +
    "</div>" +
    '<h3 class="catalog-title">' +
    titleHtml +
    "</h3>" +
    '<div class="catalog-loc-row">' +
    locChips +
    "</div>" +
    firstSeenHtml +
    summaryHtml +
    "</div>"
  );
}

// Rendered in place of a row that made buildArchiveCard throw. Reads each
// scalar field through a guard (a field could be the very thing that threw),
// so the fallback itself can never throw — the card count stays consistent
// with the header and the archive degrades to a bare card, never a blank grid.
function safeField(read, dflt) {
  try {
    const v = read();
    return v == null ? dflt : v;
  } catch {
    return dflt;
  }
}

function buildArchiveFallbackCard(g) {
  const org = escHtml(
    safeField(() => g.company_name || g.org, "") || "Unknown company",
  );
  const title = escHtml(safeField(() => g.title, "") || "Untitled role");
  const id = escHtml(String(safeField(() => g.id, "")));
  return (
    '<div class="catalog-card archive-card" data-id="' +
    id +
    '">' +
    '<div class="catalog-card-header"><div class="catalog-header-left">' +
    '<span class="catalog-org">' +
    org +
    "</span></div></div>" +
    '<h3 class="catalog-title">' +
    title +
    "</h3>" +
    "</div>"
  );
}
