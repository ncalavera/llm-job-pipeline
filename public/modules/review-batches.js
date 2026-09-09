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

// Every requirement kind already has a "screen_<kind>" string in both
// languages, so the key needs no table of its own.
const kindWord = (kind, t) =>
  t("screen_" + (kind || "other"), kind || "other");

// What fits the column at 1280 without truncating. Past this the phrase gets
// an ellipsis, which is what made the old column unreadable: every value ended
// mid-word and the cell carried nothing at a glance.
const CONFLICT_CHARS = 36;

/**
 * Shorten one requirement to a phrase that fits the row.
 *
 * The model writes `value` as a clause, sometimes several joined by commas or
 * a pipe. The first clause is the requirement; the rest is elaboration that
 * belongs in the expanded panel. Cutting there beats cutting mid-word.
 */
export function shortenRequirement(value, limit = CONFLICT_CHARS) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text.length <= limit) return text;
  const clause = text.split(/[;|(]|,\s/)[0].trim();
  // A one-word first clause is short but says less than a trimmed sentence,
  // so only a clause with real content wins over the word cut.
  if (clause.length >= 18 && clause.length <= limit) return clause;
  const cut = text.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return (space > 12 ? cut.slice(0, space) : cut).trim() + "…";
}

/**
 * The one requirement conflict worth putting in the row, as a SHORT phrase.
 *
 * Built from the requirement's own kind and value, never from the model's
 * free-text note: the note is a Russian sentence that truncated mid-word in a
 * 260px column and carried nothing at a glance. `full` keeps the note and the
 * profile factor for the hover title and the expanded panel.
 */
export function topConflict(g, fingerprint, t = (k, f) => f) {
  const requirements = screenRequirements(g);
  const comparisons = Array.isArray(g.screening?.profile_comparison)
    ? g.screening.profile_comparison
    : [];
  const batch = reasonBatch(g, fingerprint);
  const wanted = batch && batch.reasons.length ? batch.reasons[0].quote : null;
  const pick =
    comparisons.find(
      (c) =>
        c &&
        c.finding === "possible_conflict" &&
        Number.isInteger(c.requirement) &&
        requirements[c.requirement] &&
        (!wanted || requirements[c.requirement].quote === wanted),
    ) ||
    comparisons.find(
      (c) =>
        c &&
        c.finding === "possible_conflict" &&
        Number.isInteger(c.requirement) &&
        requirements[c.requirement],
    );
  if (!pick) return null;
  const r = requirements[pick.requirement];
  const kind = kindWord(r.kind, t);
  const short = shortenRequirement(r.value);
  return {
    // No kind prefix: the column is headed "Top conflict with profile" and the
    // amber says the rest. Prefixed, the kind alone ate a third of the cell in
    // the longer language.
    text: short || kind,
    full: [kind, r.value, pick.note, pick.profile_factor]
      .filter(Boolean)
      .join(" — "),
    quote: r.quote || "",
  };
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
  sections.sort((a, b) => {
    const x = nearestDeadline(a.rows);
    const y = nearestDeadline(b.rows);
    return x === y ? 0 : x - y;
  });
  // Numbered in the order they are read, so "Batch 2" means the same thing to
  // the reader and to anyone he tells about it. The unbatched rest is not one.
  let n = 0;
  for (const section of sections)
    if (section.defaultStatus) section.number = ++n;
  return sections;
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
