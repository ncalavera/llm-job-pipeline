// =============================================================================
// palette.js — ⌘K command palette logic (U17, DHA-401). PURE: no DOM, no
// state.js, no i18n. It imports only the equally-pure escHtml/qualityBand from
// helpers.js, so it unit-tests directly under `node --test`. The overlay DOM
// shell (build, open/close, focus trap, key wiring, live-data binding) lives in
// app.js — the same pure-logic / thin-shell split keys.js (pure) + catalog.js
// (shell) uses, per the pipeline.js lesson (KTD2).
//
// The palette searches the already-loaded payload (client-side substring match,
// no fuzzy) over vacancies (title + org) and companies (name), groups matches by
// type, and jumps to the chosen result's detail page. The DOM shell adapts the
// live app data into this module's flat input shape at call time (never a cached
// snapshot — KTD7), so this module stays decoupled from app field names.
//
// Ranking (documented, deterministic): each match is ranked by WHERE the query
// lands in the field, best first —
//   0 RANK_PREFIX    the field starts with the query
//   1 RANK_WORD      the query starts at a word boundary inside the field
//   2 RANK_SUBSTRING the query appears elsewhere in the field
// A vacancy's rank is the BEST (lowest) across its title and org. Ties break by
// score descending, then by original input order (stable). Word boundary is
// Unicode-aware: the character before the match is not a letter or a number.
// =============================================================================

import { escHtml, qualityBand } from "./helpers.js";

export const RANK_PREFIX = 0;
export const RANK_WORD = 1;
export const RANK_SUBSTRING = 2;

// Per-group cap on rendered matches — bounds the overlay list for a broad query
// (a single letter can match hundreds of rows). Small fixtures never hit it.
const DEFAULT_LIMIT = 25;

function numOr(v, fallback) {
  return typeof v === "number" && !Number.isNaN(v) ? v : fallback;
}

// Rank of `query` within `field`, or -1 for no match. Case-insensitive.
export function matchRank(field, query) {
  if (field == null) return -1;
  const q = String(query == null ? "" : query).toLowerCase();
  if (!q) return -1;
  const f = String(field).toLowerCase();
  const idx = f.indexOf(q);
  if (idx === -1) return -1;
  if (idx === 0) return RANK_PREFIX;
  const prev = f[idx - 1];
  // Boundary when the preceding char is not a letter/number (space, punctuation,
  // slash, dash, …). Unicode-aware so non-Latin scripts classify correctly too.
  return /[\p{L}\p{N}]/u.test(prev) ? RANK_SUBSTRING : RANK_WORD;
}

// Best (lowest) rank of `query` across several fields, or -1 if none match.
function bestRank(fields, query) {
  let best = -1;
  for (const field of fields) {
    const r = matchRank(field, query);
    if (r !== -1 && (best === -1 || r < best)) best = r;
  }
  return best;
}

// Rank + sort + cap one type's candidates. `mapped` items already carry the
// palette result shape plus `_fields` (searched) and `_order` (input index).
function rankGroup(mapped, query, limit) {
  const matched = [];
  for (const m of mapped) {
    const rank = bestRank(m._fields, query);
    if (rank === -1) continue;
    matched.push({ ...m, rank });
  }
  matched.sort((a, b) => {
    if (a.rank !== b.rank) return a.rank - b.rank;
    if (b.score !== a.score) return b.score - a.score; // higher fit first
    return a._order - b._order; // stable
  });
  return matched.slice(0, limit).map((m) => {
    // Drop the internal bookkeeping fields from the returned result.
    const { _fields, _order, ...result } = m;
    return result;
  });
}

/**
 * Filter the loaded payload into grouped, ranked palette results.
 *
 * `data`: {
 *   vacancies: [{ id, title, org, score }],   // score = the fit number, or null
 *   companies: [{ slug, name, score }],
 * }
 * Returns { vacancies: [result], companies: [result], flat: [result] } where
 * `flat` = vacancies-then-companies (the order arrow keys / Enter walk). Each
 * result carries a stable `key` ("v:"+id / "c:"+slug) so the highlighted item
 * survives a mid-open data refresh (KTD7).
 *
 * A blank / whitespace-only query returns all-empty — the palette shows its
 * "type to search" state until a real query arrives (plan test scenario).
 */
export function filterPalette(query, data, opts) {
  const q = String(query == null ? "" : query).trim();
  const limit = (opts && opts.limit) || DEFAULT_LIMIT;
  if (!q) return { vacancies: [], companies: [], flat: [] };

  const vIn = (data && data.vacancies) || [];
  const cIn = (data && data.companies) || [];

  const vMapped = vIn.map((v, i) => ({
    type: "vacancy",
    id: v && v.id,
    key: "v:" + (v && v.id),
    title: (v && v.title) || "",
    org: (v && v.org) || "",
    score: numOr(v && v.score, -1),
    _fields: [(v && v.title) || "", (v && v.org) || ""],
    _order: i,
  }));
  const cMapped = cIn.map((c, i) => ({
    type: "company",
    slug: c && c.slug,
    key: "c:" + (c && c.slug),
    name: (c && c.name) || "",
    score: numOr(c && c.score, -1),
    _fields: [(c && c.name) || ""],
    _order: i,
  }));

  const vacancies = rankGroup(vMapped, q, limit);
  const companies = rankGroup(cMapped, q, limit);
  return { vacancies, companies, flat: vacancies.concat(companies) };
}

// The ordered result keys the selection cursor walks (flat order).
export function flatKeys(fr) {
  return ((fr && fr.flat) || []).map((r) => r.key);
}

