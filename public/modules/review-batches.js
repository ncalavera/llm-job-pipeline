// =============================================================================
// review-batches.js — the ONE seam the daily review reads its batches through.
//
// Today a batch is derived in the browser from the quoted requirement conflicts
// (reason-batches.js). The strong model's batch table lands later; when it does,
// only reviewSections() changes — the review screen never touches reasonBatch()
// directly.
// =============================================================================

import { REASON_GROUPS, reasonBatch } from "./reason-batches.js";
import { screenRequirements } from "./derive.js";

const BATCH_TITLE = Object.fromEntries(REASON_GROUPS);

// A batch's default decision is a proposal, never an action. These batches hold
// roles under the visible score floor whose REQUIRED conditions conflict with
// the profile, so the proposal is Pass; the reviewer overrides row by row.
const BATCH_NOTE = {
  eligibility:
    "every role here quotes a required location, language or work-permit " +
    "condition that may not match your profile",
  expertise:
    "every role here quotes a required qualification or years of experience " +
    "that may not match your profile",
};

/** The one requirement conflict worth putting in the row, or null. */
export function topConflict(g, fingerprint) {
  const batch = reasonBatch(g, fingerprint);
  if (batch && batch.reasons.length) {
    const r = batch.reasons[0];
    return { text: r.note || r.kind, quote: r.quote };
  }
  // Outside a batch, still surface the first possible conflict the facts name.
  const requirements = screenRequirements(g);
  const comparisons = g.screening?.profile_comparison;
  for (const c of Array.isArray(comparisons) ? comparisons : []) {
    if (!c || c.finding !== "possible_conflict") continue;
    const r = Number.isInteger(c.requirement) && requirements[c.requirement];
    if (!r) continue;
    return { text: c.note || r.value || r.kind, quote: r.quote || "" };
  }
  return null;
}

/** Every quoted requirement with its profile finding — the row's expansion. */
export function requirementFacts(g) {
  const comparisons = Array.isArray(g.screening?.profile_comparison)
    ? g.screening.profile_comparison
    : [];
  return screenRequirements(g)
    .map((r, index) => {
      if (!r || typeof r.quote !== "string" || !r.quote.trim()) return null;
      const c = comparisons.find((x) => x && x.requirement === index);
      return {
        kind: r.kind || "other",
        strength: r.strength || "unknown",
        value: r.value || "",
        quote: r.quote,
        finding: c ? c.finding : "unknown",
        note: c ? c.note || "" : "",
      };
    })
    .filter(Boolean);
}

/**
 * Split the rows into sections ordered by their nearest open deadline — every
 * section, the unbatched remainder included. Pinning the batches first put the
 * primacy slot, the strongest position in a list, on the roles the screen
 * proposes to discard.
 * @returns {Array<{key, title, note, defaultStatus, rows}>}
 */
export function reviewSections(rows, fingerprint) {
  const byKey = new Map();
  const rest = [];
  for (const g of rows) {
    const batch = reasonBatch(g, fingerprint);
    if (!batch) {
      rest.push(g);
      continue;
    }
    if (!byKey.has(batch.key)) {
      byKey.set(batch.key, {
        key: batch.key,
        title: BATCH_TITLE[batch.key] || batch.key,
        note: BATCH_NOTE[batch.key] || "",
        defaultStatus: "passed",
        rows: [],
      });
    }
    byKey.get(batch.key).rows.push(g);
  }
  const sections = [...byKey.values()];
  if (rest.length)
    sections.push({
      key: "unbatched",
      title: "New, not batched",
      note: "",
      defaultStatus: null,
      rows: rest,
    });
  // Infinity - Infinity is NaN, which makes the order of two deadline-less
  // sections arbitrary between renders.
  return sections.sort((a, b) => {
    const x = nearestDeadline(a.rows);
    const y = nearestDeadline(b.rows);
    return x === y ? 0 : x - y;
  });
}

/**
 * Days to the nearest deadline still OPEN in a section; Infinity when none is.
 * A lapsed deadline is not a reason to review a batch sooner, and reporting one
 * as "the nearest deadline" reads as an alarm the reviewer cannot act on.
 */
export function nearestDeadline(rows, today = new Date()) {
  let best = Infinity;
  for (const g of rows) {
    const days = daysToDeadline(g, today);
    if (days != null && days >= 0 && days < best) best = days;
  }
  return best;
}

/** Whole days from today to the role's deadline, or null when it has none. */
export function daysToDeadline(g, today = new Date()) {
  const day = String(g?.deadline || "").slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const midnight = Date.parse(today.toISOString().slice(0, 10));
  const target = Date.parse(day);
  if (!Number.isFinite(target) || !Number.isFinite(midnight)) return null;
  return Math.round((target - midnight) / 86400000);
}