// The router call a chosen result maps to — pure so the "which action per result
// type" contract is unit-testable; the DOM shell dispatches it to
// window.openVacancyRoute / window.openCompanyProfile.
export function routeForResult(result) {
  if (!result) return null;
  if (result.type === "vacancy" && result.id != null)
    return { kind: "vacancy", id: result.id };
  if (result.type === "company" && result.slug != null)
    return { kind: "company", slug: result.slug };
  return null;
}

// ---------------------------------------------------------------------------
// Selection cursor — id(key)-keyed, mirroring keys.js's createCursor so the
// highlight survives a live-data refresh (KTD7). Adds reset() to auto-highlight
// the top match when a NEW query's results land (standard palette UX), which the
// keyboard-triage cursor deliberately does not do.
// ---------------------------------------------------------------------------

export function createSelection() {
  let key = null; // selected result key, or null when nothing is highlighted
  let index = -1; // key's last known position in the flat list (-1 = none)

  function selectAt(keys, i) {
    if (!keys.length) {
      key = null;
      index = -1;
      return null;
    }
    const clamped = Math.max(0, Math.min(i, keys.length - 1));
    index = clamped;
    key = keys[clamped];
    return key;
  }

  return {
    get key() {
      return key;
    },
    get index() {
      return index;
    },

    // A fresh query's results: highlight the top match, or clear if none.
    reset(keys) {
      return selectAt(keys || [], 0);
    },

    // Arrow movement (delta +1 down / -1 up), clamped at both ends. From a
    // dormant cursor the first press lands on the top row. If the key drifted
    // out of the list without a reconcile, step from its remembered index.
    move(delta, keys) {
      keys = keys || [];
      if (!keys.length) {
        key = null;
        index = -1;
        return null;
      }
      const at = key == null ? -1 : keys.indexOf(key);
      let cur;
      if (at !== -1) cur = at;
      else if (index >= 0) cur = Math.min(index, keys.length - 1);
      else cur = -1; // dormant → cur+delta lands on row 0 either direction
      return selectAt(keys, cur + delta);
    },

    // A mid-open data refresh (KTD7): if the highlighted key is still present,
    // keep it and refresh its index — a poll that inserts rows above the
    // selection moves the index but not the key (so a pending Enter still opens
    // the same result). If the key is gone, fall to the nearest reachable index.
    // A dormant cursor stays dormant; an emptied list clears it.
    reconcile(keys) {
      keys = keys || [];
      if (key == null) return null;
      if (!keys.length) {
        key = null;
        index = -1;
        return null;
      }
      const at = keys.indexOf(key);
      if (at !== -1) {
        index = at;
        return key;
      }
      return selectAt(keys, index);
    },

    set(nextKey, keys) {
      keys = keys || [];
      key = nextKey == null ? null : nextKey;
      index = key == null ? -1 : keys.indexOf(key);
      return key;
    },

    clear() {
      key = null;
      index = -1;
    },
  };
}

// ---------------------------------------------------------------------------
// Markup — pure (uses only escHtml/qualityBand). Every externally-sourced string
// (titles, org names, company names) routes through escHtml in BOTH text and
// attribute (title="…") positions; the query is never interpolated into markup
// (no match highlighting), so it cannot inject. onclick/onmouseenter carry only
// the integer flat index, so no id/slug is ever spliced into a handler.
// ---------------------------------------------------------------------------

function optionHtml(r, index, active) {
  const domId = "palette-opt-" + index;
  const hasScore = typeof r.score === "number" && r.score >= 0;
  const scoreCls = hasScore
    ? "q-" + qualityBand(r.score) + "-bg"
    : "palette-opt-score--none";
  const scoreTxt = hasScore ? String(Math.round(r.score)) : "—";
  const primary = r.type === "vacancy" ? r.title : r.name;
  const secondary = r.type === "vacancy" ? r.org : "";
  const secondaryHtml = secondary
    ? '<span class="palette-opt-org">' + escHtml(secondary) + "</span>"
    : "";
  return (
    '<div class="palette-option' +
    (active ? " palette-option--active" : "") +
    '" role="option" id="' +
    domId +
    '" data-idx="' +
    index +
    '" aria-selected="' +
    (active ? "true" : "false") +
    '" onclick="paletteChoose(' +
    index +
    ')" onmouseenter="paletteHover(' +
    index +
    ')">' +
    '<span class="palette-opt-score ' +
    scoreCls +
    '">' +
    escHtml(scoreTxt) +
    "</span>" +
    '<span class="palette-opt-body">' +
    '<span class="palette-opt-title" title="' +
    escHtml(primary) +
    '">' +
    escHtml(primary) +
    "</span>" +
    secondaryHtml +
    "</span>" +
    "</div>"
  );
}

/**
 * Build the grouped listbox markup for a filter result. `activeIndex` is the
 * flat index of the highlighted option (-1 for none). `opts.labelVacancies` /
 * `opts.labelCompanies` are the resolved (already-translated) group headings —
 * the shell passes them so this module needs no i18n import. Returns "" for an
 * empty result (the shell renders the empty / hint state itself).
 */
export function resultsHtml(fr, activeIndex, opts) {
  const o = opts || {};
  const labels = {
    vacancy: o.labelVacancies || "Vacancies",
    company: o.labelCompanies || "Companies",
  };
  const flat = (fr && fr.flat) || [];
  let html = "";
  let curType = null;
  for (let i = 0; i < flat.length; i++) {
    const r = flat[i];
    if (r.type !== curType) {
      if (curType !== null) html += "</div>";
      curType = r.type;
      html +=
        '<div class="palette-group" role="group" aria-label="' +
        escHtml(labels[curType] || curType) +
        '">' +
        '<div class="palette-group-label">' +
        escHtml(labels[curType] || curType) +
        "</div>";
    }
    html += optionHtml(r, i, i === activeIndex);
  }
  if (curType !== null) html += "</div>";
  return html;
}
